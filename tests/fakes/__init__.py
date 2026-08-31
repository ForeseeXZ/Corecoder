"""Deterministic system-boundary fakes used by offline contract tests."""

from copy import deepcopy
from collections.abc import Iterable

from corecoder.llm import LLMResponse


class ScriptedLLM:
    """Return predefined responses while recording each Agent request."""

    def __init__(self, responses: Iterable[LLMResponse]):
        self._responses = iter(responses)
        self.requests: list[dict] = []

    def chat(self, messages, tools=None, on_token=None) -> LLMResponse:
        self.requests.append(
            {
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
            }
        )
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise AssertionError("ScriptedLLM received an unexpected request") from exc
