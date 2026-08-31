"""Append-only event record and deterministic summary for one Repair Run."""

from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class LedgerError(RuntimeError):
    """Raised when persisted Run Ledger events cannot be reconstructed."""


class RunLedger:
    def __init__(self, path: str | Path, *, run_id: str):
        self.path = Path(path).resolve()
        self.run_id = run_id
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(event) for event in self._events)

    def append(self, event_type: str, **payload) -> dict[str, Any]:
        reserved = {"schema_version", "sequence", "run_id", "event_type", "recorded_at"}
        overlap = reserved.intersection(payload)
        if overlap:
            raise LedgerError(f"reserved event fields: {', '.join(sorted(overlap))}")
        with self._lock:
            event = {
                "schema_version": SCHEMA_VERSION,
                "sequence": len(self._events) + 1,
                "run_id": self.run_id,
                "event_type": event_type,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                **payload,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
            self._events.append(event)
            return dict(event)

    def summary(self) -> dict[str, Any]:
        return reduce_events(self.events)

    def write_summary(self, path: str | Path) -> dict[str, Any]:
        summary = self.summary()
        Path(path).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return summary

    @classmethod
    def read(cls, path: str | Path) -> "RunLedger":
        source = Path(path).resolve()
        events = _read_jsonl(source)
        if not events:
            raise LedgerError("Run Ledger is empty")
        run_ids = {event.get("run_id") for event in events}
        if len(run_ids) != 1 or None in run_ids:
            raise LedgerError("Run Ledger must contain exactly one run_id")
        for expected, event in enumerate(events, 1):
            if event.get("schema_version") != SCHEMA_VERSION:
                raise LedgerError("unsupported Run Ledger schema version")
            if event.get("sequence") != expected:
                raise LedgerError("Run Ledger sequence is not contiguous")
            if not event.get("event_type"):
                raise LedgerError("Run Ledger event_type is missing")
        ledger = cls(source, run_id=next(iter(run_ids)))
        ledger._events = events
        return ledger


def reduce_events(events) -> dict[str, Any]:
    event_list = list(events)
    run_id = event_list[0].get("run_id") if event_list else None
    event_counts = Counter(event.get("event_type") for event in event_list)
    started = {
        (event.get("turn_id"), event.get("call_id"))
        for event in event_list
        if event.get("event_type") == "tool_started"
    }
    finished_events = [
        event for event in event_list if event.get("event_type") == "tool_finished"
    ]
    finished = {
        (event.get("turn_id"), event.get("call_id")) for event in finished_events
    }
    status_counts = Counter(event.get("status") for event in finished_events)
    status_counts.pop(None, None)
    model_turn_events = [
        event for event in event_list if event.get("event_type") == "model_turn_finished"
    ]
    finished_runs = [
        event for event in event_list if event.get("event_type") == "run_finished"
    ]
    aborted_runs = [
        event for event in event_list if event.get("event_type") == "run_aborted"
    ]
    return {
        "source": "run_ledger",
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "event_count": len(event_list),
        "event_type_counts": dict(sorted(event_counts.items())),
        "model_turns": event_counts.get("model_turn_started", 0),
        "tokens": {
            "prompt": sum(event.get("prompt_tokens", 0) or 0 for event in model_turn_events),
            "completion": sum(
                event.get("completion_tokens", 0) or 0 for event in model_turn_events
            ),
        },
        "tool_calls": {
            "started": len(started),
            "finished": len(finished),
            "open": len(started - finished),
        },
        "tool_status_counts": dict(sorted(status_counts.items())),
        "workspace_snapshots": event_counts.get("workspace_snapshot", 0),
        "compressions": event_counts.get("compression", 0),
        "aborted": event_counts.get("run_aborted", 0) > 0,
        "result": (
            aborted_runs[-1].get("reason")
            if aborted_runs
            else (finished_runs[-1].get("result") if finished_runs else None)
        ),
        "observation_unknown": False,
    }


def summarize_history(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    entries = _read_jsonl(source)
    if entries and "event_type" in entries[0]:
        return RunLedger.read(source).summary()
    tool_count = sum(entry.get("kind") == "tool" for entry in entries)
    return {
        "source": "legacy_transcript",
        "event_count": len(entries),
        "tool_calls": {"started": tool_count, "finished": 0, "open": tool_count},
        "observation_unknown": True,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise LedgerError(f"Cannot read Run Ledger: {exc}") from exc


__all__ = [
    "LedgerError",
    "RunLedger",
    "SCHEMA_VERSION",
    "reduce_events",
    "summarize_history",
]
