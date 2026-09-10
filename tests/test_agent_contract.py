"""Offline behavioral contracts for the Agent loop."""

import threading
import time

import pytest

from corecoder import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools.base import Tool, ToolEffect, ToolExecutionResult
from corecoder.tools.agent import AgentTool
from corecoder.runtime import ToolStatus
from tests.fakes import ScriptedLLM


class InspectTool(Tool):
    name = "inspect"
    description = "Return a deterministic observation for a path."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self):
        self.calls = []

    def execute(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return f"contents:{kwargs['path']}"


class TypeErrorTool(Tool):
    name = "explode"
    description = "Raise a TypeError from inside the tool implementation."
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def execute(self, value: str) -> str:
        raise TypeError(f"cannot process {value}")


class CoordinatedTool(Tool):
    description = "Synchronize with another tool and return a named result."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, barrier: threading.Barrier, delay: float):
        self.name = name
        self._barrier = barrier
        self._delay = delay

    def execute(self) -> str:
        self._barrier.wait(timeout=2)
        time.sleep(self._delay)
        return f"result:{self.name}"


class InterruptingTool(Tool):
    description = "Simulate a user interrupt during tool execution."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str = "interrupt"):
        self.name = name

    def execute(self) -> str:
        raise KeyboardInterrupt


class FailingProcessTool(Tool):
    name = "run_check"
    description = "Return a deterministic non-zero process result."
    parameters = {"type": "object", "properties": {}, "required": []}
    effect = ToolEffect.PROCESS

    def execute(self) -> str:
        return "legacy"

    def execute_structured(self) -> ToolExecutionResult:
        return ToolExecutionResult(
            status=ToolStatus.NON_ZERO,
            content="SyntaxError: invalid source encoding\n[exit code: 1]",
            exit_code=1,
        )


def test_agent_returns_final_text_after_a_tool_observation():
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="inspect",
                        arguments={"path": "README.md"},
                    )
                ]
            ),
            LLMResponse(content="inspection complete"),
        ]
    )
    tool = InspectTool()
    agent = Agent(llm=llm, tools=[tool])

    result = agent.chat("Inspect the project readme")

    assert result == "inspection complete"
    assert tool.calls == [{"path": "README.md"}]
    assert llm.requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "contents:README.md",
    }
    assert agent.last_tool_observations[0].status is ToolStatus.SUCCESS
    assert agent.last_tool_observations[0].call_id == "call-1"


def test_agent_retries_once_when_model_goes_empty_after_a_failed_tool():
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call-failed", name="run_check", arguments={})
                ]
            ),
            LLMResponse(),
            LLMResponse(content="I inspected the failure and recovered."),
        ]
    )
    agent = Agent(llm=llm, tools=[FailingProcessTool()])

    result = agent.chat("Run the check and fix failures")

    assert result == "I inspected the failure and recovered."
    assert len(llm.requests) == 3
    assert llm.requests[2]["messages"][-1]["role"] == "user"
    assert "previous tool round failed" in llm.requests[2]["messages"][-1]["content"]


def test_agent_retries_when_model_goes_empty_after_a_successful_tool():
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call-success",
                        name="inspect",
                        arguments={"path": "two_sum.py"},
                    )
                ]
            ),
            LLMResponse(),
            LLMResponse(content="The requested work is complete and verified."),
        ]
    )
    agent = Agent(llm=llm, tools=[InspectTool()])

    result = agent.chat("Create the file and verify it")

    assert result == "The requested work is complete and verified."
    assert len(llm.requests) == 3
    recovery = llm.requests[2]["messages"][-1]
    assert recovery["role"] == "user"
    assert "previous tool round completed" in recovery["content"]
    assert "work or verification remains" in recovery["content"]


def test_agent_stops_after_bounded_empty_tool_recoveries():
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call-success",
                        name="inspect",
                        arguments={"path": "two_sum.py"},
                    )
                ]
            ),
            LLMResponse(),
            LLMResponse(),
            LLMResponse(),
        ]
    )
    agent = Agent(llm=llm, tools=[InspectTool()])

    result = agent.chat("Create the file and verify it")

    assert result == "(model repeatedly returned an empty response after tool calls)"
    assert agent.last_finish_reason == "model_failure"
    assert len(llm.requests) == 4


def test_agent_cannot_execute_a_tool_outside_its_capability_set(tmp_path):
    private_file = tmp_path / "private.txt"
    private_file.write_text("must-not-be-read")
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call-denied",
                        name="read_file",
                        arguments={"file_path": str(private_file)},
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = Agent(llm=llm, tools=[])

    agent.chat("Try an unavailable capability")

    observation = llm.requests[1]["messages"][-1]
    assert observation["content"] == "Error: unknown tool 'read_file'"
    assert "must-not-be-read" not in observation["content"]


