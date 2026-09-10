"""Deterministic no-progress detection for tool calls in one Repair Run."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .tools.base import ToolEffect, ToolStatus


class LoopAction(str, Enum):
    ALLOW = "allow"
    REPLAY = "replay"
    BLOCK = "block"
    STALL = "stall"


@dataclass(frozen=True, slots=True)
class LoopDecision:
    action: LoopAction
    key: tuple[str, str] | None
    reason: str = ""
    previous: Any | None = None


@dataclass(slots=True)
class _CallRecord:
    observation: Any
    repeat_count: int = 0
    transient_retries: int = 0


class ToolLoopGuard:
    """Classify exact repeats and short cycles without throwing exceptions."""

    def __init__(self, *, history_size: int = 12):
        self._history_size = history_size
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._records: dict[tuple[str, str], _CallRecord] = {}
            self._history: deque[tuple[str, str]] = deque(maxlen=self._history_size)
            self._recovery_pending = False

    def decide(
        self,
        *,
        arguments_sha256: str,
        state_sha256: str | None,
        effect: ToolEffect,
    ) -> LoopDecision:
        if state_sha256 is None:
            return LoopDecision(LoopAction.ALLOW, None)

        key = (arguments_sha256, state_sha256)
        with self._lock:
            prospective = [*self._history, key]
            short_cycle = _has_short_cycle(prospective)
            previous_record = self._records.get(key)

            if self._recovery_pending:
                if previous_record is not None or short_cycle:
                    self._history.append(key)
                    return LoopDecision(
                        LoopAction.STALL,
                        key,
                        "the model repeated a no-progress call after recovery guidance",
                        previous_record.observation if previous_record else None,
                    )
                self._recovery_pending = False

            if short_cycle:
                self._history.append(key)
                self._recovery_pending = True
                return LoopDecision(
                    LoopAction.BLOCK,
                    key,
                    "a repeated tool-call cycle was detected with no workspace change",
                    previous_record.observation if previous_record else None,
                )

            self._history.append(key)
            if previous_record is None:
                return LoopDecision(LoopAction.ALLOW, key)

            if (
                previous_record.observation.status
                in {ToolStatus.TIMEOUT, ToolStatus.EXCEPTION}
                and previous_record.transient_retries < 1
            ):
                previous_record.transient_retries += 1
                return LoopDecision(
                    LoopAction.ALLOW,
                    key,
                    "one retry is allowed for a transient tool failure",
                    previous_record.observation,
                )

            previous_record.repeat_count += 1
            if effect is ToolEffect.READ and previous_record.repeat_count == 1:
                return LoopDecision(
                    LoopAction.REPLAY,
                    key,
                    "identical read call under an unchanged workspace",
                    previous_record.observation,
                )

            self._recovery_pending = True
            return LoopDecision(
                LoopAction.BLOCK,
                key,
                "identical tool call under an unchanged workspace",
                previous_record.observation,
            )

    def remember(self, decision: LoopDecision, observation: Any) -> None:
        if decision.action is not LoopAction.ALLOW or decision.key is None:
            return
        with self._lock:
            existing = self._records.get(decision.key)
            if existing is None:
                self._records[decision.key] = _CallRecord(observation=observation)
            else:
                existing.observation = observation


def _has_short_cycle(items: list[tuple[str, str]]) -> bool:
    """Detect repeated suffixes such as A-B-A-B, excluding simple A-A repeats."""
    for period in (2, 3):
        if len(items) >= period * 2:
            if items[-period:] == items[-2 * period : -period]:
                return True
    return False


__all__ = ["LoopAction", "LoopDecision", "ToolLoopGuard"]
