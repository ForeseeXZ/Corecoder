"""Planner phase — the second increment of Planner-Executor-Reviewer.

Why this exists (data-backed, see eval/BASELINE_ANALYSIS.md):
  The Reviewer increment attacked "right place, confidently wrong" but came out
  net ~0 on its own (a self-authored check shares the agent's blind spot — see
  eval/REVIEWER_EXPERIMENT.md). The Planner attacks a DIFFERENT failure mode:
    - ②+① partial (5 tasks): the Executor fixes the main file and stops while a
      SECOND required edit in another file is missed. File-level localization is
      already 100% (0/24 wrong-file), but multi-SITE completeness is not.
    - ③ non-convergence (4 sphinx tasks): the Executor thrashes for 1.4-2.4M
      tokens and never converges. A structured plan bounds exploration up front.

Mechanism:
  Before the Executor touches anything, a PLANNER sub-agent (clean context,
  STRONG model — deepseek-v4-pro) does a BOUNDED read-only investigation of the
  repo and emits a structured repair plan: the full set of files/sites that must
  change, numbered steps, and a completeness check. That plan is then handed to
  the Executor (the fast model) as its guide. Two intended effects:
    1. Completeness — the plan names EVERY site, and the Executor prompt tells it
       to address all of them (treats the "missed second file" mode head-on).
    2. Convergence — the Executor starts from a structured target instead of
       open-ended exploration (bounds the sphinx thrash).

Division of labour: PLANNER = deepseek-v4-pro (planning needs strong reasoning),
EXECUTOR = deepseek-v4-flash (fast implementation). The two run on SEPARATE LLM
objects so their token/cost accounting (priced differently) stays clean.

Exploration budget: the Planner is given READ-ONLY tools (read/grep/glob) — it
CANNOT mutate source, so there is no patch-contamination or cleanup concern, and
the benchmark red line is trivially preserved. Its exploration is bounded by a
tool-call round cap (`max_rounds`, the hard lever) plus the shared per-task
wall-clock timeout; `token_budget` is recorded/surfaced (advisory) so a runaway
planner is visible. The point is to produce a plan and hand off — not to become
a new thrash/cost sink.

Isolation red line (same as 接法 B): the Planner sees ONLY the problem_statement
and the repo source. It NEVER sees the official `test_patch`.
"""

from __future__ import annotations

import json
import re

from .agent import Agent
from .llm import LLM
from .tools import ReadFileTool, GlobTool, GrepTool

DEFAULT_PLANNER_MODEL = "deepseek-v4-pro"


def _planner_tools() -> list:
    """The Planner gets READ-ONLY tools only: it locates the sites and reasons,
    but never edits — implementing the plan is the Executor's job. No edit/write,
    no bash (can't shell out to mutate), no recursive sub-agents. This guarantees
    the Planner cannot contaminate the graded patch."""
    return [ReadFileTool(), GlobTool(), GrepTool()]


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #

PLAN_TASK = """You are the PLANNER in a Planner-Executor loop. A separate, faster Executor agent will IMPLEMENT your plan to fix a real bug in the `{repo}` repository. Your plan is the Executor's blueprint, so it must be concrete, correct, and — above all — COMPLETE.

Your current working directory is the repository checkout at the exact base commit for this issue. You have READ-ONLY tools (read, grep, glob): investigate the code, but you cannot and must not edit anything. Your only deliverable is the written plan.

# The issue to fix
{problem_statement}

# Your job
1. **Find the root cause AND every site that must change.** Multi-file bugs are common here: a correct fix often needs coordinated edits in MORE THAN ONE place — e.g. the spot that raises an error *and* the spot that should set a flag; a value producer *and* its consumer; the main logic *and* a guard/validator elsewhere. After you find the obvious site, deliberately hunt for secondary ones: grep for who else calls, guards, validates, constructs, or consumes the thing you are changing. A plan that lists only the first file is the single most common way this fails.
2. **Stay bounded.** You have a limited number of tool calls and a shared time budget — this is not an exhaustive code review. Prioritise nailing down the file/function SET and the exact change over reading everything. Once you can name the sites and the concrete change, STOP exploring and write the plan.

Red line: work ONLY from the issue text above and the repository source. NEVER look for, read, or rely on any hidden or official test files that might grade this task.

# Output format — emit your plan as a SINGLE JSON object

When you are done investigating, output your plan as ONE JSON object and nothing of substance after it. Use EXACTLY this shape:

```json
{{
  "files": [
    {{"path": "<relative/path/to/file.py>", "why": "<why this file must change>"}}
  ],
  "steps": [
    {{"target": "<file : function or area>", "change": "<the concrete edit to make>", "expected": "<the exact expected behavior / value / type after the change>"}}
  ],
  "completeness_check": "<one or two sentences: why this file set is COMPLETE — what you checked (call sites, guards/validators, producers/consumers) to be confident no other site needs editing>"
}}
```

Rules for the JSON:
- List EVERY file that must change in "files". If the fix genuinely touches only one file, still use the array with that single entry and justify it in "why".
- "steps" must be ordered and concrete; each step names its target site, the change, and the expected post-change behavior/value/type.
- Output valid JSON only (you may wrap it in a ```json code fence). Use plain double-quoted strings; do not add comments or trailing commas.
"""


