"""Integration contracts for the append-only Repair Run ledger."""

import json
import subprocess
import pytest

from corecoder import Agent
from corecoder.ledger import RunLedger, summarize_history
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools.base import Tool
from corecoder.workspace import WorkspaceExecution
from tests.fakes import ScriptedLLM


class EchoTool(Tool):
    name = "echo"
    description = "Return one deterministic value."
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def execute(self, value: str) -> str:
        return value


class CancelTool(Tool):
    name = "cancel"
    description = "Cancel a controlled tool call."
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self) -> str:
        raise KeyboardInterrupt


def _workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"], check=True
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "base"], check=True
    )
    return WorkspaceExecution.resolve(repo)


def test_run_ledger_round_trips_events_and_reduces_deterministically(tmp_path):
    path = tmp_path / "run-ledger.jsonl"
    ledger = RunLedger(path, run_id="run-1")
    ledger.append("run_started", baseline_commit="abc")
    ledger.append("model_turn_started", turn_id="turn-1")
    ledger.append(
        "tool_started", turn_id="turn-1", call_id="call-1", name="read_file"
    )
    ledger.append(
        "tool_finished",
        turn_id="turn-1",
        call_id="call-1",
        name="read_file",
        status="success",
    )
    ledger.append("workspace_snapshot", snapshot_hash="def")
    ledger.append("run_finished", result="model_turn_complete")

    first = ledger.summary()
    second = RunLedger.read(path).summary()

    assert first == second
    assert first["run_id"] == "run-1"
    assert first["event_count"] == 6
    assert first["tool_calls"] == {"started": 1, "finished": 1, "open": 0}
    assert first["tool_status_counts"] == {"success": 1}
    assert first["workspace_snapshots"] == 1
    assert [event["sequence"] for event in ledger.events] == list(range(1, 7))


def test_legacy_transcript_is_read_only_compatible_and_marks_unknown_observations(
    tmp_path,
):
    path = tmp_path / "transcript.jsonl"
    entries = [
        {"kind": "tool", "name": "read_file", "args": {"file_path": "a.py"}},
        {"kind": "token", "text": "done"},
    ]
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in entries), encoding="utf-8"
    )

    summary = summarize_history(path)

    assert summary["source"] == "legacy_transcript"
    assert summary["tool_calls"] == {"started": 1, "finished": 0, "open": 1}
    assert summary["observation_unknown"] is True


def test_run_ledger_summarizes_loop_guard_interventions(tmp_path):
    ledger = RunLedger(tmp_path / "guard-ledger.jsonl", run_id="guard-run")
    ledger.append(
        "tool_call_suppressed",
        call_id="call-2",
        action="block",
        reason="duplicate",
    )
    ledger.append(
        "tool_call_suppressed",
        call_id="call-3",
        action="stall",
        reason="no progress",
    )
    ledger.append("run_finished", result="no_progress")

    assert ledger.summary()["loop_guard"] == {
        "suppressed": 2,
        "actions": {"block": 1, "stall": 1},
    }


def test_agent_records_a_complete_offline_repair_run_from_ledger_events(tmp_path):
    workspace = _workspace(tmp_path)
    ledger = RunLedger(tmp_path / "run-ledger.jsonl", run_id="repair-1")
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call-1", name="echo", arguments={"value": "seen"})
                ]
            ),
            LLMResponse(content="finished"),
        ]
    )
    agent = Agent(
        llm=llm,
        tools=[EchoTool()],
        workspace=workspace,
        ledger=ledger,
        run_id="repair-1",
    )

    result = agent.chat("perform one offline Repair Run")
    summary = ledger.summary()

    assert result == "finished"
    assert summary["model_turns"] == 2
    assert summary["tool_calls"] == {"started": 1, "finished": 1, "open": 0}
    assert summary["tool_status_counts"] == {"success": 1}
    assert summary["workspace_snapshots"] == 1
    assert summary["aborted"] is False
    assert ledger.events[0]["event_type"] == "run_started"
    assert ledger.events[-1]["event_type"] == "run_finished"


def test_cancelled_tool_is_closed_and_the_repair_run_is_explicitly_aborted(tmp_path):
    ledger = RunLedger(tmp_path / "run-ledger.jsonl", run_id="cancelled-run")
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="cancel-1", name="cancel", arguments={})]
            )
        ]
    )
    agent = Agent(llm=llm, tools=[CancelTool()], ledger=ledger, run_id="cancelled-run")

    with pytest.raises(KeyboardInterrupt):
        agent.chat("cancel")

    summary = ledger.summary()
    assert summary["tool_calls"] == {"started": 1, "finished": 1, "open": 0}
    assert summary["tool_status_counts"] == {"cancelled": 1}
    assert summary["aborted"] is True
    assert summary["result"] == "cancelled"
