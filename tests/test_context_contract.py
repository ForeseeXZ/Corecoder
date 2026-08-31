"""Behavioral contracts for keeping Model Turn tool groups valid."""

from corecoder.context import ContextManager
from corecoder.compress import CompressionManager


def _assistant_tool_call(*call_ids: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "inspect", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def _assert_complete_tool_groups(messages: list[dict]) -> None:
    pending: list[str] = []
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            assert not pending
            pending = [call["id"] for call in message["tool_calls"]]
        elif message.get("role") == "tool":
            assert message.get("tool_call_id") in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending
    assert not pending


def _multi_tool_history() -> list[dict]:
    return [
        {"role": "user", "content": "earlier request " + "a" * 300},
        {"role": "assistant", "content": "earlier answer " + "b" * 300},
        _assistant_tool_call("call-1", "call-2"),
        {"role": "tool", "tool_call_id": "call-1", "content": "first"},
        {"role": "tool", "tool_call_id": "call-2", "content": "second"},
        {"role": "user", "content": "u1" * 20},
        {"role": "assistant", "content": "a1" * 20},
        {"role": "user", "content": "u2" * 20},
        {"role": "assistant", "content": "a2" * 20},
        {"role": "user", "content": "u3" * 20},
        {"role": "assistant", "content": "a3" * 20},
    ]


def test_context_compression_keeps_a_multi_tool_group_complete():
    messages = _multi_tool_history()
    context = ContextManager(max_tokens=400)

    assert context.maybe_compress(messages, llm=None) is True

    _assert_complete_tool_groups(messages)


def test_instrumented_compression_keeps_a_multi_tool_group_complete():
    messages = _multi_tool_history()
    context = CompressionManager(
        max_tokens=400,
        snip_at=0.50,
        summarize_at=0.70,
        collapse_at=0.99,
        keep_recent=8,
    )

    assert context.maybe_compress(messages, llm=None) is True

    _assert_complete_tool_groups(messages)