PLAN_BLOCK = """

# Repair plan (produced by a planning pass — follow it)

A planning agent investigated this repository and produced the structured plan below. Treat it as your blueprint:

- Address **every file and every step** it lists. A common and costly failure is to fix the first site, feel done, and stop — while a second required edit in another file is left undone. Before you finish, confirm you have handled each FILE listed.
- Use the STEPS for the concrete change and the expected value/type at each site.
- If, while editing, you find the plan is wrong or genuinely incomplete, you may deviate — but do not *silently* skip a listed site; verify it first.

{plan}
"""


# --------------------------------------------------------------------------- #
# JSON plan parsing — the Planner emits a structured JSON object. We clean it
# (strip code fences / surrounding prose), json.loads it, and validate the shape.
# Anything that fails falls back to the legacy text path (`_parse_files`), which
# in turn falls back to baseline (plan-less) — a single bad JSON format NEVER
# crashes the task.
# --------------------------------------------------------------------------- #

def _extract_json_block(text: str) -> str | None:
    """Pull the JSON object out of a raw LLM message.

    Handles the two common wrappers: a ```json ... ``` (or bare ```) code fence,
    and leading/trailing prose around the object. Returns the substring from the
    first `{` to the last `}` (after unwrapping any fence), or None if there is
    no brace pair at all."""
    if not text:
        return None
    s = text.strip()
    # 1) if there's a fenced block, prefer its contents
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL | re.IGNORECASE)
    if fence:
        s = fence.group(1).strip()
    # 2) slice from the first { to the last } (drops surrounding prose)
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    return s[start:end + 1]


def _parse_plan_json(text: str) -> dict | None:
    """Clean + json.loads the planner output. Returns the dict, or None on any
    failure (no fence/braces, invalid JSON, or not an object)."""
    block = _extract_json_block(text)
    if not block:
        return None
    try:
        obj = json.loads(block)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _files_from_json(obj: dict) -> list[str]:
    """Ordered, de-duplicated list of file paths from the JSON `files` array.
    Tolerates both [{"path": ...}] and bare-string entries."""
    out: list[str] = []
    seen: set[str] = set()
    for f in obj.get("files") or []:
        path = f.get("path") if isinstance(f, dict) else f
        if isinstance(path, str):
            path = path.strip().strip("`")
            if path and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _render_plan_from_json(obj: dict) -> str:
    """Format the structured JSON plan into the readable FILES/STEPS/COMPLETENESS
    text the Executor already knows how to consume. The JSON is the internal
    structured representation; the Executor sees this human-readable rendering."""
    lines: list[str] = ["FILES:"]
    files = obj.get("files") or []
    if files:
        for f in files:
            if isinstance(f, dict):
                path = (f.get("path") or "?").strip()
                why = (f.get("why") or "").strip()
                lines.append(f"- {path} — {why}" if why else f"- {path}")
            else:
                lines.append(f"- {f}")
    else:
        lines.append("- (none listed)")

    lines += ["", "STEPS:"]
    steps = obj.get("steps") or []
    if steps:
        for i, s in enumerate(steps, 1):
            if isinstance(s, dict):
                target = (s.get("target") or "?").strip()
                change = (s.get("change") or "").strip()
                expected = (s.get("expected") or "").strip()
                seg = f"{i}. {target} — {change}" if change else f"{i}. {target}"
                if expected:
                    seg += f"  (expected: {expected})"
                lines.append(seg)
            else:
                lines.append(f"{i}. {s}")
    else:
        lines.append("1. (no explicit steps given)")

    cc = obj.get("completeness_check")
    if cc:
        lines += ["", "COMPLETENESS CHECK:", str(cc).strip()]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# legacy text parsing — fallback when JSON parsing fails (best-effort path
# extraction; the Executor is then handed the raw plan text verbatim).
# --------------------------------------------------------------------------- #

