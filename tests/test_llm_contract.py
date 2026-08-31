"""Offline behavioral contracts for the OpenAI-compatible LLM boundary."""

from copy import deepcopy

import pytest
from openai import APIError, BadRequestError, RateLimitError

try:  # OpenAI 3.x vendors httpx as httpx2; 1.x/2.x use httpx.
    import httpx2 as httpx_compat
except ImportError:  # pragma: no cover - exercised by older Linux dependency sets
    import httpx as httpx_compat

from corecoder.llm import LLM, normalize_tool_calls


def _bare_llm() -> LLM:
    llm = LLM.__new__(LLM)
    llm.model = "test-model"
    llm.extra = {}
    llm.total_prompt_tokens = 0
    llm.total_completion_tokens = 0
    return llm


def _rate_limit_error() -> RateLimitError:
    request = httpx_compat.Request("POST", "https://example.invalid/chat")
    response = httpx_compat.Response(429, request=request)
    return RateLimitError("rate limited", response=response, body=None)


def _bad_request_error() -> BadRequestError:
    request = httpx_compat.Request("POST", "https://example.invalid/chat")
    response = httpx_compat.Response(400, request=request)
    return BadRequestError("unsupported stream_options", response=response, body=None)


class _EmptyChunk:
    usage = None
    choices = []


class _NullUsage:
    prompt_tokens = None
    completion_tokens = None


class _UsageChunk:
    usage = _NullUsage()
    choices = []


def test_transient_failure_does_not_trigger_a_second_stream_options_request():
    llm = _bare_llm()
    requests = []

    def fail(params):
        requests.append(deepcopy(params))
        raise _rate_limit_error()

    llm._call_with_retry = fail

    with pytest.raises(RateLimitError):
        llm.chat(messages=[{"role": "user", "content": "hello"}])

    assert len(requests) == 1
    assert "stream_options" in requests[0]


def test_bad_request_retries_once_without_stream_options():
    llm = _bare_llm()
    requests = []

    def call(params):
        requests.append(deepcopy(params))
        if len(requests) == 1:
            raise _bad_request_error()
        return iter([_EmptyChunk()])

    llm._call_with_retry = call

    result = llm.chat(messages=[{"role": "user", "content": "hello"}])

    assert result.content == ""
    assert len(requests) == 2
    assert "stream_options" in requests[0]
    assert "stream_options" not in requests[1]


def test_null_usage_fields_are_counted_as_zero():
    llm = _bare_llm()
    llm._call_with_retry = lambda params: iter([_UsageChunk()])

    result = llm.chat(messages=[{"role": "user", "content": "hello"}])

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert llm.total_prompt_tokens == 0
    assert llm.total_completion_tokens == 0


def test_api_error_without_a_status_code_is_reraised_unchanged():
    llm = _bare_llm()
    request = httpx_compat.Request("POST", "https://example.invalid/chat")
    error = APIError("provider failure", request=request, body=None)

    class Completions:
        @staticmethod
        def create(**params):
            raise error

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    llm.client = Client()

    with pytest.raises(APIError) as caught:
        llm._call_with_retry({"model": "test-model"}, max_retries=1)

    assert caught.value is error


def test_tool_call_normalization_preserves_malformed_fragments_and_unique_ids():
    calls = normalize_tool_calls(
        {
            0: {"id": "same", "name": "bash", "args": '{"command":"echo ok"}'},
            1: {"id": "same", "name": "bash", "args": '{"token":"secret"'},
            2: {"id": "", "name": "read_file", "args": '{"file_path":"a"}'},
        }
    )

    assert calls[0].arguments == {"command": "echo ok"}
    assert calls[0].parse_error is None
    assert calls[1].arguments == {}
    assert "malformed JSON" in calls[1].parse_error
    assert "duplicate call id" in calls[1].parse_error
    assert "secret" not in calls[1].raw_arguments_preview
    assert len(calls[1].raw_arguments_sha256) == 64
    assert calls[2].id.startswith("missing-2-")
    assert "missing call id" in calls[2].parse_error
    assert len({call.id for call in calls}) == 3
