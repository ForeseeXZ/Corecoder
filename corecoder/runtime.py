"""Structured execution boundary for tools used during a Repair Run."""

from __future__ import annotations

import concurrent.futures
import hashlib
import inspect
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .tools.base import Tool, ToolEffect, ToolExecutionResult, ToolStatus


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    path: str
    sha256: str
    length: int


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """Stable facts produced for one tool call."""

    call_id: str
    name: str
    effect: ToolEffect
    status: ToolStatus
    arguments_sha256: str
    model_text: str
    output_sha256: str
    output_length: int
    started_order: int
    finished_order: int
    duration_ms: int
    exit_code: int | None = None
    error: str | None = None
    artifact: ArtifactReference | None = None
    original_status: ToolStatus | None = None
    raw_arguments_sha256: str | None = None
    raw_arguments_preview: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["effect"] = self.effect.value
        data["status"] = self.status.value
        if self.original_status is not None:
            data["original_status"] = self.original_status.value
        return data


class ToolRuntime:
    """Validate, schedule, classify, and persist tool calls."""

    def __init__(
        self,
        tools: list[Tool],
        *,
        artifact_dir: str | Path | None = None,
        max_inline_chars: int = 15_000,
        max_workers: int = 8,
        ledger=None,
        phase: str = "executor",
    ):
        self._tools = {tool.name: tool for tool in tools}
        self.artifact_dir = Path(artifact_dir).resolve() if artifact_dir else None
        self.max_inline_chars = max_inline_chars
        self.max_workers = max_workers
        self.ledger = ledger
        self.phase = phase
        self._counter_lock = threading.Lock()
        self._started_counter = 0
        self._finished_counter = 0

    def execute(
        self,
        call,
        *,
        turn_id: str | None = None,
        started_order: int | None = None,
    ) -> ToolObservation:
        started_order = started_order or self._next_started()
        started_at = time.monotonic()
        tool = self._tools.get(call.name)
        effect = tool.effect if tool is not None else ToolEffect.READ
        arguments_hash = _json_hash(getattr(call, "arguments", {}))
        self._record(
            "tool_started",
            turn_id=turn_id,
            call_id=call.id,
            name=call.name,
            effect=effect.value,
            arguments_sha256=arguments_hash,
            started_order=started_order,
        )

        invalid = self._validate(call, tool)
        if invalid is not None:
            result = ToolExecutionResult(ToolStatus.BAD_ARGUMENTS, invalid, error=invalid)
        else:
            try:
                structured = getattr(tool, "execute_structured", None)
                if structured is not None:
                    result = structured(**call.arguments)
                else:
                    result = ToolExecutionResult(
                        ToolStatus.SUCCESS,
                        tool.execute(**call.arguments),
                    )
            except KeyboardInterrupt:
                result = ToolExecutionResult(
                    ToolStatus.CANCELLED,
                    "[interrupted]",
                    error="tool execution interrupted",
                )
            except PermissionError as exc:
                message = f"Blocked: {exc}"
                result = ToolExecutionResult(
                    ToolStatus.BLOCKED,
                    message,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                message = f"Error executing {call.name}: {exc}"
                result = ToolExecutionResult(
                    ToolStatus.EXCEPTION,
                    message,
                    error=f"{type(exc).__name__}: {exc}",
                )

        observation = self._observation(
            call=call,
            effect=effect,
            result=result,
            arguments_hash=arguments_hash,
            started_order=started_order,
            started_at=started_at,
        )
        self._record("tool_finished", turn_id=turn_id, **observation.as_dict())
        return observation

    def execute_many(self, calls, *, turn_id: str | None = None) -> list[ToolObservation]:
        """Run contiguous read groups concurrently and all mutations serially."""
        observations: list[ToolObservation | None] = [None] * len(calls)
        started_orders = [self._next_started() for _ in calls]
        index = 0
        while index < len(calls):
            if self._effect_for(calls[index]) is ToolEffect.READ:
                end = index + 1
                while end < len(calls) and self._effect_for(calls[end]) is ToolEffect.READ:
                    end += 1
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(self.max_workers, end - index)
                ) as pool:
                    futures = {
                        pool.submit(
                            self.execute,
                            calls[pos],
                            turn_id=turn_id,
                            started_order=started_orders[pos],
                        ): pos
                        for pos in range(index, end)
                    }
                    for future, pos in futures.items():
                        observations[pos] = future.result()
                index = end
            else:
                observations[index] = self.execute(
                    calls[index],
                    turn_id=turn_id,
                    started_order=started_orders[index],
                )
                index += 1
        return [item for item in observations if item is not None]

    def _validate(self, call, tool: Tool | None) -> str | None:
        parse_error = getattr(call, "parse_error", None)
        if parse_error:
            return f"Error: bad arguments for {call.name}: {parse_error}"
        if tool is None:
            return f"Error: unknown tool '{call.name}'"
        parameters = tool.parameters or {}
        required = set(parameters.get("required", []))
        provided = set(call.arguments)
        missing = sorted(required - provided)
        unexpected = sorted(provided - set(parameters.get("properties", {})))
        if missing:
            return f"Error: bad arguments for {call.name}: missing {', '.join(missing)}"
        if unexpected:
            return (
                f"Error: bad arguments for {call.name}: "
                f"unexpected {', '.join(unexpected)}"
            )
        try:
            implementation = getattr(tool, "execute_structured", tool.execute)
            inspect.signature(implementation).bind(**call.arguments)
        except TypeError as exc:
            return f"Error: bad arguments for {call.name}: {exc}"
        return None

    def _observation(
        self,
        *,
        call,
        effect: ToolEffect,
        result: ToolExecutionResult,
        arguments_hash: str,
        started_order: int,
        started_at: float,
    ) -> ToolObservation:
        full_text = result.content or "(no output)"
        output_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
        artifact = None
        status = result.status
        original_status = None
        model_text = full_text
        if len(full_text) > self.max_inline_chars:
            artifact = self._write_artifact(call.id, full_text, output_hash)
            model_text = (
                full_text[:6000]
                + f"\n\n... truncated ({len(full_text)} chars total; "
                + f"artifact={artifact.path if artifact else 'unavailable'}) ...\n\n"
                + full_text[-3000:]
            )
            original_status = status
            status = ToolStatus.TRUNCATED

        return ToolObservation(
            call_id=call.id,
            name=call.name,
            effect=effect,
            status=status,
            arguments_sha256=arguments_hash,
            model_text=model_text,
            output_sha256=output_hash,
            output_length=len(full_text),
            started_order=started_order,
            finished_order=self._next_finished(),
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            exit_code=result.exit_code,
            error=result.error,
            artifact=artifact,
            original_status=original_status,
            raw_arguments_sha256=getattr(call, "raw_arguments_sha256", None),
            raw_arguments_preview=getattr(call, "raw_arguments_preview", None),
        )

    def _write_artifact(
        self, call_id: str, content: str, output_hash: str
    ) -> ArtifactReference | None:
        if self.artifact_dir is None:
            return None
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        safe_call_id = "".join(
            char if char.isalnum() or char in "._-" else "-" for char in call_id
        )[:80] or "call"
        path = self.artifact_dir / f"{safe_call_id}-{output_hash[:12]}.txt"
        path.write_text(content, encoding="utf-8", newline="\n")
        reference_path = path.name
        if self.ledger is not None:
            try:
                reference_path = path.relative_to(self.ledger.path.parent).as_posix()
            except ValueError:
                reference_path = str(path)
        return ArtifactReference(
            path=reference_path,
            sha256=output_hash,
            length=len(content),
        )

    def _effect_for(self, call) -> ToolEffect:
        tool = self._tools.get(call.name)
        return tool.effect if tool is not None else ToolEffect.READ

    def _next_started(self) -> int:
        with self._counter_lock:
            self._started_counter += 1
            return self._started_counter

    def _next_finished(self) -> int:
        with self._counter_lock:
            self._finished_counter += 1
            return self._finished_counter

    def _record(self, event_type: str, **payload) -> None:
        if self.ledger is not None:
            payload.setdefault("phase", self.phase)
            self.ledger.append(event_type, **payload)


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "ArtifactReference",
    "ToolEffect",
    "ToolExecutionResult",
    "ToolObservation",
    "ToolRuntime",
    "ToolStatus",
]