def _parse_files(text: str) -> list[str]:
    """Extract the file paths listed under the `FILES:` section (best-effort,
    for meta/debug). The Executor is handed the full plan text verbatim, so a
    parse miss never loses information."""
    m = re.search(r"FILES:\s*(.+?)(?:\n\s*STEPS:|\Z)", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    files: list[str] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or not line.lstrip().startswith(("-", "*")):
            continue
        body = line.lstrip("-* \t").strip("`")
        # Normally the path leads the bullet ("path.py — why"); take the first
        # token, splitting on a separator that may be attached ("path.py:" /
        # "path.py—why"). Fall back to the first path-like token anywhere on the
        # line so a prose-style bullet still yields its path (this is best-effort
        # logging only — the Executor always receives the full plan text).
        def _clean(tok: str) -> str:
            return tok.strip().strip("`").rstrip(":—–-,;")

        def _is_path(tok: str) -> bool:
            return bool(tok) and ("/" in tok or tok.endswith(".py"))

        head = _clean(re.split(r"\s+[—:–-]\s+|\s{2,}|\s+|[—:]", body, maxsplit=1)[0])
        if _is_path(head):
            files.append(head)
        else:
            for tok in re.split(r"[\s,;]+", body):
                tok = _clean(tok)
                if _is_path(tok):
                    files.append(tok)
                    break
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #

def run_plan_phase(
    *,
    config,
    repo_dir,
    repo: str,
    problem_statement: str,
    model: str = DEFAULT_PLANNER_MODEL,
    max_rounds: int = 20,
    token_budget: int = 600_000,
    max_tokens: int = 16384,
    max_context_tokens: int = 128_000,
    on_token=None,
    on_tool=None,
    log=print,
) -> tuple[str, dict]:
    """Run the bounded Planner pass and return (plan_block_for_executor, meta).

    Creates its OWN strong-model LLM (deepseek-v4-pro) so planner tokens/cost are
    accounted separately from the flash Executor. The caller is expected to be
    chdir'd into `repo_dir` already (so the read-only tools see the checkout),
    mirroring how the Reviewer loop is invoked.

    Never raises: any internal failure degrades to ("", meta-with-error) so the
    Executor simply proceeds plan-less (i.e. baseline behavior). Only a per-task
    timeout (SIGALRM) propagates, which means the whole task is out of time anyway.
    """
    meta: dict = {
        "enabled": True, "model": model, "max_rounds": max_rounds,
        "token_budget": token_budget, "planned_files": [], "n_planned_files": 0,
        "plan_text": "", "tool_calls": 0, "prompt_tokens": 0,
        "completion_tokens": 0, "tokens_spent": 0, "cost_cny": None,
        "budget_stop": False, "error": None,
        # JSON-plan instrumentation: did the planner emit schema-conformant JSON,
        # and if not, which fallback did we take? (lets us measure pro-vs-flash
        # JSON adherence). plan_json holds the parsed object for inspection.
        "json_parse_ok": False, "json_fallback": None, "plan_json": None,
    }

    planner_llm = LLM(
        model=model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=max_tokens,
    )

    n_tools = {"count": 0}

    def _on_tool(name, kwargs):
        n_tools["count"] += 1
        if on_tool:
            on_tool(name, kwargs)

    planner = Agent(
        llm=planner_llm,
        tools=_planner_tools(),
        max_context_tokens=max_context_tokens,
        max_rounds=max_rounds,
    )

    plan_text = ""
    try:
        plan_text = planner.chat(
            PLAN_TASK.format(repo=repo, problem_statement=problem_statement.strip()),
            on_token=on_token, on_tool=_on_tool,
        )
    except Exception as e:  # noqa: BLE001 — degrade to plan-less, never break the run
        # (AgentTimeoutError is a subclass of Exception but is raised by the
        #  outer SIGALRM handler; if it fires here the task is out of time. We
        #  record it and let the caller proceed — the executor will be skipped
        #  by the same alarm semantics if relevant.)
        meta["error"] = f"{type(e).__name__}: {e}"
        log(f"[plan] planner failed: {meta['error']} — proceeding without a plan")

    spent = planner_llm.total_prompt_tokens + planner_llm.total_completion_tokens

    # --- parse the JSON plan, with a graded fallback that never crashes ---
    # 1) schema-conformant JSON  -> render to readable text, files from JSON
    # 2) parseable-but-off / not-JSON -> legacy text path (raw text + regex paths)
    # 3) empty planner output     -> baseline (plan-less), handled below
    plan_obj = _parse_plan_json(plan_text)
    schema_ok = (
        isinstance(plan_obj, dict)
        and isinstance(plan_obj.get("files"), list)
        and isinstance(plan_obj.get("steps"), list)
    )
    if schema_ok:
        files = _files_from_json(plan_obj)
        rendered = _render_plan_from_json(plan_obj)
        json_parse_ok = True
        json_fallback = None
    else:
        # JSON didn't parse or didn't match the shape -> fall back to the legacy
        # text handling (best-effort path regex + hand the raw plan to Executor).
        files = _parse_files(plan_text)
        rendered = plan_text.strip()
        json_parse_ok = False
        json_fallback = "text" if plan_text.strip() else "empty"

    meta.update({
        "planned_files": files,
        "n_planned_files": len(files),
        "plan_text": plan_text,
        "plan_json": plan_obj,
        "json_parse_ok": json_parse_ok,
        "json_fallback": json_fallback,
        "tool_calls": n_tools["count"],
        "prompt_tokens": planner_llm.total_prompt_tokens,
        "completion_tokens": planner_llm.total_completion_tokens,
        "tokens_spent": spent,
        "cost_cny": planner_llm.estimated_cost,
        "budget_stop": spent > token_budget,
    })
    if meta["budget_stop"]:
        log(f"[plan] planner exceeded token budget ({spent}>{token_budget}) "
            f"(advisory — bounded by max_rounds={max_rounds})")

    if not plan_text.strip():
        return "", meta

    log(f"[plan] {len(files)} file(s) planned: {files} "
        f"(json_ok={json_parse_ok}"
        f"{'' if json_parse_ok else f'/fallback={json_fallback}'}, "
        f"{n_tools['count']} tool calls, {spent} tok)")
    return PLAN_BLOCK.format(plan=rendered), meta
