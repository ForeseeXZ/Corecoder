"""LLM provider layer - thin wrapper over OpenAI-compatible APIs.

Since most providers (DeepSeek, Qwen, Kimi, GLM, Ollama, etc.) expose an
OpenAI-compatible endpoint, we just use the openai SDK directly.  Switch
provider by changing OPENAI_BASE_URL + OPENAI_API_KEY. That's it.

For providers that are NOT OpenAI-compatible (AWS Bedrock, Google Vertex,
etc.), use the LiteLLM backend which routes to 100+ providers through a
single unified interface. Set CORECODER_PROVIDER=litellm.
"""

import json
import hashlib
import os
import re
import time
from dataclasses import dataclass, field

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    parse_error: str | None = None
    raw_arguments_sha256: str | None = None
    raw_arguments_preview: str | None = None


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def message(self) -> dict:
        """Convert to OpenAI message format for appending to history."""
        msg: dict = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
        return msg


_SECRET_FRAGMENT_RE = re.compile(
    r'(?i)("(?:api[_-]?key|token|password|secret)"\s*:\s*")[^"]*'
)
_SECRET_MARKER_RE = re.compile(r"(?i)api[_-]?key|token|password|secret")


def normalize_tool_calls(tc_map: dict[int, dict]) -> list[ToolCall]:
    """Normalize streamed tool fragments without inventing empty arguments."""
    parsed: list[ToolCall] = []
    seen_ids: set[str] = set()
    for idx in sorted(tc_map):
        raw = tc_map[idx]
        raw_arguments = raw.get("args", "") or ""
        raw_hash = hashlib.sha256(raw_arguments.encode("utf-8")).hexdigest()
        errors: list[str] = []
        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                errors.append("tool arguments must be a JSON object")
                arguments = {}
        except (json.JSONDecodeError, TypeError):
            arguments = {}
            errors.append("malformed JSON arguments")

        call_id = raw.get("id", "") or ""
        if not call_id:
            call_id = f"missing-{idx}-{raw_hash[:10]}"
            errors.append("missing call id")
        elif call_id in seen_ids:
            original_id = call_id
            call_id = f"{original_id}-duplicate-{idx}-{raw_hash[:8]}"
            errors.append(f"duplicate call id: {original_id}")
        seen_ids.add(call_id)

        name = raw.get("name", "") or ""
        if not name:
            errors.append("missing tool name")
        preview = _SECRET_FRAGMENT_RE.sub(r'\1<redacted>', raw_arguments)[:240]
        if _SECRET_MARKER_RE.search(raw_arguments):
            preview = "<redacted-sensitive-fragment>"
        parsed.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments,
                parse_error="; ".join(errors) or None,
                raw_arguments_sha256=raw_hash,
                raw_arguments_preview=preview,
            )
        )
    return parsed


# Pricing per million tokens in RMB (CNY): (input, output).
# Extend or override with CORECODER_PRICING_CNY_PER_M, for example:
# {"mimo-v2.5":[1,2],"mimo-v2.5-pro":[3,6]}
_PRICING = {
    "deepseek-v4-flash": (1, 2),
    "deepseek-v4-pro": (3, 6),
}


def _pricing_for(model: str) -> tuple[float, float] | None:
    raw = os.getenv("CORECODER_PRICING_CNY_PER_M", "").strip()
    if raw:
        try:
            table = json.loads(raw)
            if model in table and len(table[model]) == 2:
                return float(table[model][0]), float(table[model][1])
        except Exception:
            pass
    return _PRICING.get(model)


