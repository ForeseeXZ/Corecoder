"""Multi-layer context compression — the third increment (graded, instrumented).

Why this exists (data-backed, see eval/BASELINE_ANALYSIS.md + dev_subset.json):
  A handful of tasks never converge because the *conversation context* balloons:
  sphinx-8548 burns 1.41M tokens and hits the round cap; sphinx-9229 burns 2.3M
  with an empty patch. Each agent round re-sends the WHOLE history, so once the
  context is fat every subsequent round pays for it — cost and the per-task
  wall-clock both blow up, and the model loses the thread in the noise.

  corecoder/context.py already ships a basic always-on `ContextManager` (light
  tool-output snipping). That stays the BASELINE path, untouched. This module is
  a switchable, *instrumented* upgrade: it applies three graded layers at
  different occupancy thresholds and records exactly what each layer reclaimed,
  so the token saving is measurable (the headline number for the résumé claim).

Three layers, lightest → heaviest, each tripped by a higher occupancy ratio
(estimated context tokens / max_context_tokens):

  Layer 1 — tool-output trim  (lightest, near-lossless)
      Verbose tool results (read/grep/bash dumps) are trimmed to head+tail with a
      placeholder for the elided middle. The most RECENT tool outputs are left
      intact (the agent is actively using them). Touches only `role:"tool"`
      message *content*, never the tool_call_id linkage.

  Layer 2 — LLM summary of early turns  (medium)
      When the early history piles up, one LLM call compresses everything except
      the most recent `keep_recent` turns into a single task-focused summary
      (files touched, findings, current plan, next step). Recent turns stay
      verbatim — recent information is the most load-bearing.

  Layer 3 — structured archive  (heaviest, last resort)
      When context still nears the ceiling, collapse to a STRUCTURED brief
      (confirmed facts / files located & edited / approaches tried / current
      state & next step) plus only the last few messages. Maximal reclaim while
      preserving the skeleton the agent needs to keep solving.

Safety:
  - Compression only ever rewrites the agent's OWN conversation history. It never
    touches files, the repo, or anything test-related — the benchmark red line is
    untouched by construction.
  - Slicing respects OpenAI tool-call pairing: the kept tail never starts on an
    orphan `tool` message whose owning assistant turn was summarized away (see
    `_safe_tail_start`).
  - The LLM summary calls run on the shared executor LLM; their token cost is
    attributed back as `overhead_tokens` so the *net* saving stays honest
    (compression is not free).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .context import estimate_tokens, _approx_tokens  # reuse the same estimator

if TYPE_CHECKING:
    from .llm import LLM


# --------------------------------------------------------------------------- #
# tunable defaults (all overridable via compress_cfg / CLI)
# --------------------------------------------------------------------------- #

DEFAULT_SNIP_AT = 0.55        # >55% occupancy -> Layer 1 (trim tool outputs)
DEFAULT_SUMMARIZE_AT = 0.72   # >72% occupancy -> Layer 2 (LLM summary)
DEFAULT_COLLAPSE_AT = 0.88    # >88% occupancy -> Layer 3 (structured archive)

DEFAULT_KEEP_RECENT = 8       # turns kept verbatim by Layer 2
DEFAULT_COLLAPSE_KEEP = 4     # turns kept verbatim by Layer 3

DEFAULT_TOOL_SNIP_CHARS = 1200  # trim tool outputs longer than this many chars
DEFAULT_TOOL_SNIP_HEAD = 8      # ...keeping this many head lines
DEFAULT_TOOL_SNIP_TAIL = 6      # ...and this many tail lines
DEFAULT_PROTECT_RECENT = 6      # never trim tool outputs in the last N messages


SUMMARY_SYS = (
    "You are compressing the EARLIER part of a coding agent's working transcript "
    "so it can keep going with less context. Produce a tight summary that "
    "preserves everything needed to CONTINUE the task and drops the noise.\n"
    "KEEP: which files/functions were located and why they matter; edits already "
    "made (file + what changed); concrete findings about the bug's root cause; "
    "the current plan and the immediate next step; errors/dead-ends hit.\n"
    "DROP: verbose command/tool output, full code listings, repeated back-and-forth.\n"
    "Be factual and specific (name files and symbols). 8-15 lines max."
)

ARCHIVE_SYS = (
    "You are doing an emergency context reset for a coding agent that is running "
    "out of room. Compress the transcript so far into a STRUCTURED brief the agent "
    "can resume from. Use EXACTLY these four sections, each terse and specific:\n"
    "CONFIRMED FACTS: <root cause / behavior established so far>\n"
    "FILES: <path — located? edited? what change was made or is intended>\n"
    "APPROACHES TRIED: <what was attempted and whether it worked / why not>\n"
    "CURRENT STATE & NEXT STEP: <where things stand and the single next action>\n"
    "Name files and symbols explicitly. Drop all raw tool output."
)


def _flatten(messages: list[dict], per_msg: int = 900, total: int = 20000) -> str:
    """Render messages to a compact text block for the summarizer LLM.

    Keeps role + (truncated) content + tool-call names so the summary can mention
    what was done, without shipping the full payloads."""
    parts: list[str] = []
    used = 0
    for m in messages:
        role = m.get("role", "?")
        text = m.get("content") or ""
        if m.get("tool_calls"):
            names = ", ".join(
                tc.get("function", {}).get("name", "?") for tc in m["tool_calls"]
            )
            text = (text + f" [tool_calls: {names}]").strip()
        if not text:
            continue
        chunk = f"[{role}] {text[:per_msg]}"
        parts.append(chunk)
        used += len(chunk)
        if used >= total:
            parts.append("[... earlier content elided ...]")
            break
    return "\n".join(parts)


def _heuristic_summary(messages: list[dict]) -> str:
    """LLM-free fallback: pull file paths + error lines so we never archive to
    nothing if the summary call fails."""
    import re
    files: set[str] = set()
    errors: list[str] = []
    for m in messages:
        text = m.get("content") or ""
        for match in re.finditer(r"[\w./\-]+\.\w{1,5}", text):
            files.add(match.group())
        for line in text.splitlines():
            if "error" in line.lower():
                errors.append(line.strip()[:150])
    out = []
    if files:
        out.append("Files seen: " + ", ".join(sorted(files)[:20]))
    if errors:
        out.append("Errors seen: " + "; ".join(errors[:5]))
    return "\n".join(out) or "(no extractable context)"


class CompressionManager:
    """Drop-in replacement for `ContextManager` with graded layers + metrics.

    Exposes the same `maybe_compress(messages, llm) -> bool` surface the agent
    loop already calls, plus a `.stats` dict the runner folds into the summary.
    """

    def __init__(
        self,
        max_tokens: int = 128_000,
        *,
        snip_at: float = DEFAULT_SNIP_AT,
        summarize_at: float = DEFAULT_SUMMARIZE_AT,
        collapse_at: float = DEFAULT_COLLAPSE_AT,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        collapse_keep: int = DEFAULT_COLLAPSE_KEEP,
        tool_snip_chars: int = DEFAULT_TOOL_SNIP_CHARS,
        tool_snip_head: int = DEFAULT_TOOL_SNIP_HEAD,
        tool_snip_tail: int = DEFAULT_TOOL_SNIP_TAIL,
        protect_recent: int = DEFAULT_PROTECT_RECENT,
        log=None,
    ):
        self.max_tokens = max_tokens
        self.snip_at = snip_at
        self.summarize_at = summarize_at
        self.collapse_at = collapse_at
        self.keep_recent = keep_recent
        self.collapse_keep = collapse_keep
        self.tool_snip_chars = tool_snip_chars
        self.tool_snip_head = tool_snip_head
        self.tool_snip_tail = tool_snip_tail
        self.protect_recent = protect_recent
        self._log = log

        # absolute trip points (tokens)
        self._snip_tok = int(max_tokens * snip_at)
        self._summarize_tok = int(max_tokens * summarize_at)
        self._collapse_tok = int(max_tokens * collapse_at)

        self.stats: dict = {
            "enabled": True,
            "max_tokens": max_tokens,
            "thresholds": {
                "snip_at": snip_at,
                "summarize_at": summarize_at,
                "collapse_at": collapse_at,
            },
            "keep_recent": keep_recent,
            "collapse_keep": collapse_keep,
            "events": [],          # per-trigger {layer, tokens_before/after, reclaimed, ...}
            "layer_counts": {"1_tool_snip": 0, "2_summarize": 0, "3_archive": 0},
            "total_reclaimed_tokens": 0,   # sum of (before-after) across all events
            "overhead_tokens": 0,          # tokens the summary/archive LLM calls cost
            "overhead_llm_calls": 0,
            "peak_tokens": 0,              # highest occupancy observed
            "final_tokens": 0,            # occupancy at end of run
        }

    # ------------------------------------------------------------------ #
    # main entry — same signature the agent loop already calls
    # ------------------------------------------------------------------ #
    def maybe_compress(self, messages: list[dict], llm: "LLM | None" = None) -> bool:
        current = estimate_tokens(messages)
        if current > self.stats["peak_tokens"]:
            self.stats["peak_tokens"] = current
        compressed = False

        # Layer 1 — trim verbose tool outputs (near-lossless)
        if current > self._snip_tok:
            before, nb = current, len(messages)
            if self._snip_tool_outputs(messages):
                current = estimate_tokens(messages)
                self._record("1_tool_snip", before, current, nb, len(messages))
                compressed = True

        # Layer 2 — LLM summary of early turns
        if current > self._summarize_tok and len(messages) > self.keep_recent + 2:
            before, nb = current, len(messages)
            if self._summarize_old(messages, llm):
                current = estimate_tokens(messages)
                self._record("2_summarize", before, current, nb, len(messages))
                compressed = True

        # Layer 3 — structured archive (last resort)
        if current > self._collapse_tok and len(messages) > self.collapse_keep + 1:
            before, nb = current, len(messages)
            self._archive(messages, llm)
            current = estimate_tokens(messages)
            self._record("3_archive", before, current, nb, len(messages))
            compressed = True

        self.stats["final_tokens"] = current
        return compressed

    # ------------------------------------------------------------------ #
    # bookkeeping
    # ------------------------------------------------------------------ #
    def _record(self, layer: str, before: int, after: int, nb: int, na: int):
        reclaimed = max(0, before - after)
        self.stats["layer_counts"][layer] += 1
        self.stats["total_reclaimed_tokens"] += reclaimed
        self.stats["events"].append({
            "layer": layer,
            "ratio_before": round(before / self.max_tokens, 3),
            "tokens_before": before,
            "tokens_after": after,
            "reclaimed": reclaimed,
            "n_messages_before": nb,
            "n_messages_after": na,
        })
        if self._log:
            self._log(f"[compress] {layer}: {before}->{after} tok "
                      f"(-{reclaimed}), msgs {nb}->{na}")

    # ------------------------------------------------------------------ #
    # Layer 1 — tool-output trimming
    # ------------------------------------------------------------------ #
    def _snip_tool_outputs(self, messages: list[dict]) -> bool:
        """Trim oversized tool results to head+tail. Protects the last
        `protect_recent` messages so the agent's freshest reads stay whole."""
        changed = False
        cutoff = len(messages) - self.protect_recent
        for i, m in enumerate(messages):
            if i >= cutoff:                       # leave recent outputs intact
                break
            if m.get("role") != "tool":
                continue
            content = m.get("content") or ""
            if len(content) <= self.tool_snip_chars:
                continue
            lines = content.splitlines()
            if len(lines) <= self.tool_snip_head + self.tool_snip_tail:
                continue
            snipped = (
                "\n".join(lines[: self.tool_snip_head])
                + f"\n... ({len(lines)} lines, "
                  f"{len(content)} chars — trimmed to save context) ...\n"
                + "\n".join(lines[-self.tool_snip_tail:])
            )
            m["content"] = snipped
            changed = True
        return changed

    # ------------------------------------------------------------------ #
    # Layer 2 — summarize early turns, keep recent verbatim
    # ------------------------------------------------------------------ #
    def _summarize_old(self, messages: list[dict], llm: "LLM | None") -> bool:
        k = self._safe_tail_start(messages, self.keep_recent)
        if k <= 1:                       # not enough early history to bother
            return False
        old = messages[:k]
        tail = messages[k:]
        summary = self._llm_summary(old, llm, SUMMARY_SYS)
        if not summary.strip():
            return False
        new = [
            {"role": "user",
             "content": f"[Earlier conversation compressed to a summary]\n{summary}"},
            {"role": "assistant",
             "content": "Understood — I have the key context from the earlier "
                        "steps and will continue the task from here."},
        ]
        new.extend(tail)
        messages.clear()
        messages.extend(new)
        return True

    # ------------------------------------------------------------------ #
    # Layer 3 — structured archive, keep only the last few turns
    # ------------------------------------------------------------------ #
    def _archive(self, messages: list[dict], llm: "LLM | None"):
        k = self._safe_tail_start(messages, self.collapse_keep)
        if k <= 0:
            k = max(1, len(messages) - self.collapse_keep)
        old = messages[:k]
        tail = messages[k:]
        brief = self._llm_summary(old, llm, ARCHIVE_SYS)
        if not brief.strip():
            brief = _heuristic_summary(old)
        new = [
            {"role": "user",
             "content": f"[Context archived — structured brief of the work so far]\n{brief}"},
            {"role": "assistant",
             "content": "Context restored from the brief. Continuing the task."},
        ]
        new.extend(tail)
        messages.clear()
        messages.extend(new)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _safe_tail_start(self, messages: list[dict], want_keep: int) -> int:
        """Index where the kept tail should begin so we never split a tool-call
        group. The OpenAI schema requires an assistant message that carries
        `tool_calls` to be immediately followed by the matching `tool` results;
        starting the tail on an orphan `tool` message (whose assistant turn we
        just summarized away) is an API error. So we walk the cut point back to a
        round boundary — an assistant message — which always sits behind its own
        tool replies and behind the previous round's completed group.
        """
        n = len(messages)
        target = n - want_keep
        if target < 1:
            return target
        # never start the tail on a `tool` message — back up to its assistant turn
        while target > 1 and messages[target].get("role") == "tool":
            target -= 1
        return target

    def _llm_summary(self, msgs: list[dict], llm: "LLM | None", system: str) -> str:
        """One compression call on the shared executor LLM. Its token cost is
        attributed to `overhead_tokens` so net savings stay honest. Degrades to a
        heuristic extraction if there's no LLM or the call fails."""
        if llm is None:
            return _heuristic_summary(msgs)
        flat = _flatten(msgs)
        before = llm.total_prompt_tokens + llm.total_completion_tokens
        try:
            resp = llm.chat(messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": flat},
            ])
            text = resp.content or ""
        except Exception:                       # noqa: BLE001 — never break the run
            text = _heuristic_summary(msgs)
        after = llm.total_prompt_tokens + llm.total_completion_tokens
        self.stats["overhead_tokens"] += max(0, after - before)
        self.stats["overhead_llm_calls"] += 1
        return text