def test_default_agents_do_not_share_mutable_tool_instances():
    first = Agent(llm=ScriptedLLM([]))
    second = Agent(llm=ScriptedLLM([]))

    assert first.tools is not second.tools
    assert all(left is not right for left, right in zip(first.tools, second.tools))


def test_sub_agent_cannot_recover_a_capability_missing_from_its_parent():
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="delegate-1",
                        name="agent",
                        arguments={"task": "try to use bash"},
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="forbidden-1",
                        name="bash",
                        arguments={"command": "echo bypassed"},
                    )
                ]
            ),
            LLMResponse(content="sub-agent done"),
            LLMResponse(content="parent done"),
        ]
    )
    agent = Agent(llm=llm, tools=[AgentTool()])

    agent.chat("delegate")

    denied_observation = llm.requests[2]["messages"][-1]
    assert denied_observation["tool_call_id"] == "forbidden-1"
    assert denied_observation["content"] == "Error: unknown tool 'bash'"


def test_agent_reports_an_internal_type_error_as_an_execution_error():
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call-error",
                        name="explode",
                        arguments={"value": "input"},
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = Agent(llm=llm, tools=[TypeErrorTool()])

    agent.chat("Exercise a failing tool")

    observation = llm.requests[1]["messages"][-1]
    assert observation["content"] == "Error executing explode: cannot process input"
    assert agent.last_tool_observations[0].status is ToolStatus.EXCEPTION


def test_agent_rejects_bad_arguments_before_executing_the_tool():
    tool = InspectTool()
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call-bad-args", name="inspect", arguments={})
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = Agent(llm=llm, tools=[tool])

    agent.chat("Call a tool with missing arguments")

    observation = llm.requests[1]["messages"][-1]
    assert observation["content"].startswith("Error: bad arguments for inspect:")
    assert tool.calls == []


def test_agent_rejects_unexpected_arguments_before_executing_the_tool():
    tool = InspectTool()
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call-extra-args",
                        name="inspect",
                        arguments={"path": "README.md", "secret": True},
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = Agent(llm=llm, tools=[tool])

    agent.chat("Call a tool with extra arguments")

    observation = llm.requests[1]["messages"][-1]
    assert observation["content"] == "Error: bad arguments for inspect: unexpected secret"
    assert tool.calls == []


def test_agent_stops_after_the_configured_tool_call_limit():
    repeated_call = lambda call_id: LLMResponse(
        tool_calls=[
            ToolCall(
                id=call_id,
                name="inspect",
                arguments={"path": "README.md"},
            )
        ]
    )
    llm = ScriptedLLM([repeated_call("call-1"), repeated_call("call-2")])
    tool = InspectTool()
    agent = Agent(llm=llm, tools=[tool], max_rounds=2)

    result = agent.chat("Keep inspecting")

    assert result == "(reached maximum tool-call rounds)"
    assert len(llm.requests) == 2
    assert len(tool.calls) == 2


def test_parallel_tool_observations_keep_the_model_call_order():
    barrier = threading.Barrier(2)
    slow = CoordinatedTool("slow", barrier, delay=0.05)
    fast = CoordinatedTool("fast", barrier, delay=0)
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call-slow", name="slow", arguments={}),
                    ToolCall(id="call-fast", name="fast", arguments={}),
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = Agent(llm=llm, tools=[slow, fast])

    agent.chat("Run both tools")

    observations = llm.requests[1]["messages"][-2:]
    assert [(item["tool_call_id"], item["content"]) for item in observations] == [
        ("call-slow", "result:slow"),
        ("call-fast", "result:fast"),
    ]


def test_agent_records_a_tool_observation_when_execution_is_interrupted():
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call-interrupt", name="interrupt", arguments={})
                ]
            )
        ]
    )
    agent = Agent(llm=llm, tools=[InterruptingTool()])

    with pytest.raises(KeyboardInterrupt):
        agent.chat("Interrupt the tool")

    assert agent.messages[-1] == {
        "role": "tool",
        "tool_call_id": "call-interrupt",
        "content": "[interrupted]",
    }
    assert agent.last_tool_observations[0].status is ToolStatus.CANCELLED


def test_agent_records_every_pending_observation_when_parallel_execution_is_interrupted():
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call-first", name="interrupt-first", arguments={}),
                    ToolCall(id="call-second", name="interrupt-second", arguments={}),
                ]
            )
        ]
    )
    agent = Agent(
        llm=llm,
        tools=[InterruptingTool("interrupt-first"), InterruptingTool("interrupt-second")],
    )

    with pytest.raises(KeyboardInterrupt):
        agent.chat("Interrupt parallel tools")

    observations = agent.messages[-2:]
    assert [(item["tool_call_id"], item["content"]) for item in observations] == [
        ("call-first", "[interrupted]"),
        ("call-second", "[interrupted]"),
    ]
