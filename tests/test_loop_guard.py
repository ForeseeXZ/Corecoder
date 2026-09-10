"""Behavioral contracts for no-progress tool-call handling."""

from corecoder import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.runtime import ToolRuntime, ToolStatus
from corecoder.tools.base import Tool, ToolEffect
from tests.fakes import ScriptedLLM


class CountingTool(Tool):
    description = "Return a deterministic result and count physical executions."
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def __init__(self, name="inspect", effect=ToolEffect.READ, on_call=None):
        self.name = name
        self.effect = effect
        self.calls = []
        self.on_call = on_call

    def execute(self, value: str) -> str:
        self.calls.append(value)
        if self.on_call is not None:
            self.on_call()
        return f"result:{self.name}:{value}"


def _call(call_id, name="inspect", value="same"):
    return LLMResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments={"value": value})]
    )


def _agent(responses, tools, state):
    runtime = ToolRuntime(tools, state_provider=lambda: state["value"])
    return Agent(llm=ScriptedLLM(responses), tools=tools, runtime=runtime)


def test_identical_read_is_replayed_without_a_second_physical_execution():
    state = {"value": "workspace-1"}
    tool = CountingTool()
    agent = _agent(
        [_call("call-1"), _call("call-2"), LLMResponse(content="done")],
        [tool],
        state,
    )

    assert agent.chat("inspect twice") == "done"
    assert tool.calls == ["same"]
    assert agent.last_tool_observations[1].execution_mode == "cached"
    assert agent.last_tool_observations[1].related_call_id == "call-1"


def test_same_process_call_is_allowed_after_workspace_state_changes():
    state = {"value": "workspace-1"}

    def advance_state():
        state["value"] += "-changed"

    tool = CountingTool("process", ToolEffect.PROCESS, advance_state)
    agent = _agent(
        [
            _call("call-1", "process"),
            _call("call-2", "process"),
            LLMResponse(content="done"),
        ],
        [tool],
        state,
    )

    assert agent.chat("run after each change") == "done"
    assert tool.calls == ["same", "same"]
    assert all(item.execution_mode == "executed" for item in agent.last_tool_observations)


def test_repeated_process_call_gets_recovery_then_stalls_without_exception():
    state = {"value": "workspace-1"}
    tool = CountingTool("process", ToolEffect.PROCESS)
    agent = _agent(
        [
            _call("call-1", "process"),
            _call("call-2", "process"),
            _call("call-3", "process"),
        ],
        [tool],
        state,
    )

    assert agent.chat("repeat forever") == "(stalled: repeated tool calls made no progress)"
    assert agent.last_finish_reason == "no_progress"
    assert tool.calls == ["same"]
    assert [item.execution_mode for item in agent.last_tool_observations] == [
        "executed",
        "suppressed",
        "suppressed",
    ]
    assert agent.last_tool_observations[-1].status is ToolStatus.BLOCKED
    assert agent.last_tool_observations[-1].block_reason == "no_progress_stalled"


def test_short_abab_cycle_requires_recovery_and_then_stalls():
    state = {"value": "workspace-1"}
    first = CountingTool("first")
    second = CountingTool("second")
    agent = _agent(
        [
            _call("a1", "first"),
            _call("b1", "second"),
            _call("a2", "first"),
            _call("b2", "second"),
            _call("a3", "first"),
        ],
        [first, second],
        state,
    )

    assert agent.chat("cycle") == "(stalled: repeated tool calls made no progress)"
    assert first.calls == ["same"]
    assert second.calls == ["same"]
    assert agent.last_tool_observations[-2].block_reason == "duplicate_no_progress"
    assert agent.last_tool_observations[-1].block_reason == "no_progress_stalled"