class LLM:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        **kwargs,
    ):
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.extra = kwargs  # temperature, max_tokens, etc.
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @property
    def estimated_cost(self) -> float | None:
        """Rough cost estimate in RMB (CNY). Returns None if model not in pricing table."""
        pricing = _pricing_for(self.model)
        if not pricing:
            return None
        input_rate, output_rate = pricing
        return (
            self.total_prompt_tokens * input_rate / 1_000_000
            + self.total_completion_tokens * output_rate / 1_000_000
        )

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token=None,
    ) -> LLMResponse:
        """Send messages, stream back response, handle tool calls."""
        params: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            **self.extra,
        }
        if tools:
            params["tools"] = tools

        # stream_options is an OpenAI extension; not all providers support it
        try:
            params["stream_options"] = {"include_usage": True}
            stream = self._call_with_retry(params)
        except BadRequestError:
            params.pop("stream_options", None)
            stream = self._call_with_retry(params)

        content_parts: list[str] = []
        tc_map: dict[int, dict] = {}  # index -> {id, name, arguments_str}
        prompt_tok = 0
        completion_tok = 0

        for chunk in stream:
            # usage info comes in the final chunk
            if chunk.usage:
                prompt_tok = getattr(chunk.usage, "prompt_tokens", 0) or 0
                completion_tok = getattr(chunk.usage, "completion_tokens", 0) or 0

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # accumulate text
            if delta.content:
                content_parts.append(delta.content)
                if on_token:
                    on_token(delta.content)

            # accumulate tool calls across chunks
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tc_map:
                        tc_map[idx] = {"id": "", "name": "", "args": ""}
                    if tc_delta.id:
                        tc_map[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tc_map[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tc_map[idx]["args"] += tc_delta.function.arguments

        # parse accumulated tool calls
        parsed = normalize_tool_calls(tc_map)

        self.total_prompt_tokens += prompt_tok
        self.total_completion_tokens += completion_tok

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=parsed,
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
        )

    def _call_with_retry(self, params: dict, max_retries: int = 3):
        """Retry on transient errors with exponential backoff."""
        for attempt in range(max_retries):
            try:
                return self.client.chat.completions.create(**params)
            except (RateLimitError, APITimeoutError, APIConnectionError) as e:
                if attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                time.sleep(wait)
            except APIError as e:
                # 5xx = server error, retry; 4xx = client error, don't
                status_code = getattr(e, "status_code", None)
                if status_code and status_code >= 500 and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise


class LiteLLM(LLM):
    """LLM backend via LiteLLM, supporting 100+ providers.

    Use this when your target provider is NOT OpenAI-compatible
    (AWS Bedrock, Google Vertex, Cohere, etc.) or when you want
    a single interface to switch between any provider by changing
    the model string.

    Set CORECODER_PROVIDER=litellm and use LiteLLM model strings
    like ``anthropic/claude-3-haiku``, ``bedrock/anthropic.claude-v2``,
    ``vertex_ai/gemini-pro``, etc.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs,
    ):
        # skip LLM.__init__ which creates an OpenAI client
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.extra = kwargs
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token=None,
    ) -> LLMResponse:
        """Send messages via litellm, stream back response, handle tool calls."""
        params: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            **self.extra,
        }
        if tools:
            params["tools"] = tools

        stream = self._call_with_retry(params)

        content_parts: list[str] = []
        tc_map: dict[int, dict] = {}
        prompt_tok = 0
        completion_tok = 0

        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                prompt_tok = getattr(usage, "prompt_tokens", 0) or 0
                completion_tok = getattr(usage, "completion_tokens", 0) or 0

            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta

            if getattr(delta, "content", None):
                content_parts.append(delta.content)
                if on_token:
                    on_token(delta.content)

            if getattr(delta, "tool_calls", None):
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tc_map:
                        tc_map[idx] = {"id": "", "name": "", "args": ""}
                    if tc_delta.id:
                        tc_map[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tc_map[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tc_map[idx]["args"] += tc_delta.function.arguments

        parsed = normalize_tool_calls(tc_map)

        self.total_prompt_tokens += prompt_tok
        self.total_completion_tokens += completion_tok

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=parsed,
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
        )

    def _call_with_retry(self, params: dict, max_retries: int = 3):
        """Retry on transient errors with exponential backoff via litellm."""
        import litellm

        params["drop_params"] = True
        if self.api_key:
            params["api_key"] = self.api_key
        if self.base_url:
            params["api_base"] = self.base_url

        for attempt in range(max_retries):
            try:
                return litellm.completion(**params)
            except Exception as e:
                err = str(e).lower()
                is_transient = any(
                    kw in err
                    for kw in ["rate_limit", "timeout", "connection", "502", "503", "529"]
                )
                is_server = any(kw in err for kw in ["500", "502", "503", "504"])
                if (is_transient or is_server) and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise
