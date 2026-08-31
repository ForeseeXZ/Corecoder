"""Behavioral contracts for structured tool execution."""

import sys
import threading
import hashlib

from corecoder.llm import ToolCall
from corecoder.ledger import RunLedger
from corecoder.runtime import (
    ToolEffect,
    ToolExecutionResult,
    ToolRuntime,
    ToolStatus,
)
from corecoder.tools.base import Tool
from corecoder.tools.bash import BashTool


class ResultTool(Tool):
    name = "result"
    description = "Return a caller-selected structured result."
    effect = ToolEffect.PROCESS
    parameters = {
        "type": "object",
        "properties": {"kind": {"type": "string"}},
        "required": ["kind"],
    }

    def execute(self, kind: str) -> str:
        return self.execute_structured(kind=kind).content

    def execute_structured(self, kind: str) -> ToolExecutionResult:
        if kind == "non-zero":
            return ToolExecutionResult(
                status=ToolStatus.NON_ZERO,
                content="command failed",
                exit_code=7,
            )
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content="ok")


class BarrierReadTool(Tool):
    description = "Prove that read calls overlap."
    effect = ToolEffect.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, barrier: threading.Barrier):
        self.name = name
        self.barrier = barrier

    def execute(self) -> str:
        self.barrier.wait(timeout=2)
        return self.name


class GuardedWriteTool(Tool):
    description = "Detect overlapping mutations."
    effect = ToolEffect.WRITE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, state: dict, lock: threading.Lock):
        self.name = name
        self.state = state
        self.lock = lock

    def execute(self) -> str:
        with self.lock:
            if self.state["active"]:
                self.state["overlapped"] = True
            self.state["active"] = True
        with self.lock:
            self.state["order"].append(self.name)
            self.state["active"] = False
        return self.name


class OrderedReadTool(Tool):
    description = "Finish in a controlled order without timing assumptions."
    effect = ToolEffect.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(
        self,
        name: str,
        barrier: threading.Barrier,
        release_slow: threading.Event,
        slow: bool,
    ):
        self.name = name
        self.barrier = barrier
        self.release_slow = release_slow
        self.slow = slow

    def execute(self) -> str:
        self.barrier.wait(timeout=2)
        if self.slow:
            assert self.release_slow.wait(timeout=2)
        return self.name


class SignalingLedger(RunLedger):
    def __init__(self, path, *, run_id: str, fast_finished: threading.Event):
        super().__init__(path, run_id=run_id)
        self.fast_finished = fast_finished

    def append(self, event_type: str, **payload):
        event = super().append(event_type, **payload)
        if event_type == "tool_finished" and payload.get("call_id") == "call-fast":
            self.fast_finished.set()
        return event


def test_runtime_returns_a_structured_observation_without_parsing_result_text():
    runtime = ToolRuntime([ResultTool()])

    observation = runtime.execute(
        ToolCall(id="call-1", name="result", arguments={"kind": "non-zero"})
    )

    assert observation.call_id == "call-1"
    assert observation.name == "result"
    assert observation.effect is ToolEffect.PROCESS
    assert observation.status is ToolStatus.NON_ZERO
    assert observation.exit_code == 7
    assert observation.model_text == "command failed"
    assert len(observation.arguments_sha256) == 64
    assert len(observation.output_sha256) == 64


def test_runtime_rejects_bad_arguments_before_the_tool_runs():
    runtime = ToolRuntime([ResultTool()])

    observation = runtime.execute(
        ToolCall(id="call-bad", name="result", arguments={})
    )

    assert observation.status is ToolStatus.BAD_ARGUMENTS
    assert "missing kind" in observation.model_text


def test_runtime_keeps_malformed_argument_evidence_without_executing():
    call = ToolCall(
        id="broken",
        name="result",
        arguments={},
        parse_error="malformed JSON arguments",
        raw_arguments_sha256="a" * 64,
        raw_arguments_preview='{"kind":',
    )

    observation = ToolRuntime([ResultTool()]).execute(call)

    assert observation.status is ToolStatus.BAD_ARGUMENTS
    assert observation.raw_arguments_sha256 == "a" * 64
    assert observation.raw_arguments_preview == '{"kind":'


def test_bash_reports_blocked_non_zero_and_timeout_as_data():
    runtime = ToolRuntime([BashTool()])
    calls = [
        ToolCall(id="blocked", name="bash", arguments={"command": "rm -rf /"}),
        ToolCall(
            id="non-zero",
            name="bash",
            arguments={
                "command": f'"{sys.executable}" -c "import sys;sys.exit(7)"'
            },
        ),
        ToolCall(
            id="timeout",
            name="bash",
            arguments={
                "command": f'"{sys.executable}" -c "import time;time.sleep(2)"',
                "timeout": 0.01,
            },
        ),
    ]

    observations = runtime.execute_many(calls)

    assert [item.status for item in observations] == [
        ToolStatus.BLOCKED,
        ToolStatus.NON_ZERO,
        ToolStatus.TIMEOUT,
    ]
    assert observations[1].exit_code == 7


def test_runtime_runs_reads_concurrently_but_serializes_writes():
    barrier = threading.Barrier(2)
    state = {"active": False, "overlapped": False, "order": []}
    lock = threading.Lock()
    tools = [
        BarrierReadTool("read-a", barrier),
        BarrierReadTool("read-b", barrier),
        GuardedWriteTool("write-a", state, lock),
        GuardedWriteTool("write-b", state, lock),
    ]
    runtime = ToolRuntime(tools)
    calls = [ToolCall(id=tool.name, name=tool.name, arguments={}) for tool in tools]

    observations = runtime.execute_many(calls)

    assert [item.call_id for item in observations] == [tool.name for tool in tools]
    assert state == {
        "active": False,
        "overlapped": False,
        "order": ["write-a", "write-b"],
    }
    read_observations = observations[:2]
    assert {item.finished_order for item in read_observations} == {1, 2}


def test_runtime_keeps_large_output_in_a_verifiable_artifact(tmp_path):
    ledger = RunLedger(tmp_path / "run-ledger.jsonl", run_id="large-run")
    runtime = ToolRuntime(
        [ResultTool()],
        artifact_dir=tmp_path / "artifacts",
        max_inline_chars=10,
        ledger=ledger,
    )
    tool = runtime._tools["result"]
    tool.execute_structured = lambda kind: ToolExecutionResult(  # system boundary fake
        ToolStatus.SUCCESS, "中文" * 20
    )

    observation = runtime.execute(
        ToolCall(id="large/output", name="result", arguments={"kind": "large"})
    )

    assert observation.status is ToolStatus.TRUNCATED
    assert observation.original_status is ToolStatus.SUCCESS
    assert observation.artifact is not None
    artifact = tmp_path / observation.artifact.path
    content = artifact.read_text(encoding="utf-8")
    assert content == "中文" * 20
    assert observation.output_sha256 == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    assert observation.artifact.sha256 == observation.output_sha256
    finished = [
        event for event in ledger.events if event["event_type"] == "tool_finished"
    ][0]
    assert finished["artifact"]["path"] == observation.artifact.path


def test_parallel_observations_keep_schedule_and_completion_order_reconstructable(
    tmp_path,
):
    barrier = threading.Barrier(2)
    release_slow = threading.Event()
    slow = OrderedReadTool("slow", barrier, release_slow, slow=True)
    fast = OrderedReadTool("fast", barrier, release_slow, slow=False)
    ledger = SignalingLedger(
        tmp_path / "run-ledger.jsonl",
        run_id="parallel-run",
        fast_finished=release_slow,
    )
    runtime = ToolRuntime([slow, fast], ledger=ledger)

    observations = runtime.execute_many(
        [
            ToolCall(id="call-slow", name="slow", arguments={}),
            ToolCall(id="call-fast", name="fast", arguments={}),
        ],
        turn_id="turn-1",
    )

    assert [item.call_id for item in observations] == ["call-slow", "call-fast"]
    assert [item.started_order for item in observations] == [1, 2]
    assert observations[1].finished_order < observations[0].finished_order
    finished = {
        event["call_id"]: event
        for event in ledger.events
        if event["event_type"] == "tool_finished"
    }
    assert finished["call-slow"]["finished_order"] > finished["call-fast"][
        "finished_order"
    ]
