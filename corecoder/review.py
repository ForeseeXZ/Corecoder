"""Reviewer self-check loop — the Reviewer-first increment of Planner-Executor-Reviewer.

Why this exists (data-backed, see eval/BASELINE_ANALYSIS.md):
  83% of SWE-bench Mini baseline failures are "right place, confidently wrong,
  ZERO self-check": the Executor edits the correct file, declares done, and
  nothing ever re-examines the patch before stopping. This module inserts that
  missing step.

Mechanism (two signals, combined):
  1. PRIMARY — self-written verification, run with a deterministic before/after.
     A Reviewer sub-agent (clean context, isolated from the Executor's
     self-confirmation bias) writes an executable `.cc_verify/verify.sh` that
     exercises the exact behavior in the issue and exits 0 iff the bug is fixed.
     The ORCHESTRATOR (this module, in Python — not the LLM) then runs it twice:
       - AFTER  : on the patched working tree            (expect exit 0)
       - BEFORE : `git stash` the patch, run again, pop  (expect exit != 0)
     before-fail & after-pass  -> STRONG_PASS  (the patch fixes a *reproduced* bug)
     after-fail                -> AFTER_FAIL   (patch does not fix -> must revise)
     The risky git ops live in Python, never in the model, so the working tree
     can't be corrupted and the diff stays clean.
  2. AUXILIARY — reasoning critique. The same Reviewer judges root-cause vs
     symptom, multi-file completeness, and edge/value/type correctness, emitting
     `VERDICT: PASS|REVISE` + a concrete `CRITIQUE:` for the Executor.

Loop: review -> (revise via the Executor) -> review, at most `max_rounds`
revisions. PASS exits early. A token budget on the whole stage prevents the
review loop from becoming a new cost sink.

Isolation red line (same as 接法 B): the Reviewer and its self-written script
see ONLY the problem_statement and the repo source. They NEVER see the official
`test_patch`. That is the credibility line for the whole benchmark.

Patch hygiene: the Reviewer writes scratch files (verify.sh, helpers). Any file
the *Reviewer* creates is tracked per-call (untracked delta around each reviewer
run) and removed at the end, plus `.cc_verify/` is deleted wholesale — so the
extracted model_patch contains only real source edits, never a verification
artifact. Files the *Executor* created (a legit new source file) are preserved.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .agent import Agent
from .tools import BashTool, ReadFileTool, WriteFileTool, GlobTool, GrepTool
from .workspace import WorkspaceExecution

SCRATCH = ".cc_verify"
VERIFY_ENTRY = f"{SCRATCH}/verify.sh"


# --------------------------------------------------------------------------- #
# git / filesystem helpers (all the side-effectful, risky ops live here)
# --------------------------------------------------------------------------- #

def _git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )


def _untracked(repo: Path) -> set[str]:
    """Non-ignored untracked paths (git already excludes .gitignored files,
    so pycache/build noise never shows up here and never contaminates a patch)."""
    out = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    return {ln[3:] for ln in out.splitlines() if ln.startswith("?? ")}


def _has_tracked_changes(repo: Path) -> bool:
    return _git(repo, "diff", "--quiet").returncode != 0


def _working_diff(repo: Path) -> str:
    """Executor's edits as a diff against the index/HEAD (tracked files only).
    Shown to the Reviewer; final grading patch is extracted later by the caller."""
    return _git(repo, "-c", "core.fileMode=false", "diff").stdout


def _reviewer_tools() -> list:
    """Reviewer can read/search the code, write its scratch script, and run it.
    It deliberately gets NO edit_file (revising source is the Executor's job) and
    NO agent (no recursive sub-agents)."""
    return [BashTool(), ReadFileTool(), WriteFileTool(), GlobTool(), GrepTool()]


# --------------------------------------------------------------------------- #
# verify-script execution + before/after classification
# --------------------------------------------------------------------------- #

def _run_verify(repo: Path, timeout: int) -> tuple[int | None, str]:
    """Run .cc_verify/verify.sh from the repo root with the checkout on
    PYTHONPATH (so `import <project>` loads THIS source, not site-packages).
    Returns (exit_code, tail_of_output). exit_code None == could not run."""
    entry = repo / VERIFY_ENTRY
    if not entry.exists():
        return None, "no verify.sh produced"
    import os
    env = {**os.environ}
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        p = subprocess.run(
            ["bash", VERIFY_ENTRY],
            cwd=str(repo), capture_output=True, text=True, timeout=timeout, env=env,
        )
        out = (p.stdout or "")
        if p.stderr:
            out += "\n[stderr]\n" + p.stderr
        return p.returncode, out[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"verify.sh timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001 - surface any launch error as a signal
        return None, f"verify.sh launch error: {e}"


def _script_signal(repo: Path, timeout: int) -> dict:
    """Deterministic before/after. AFTER = patched tree (current); BEFORE = patch
    stashed away. verify.sh lives under .cc_verify/ (untracked), so `git stash`
    (without -u) keeps it in place while reverting the tracked source edits."""
    entry = repo / VERIFY_ENTRY
    if not entry.exists():
        return {"signal": "NO_SCRIPT", "after_code": None, "before_code": None,
                "detail": "reviewer produced no .cc_verify/verify.sh",
                "after_out": "", "before_out": ""}

    after_code, after_out = _run_verify(repo, timeout)

    before_code, before_out = None, ""
    if _has_tracked_changes(repo):
        stash = _git(repo, "stash", "push", "-m", "cc_review_before")
        stashed = stash.returncode == 0 and "No local changes" not in stash.stdout
        if stashed:
            try:
                before_code, before_out = _run_verify(repo, timeout)
            finally:
                _git(repo, "stash", "pop")  # always restore the patch

    if after_code is None:
        sig = "SCRIPT_ERROR"
    elif after_code != 0:
        sig = "AFTER_FAIL"          # patch does not satisfy the issue's behavior
    elif before_code is None:
        sig = "WEAK"                # no failing baseline to compare against
    elif before_code != 0:
        sig = "STRONG_PASS"         # fails unpatched, passes patched -> real fix
    else:
        sig = "WEAK"               # passes both ways -> script doesn't discriminate

    return {"signal": sig, "after_code": after_code, "before_code": before_code,
            "after_out": after_out[-1500:], "before_out": before_out[-800:]}


# --------------------------------------------------------------------------- #
# verdict parsing
# --------------------------------------------------------------------------- #

def _parse_verdict(text: str) -> tuple[str | None, str]:
    """Pull the last `VERDICT: PASS|REVISE` and `CRITIQUE:` from the reviewer's
    final message. Returns (verdict_or_None, critique)."""
    verdict = None
    vmatches = re.findall(r"VERDICT:\s*(PASS|REVISE)", text, re.IGNORECASE)
    if vmatches:
        verdict = vmatches[-1].upper()
    critique = ""
    cmatches = list(re.finditer(r"CRITIQUE:\s*(.+)", text, re.IGNORECASE | re.DOTALL))
    if cmatches:
        critique = cmatches[-1].group(1).strip()
    return verdict, critique


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #

REVIEW_TASK = """You are the REVIEWER in a Planner-Executor-Reviewer loop. An Executor agent has just produced a candidate fix for a real bug in the `{repo}` repository. Your job is to INDEPENDENTLY judge whether that patch ACTUALLY and COMPLETELY resolves the issue. Assume nothing — the Executor is frequently confidently wrong.

Your current working directory is the repository checkout, which already contains the Executor's edits.

# The original issue
{problem_statement}

# The Executor's candidate patch (diff against the original code)
```diff
{patch}
```
{empty_note}
# Do BOTH of the following.

## 1. Write and run a verification script (PRIMARY signal)

### 1a. FIRST, take a BOUNDED look at how this project already tests the affected code
Do a QUICK, bounded look (this is not a research task — budget is limited): grep/glob the `tests/` tree for the changed class/function/attribute name and read AT MOST one or two existing test functions to learn HOW this project asserts correctness here — which object it inspects, and at which layer. Mirror that convention. Do NOT read the whole suite or wander; spend your effort on the script, not on browsing. These pre-existing repo tests are fair game — but you must NEVER look for, read, or rely on any hidden/official test that grades this task; work only from the issue text and the project's own source.

### 1b. THEN write the script — assert the CORE MECHANISM, not a surface proxy
Create an executable entry point at `{verify_entry}` (a bash script). Requirements:
- It must exercise the EXACT behavior described in the issue by importing/calling this project's own code. The repo root is on PYTHONPATH, so `import {repo_top}` loads THIS checkout's source.
- It must EXIT 0 if and only if the issue's described correct behavior holds, and EXIT NON-ZERO when the bug is present.
- **Assert on the precise core mechanism, NOT a downstream surface symptom.** The bug usually lives in an exact value, TYPE, boundary, or return value. Check the actual Python object the issue is about — its value AND its type — at the same layer the project's own tests check it. Do NOT settle for a looser proxy (a rendered HTML string, a serialized form, a "does the element/attribute exist" presence check): such proxies routinely COERCE types or hide off-by-one/precision errors, so they pass even when the real bug is still present. Concretely: if the issue concerns an attribute's value, assert `obj.attr == <expected>` AND `type(obj.attr) is <expected_type>` on the object itself — never on its string rendering.
- Use `==` against concrete expected values and assert the exact type where type matters; avoid substring/regex matches on rendered output as your acceptance criterion. A PASS must mean the bug is genuinely fixed, not something that trivially passes.
- Keep it self-contained and fast. Put the script AND any helper files ONLY under `{scratch}/`. Do NOT modify, create, or delete anything outside `{scratch}/`, and never touch test files.
- An automated harness will run your `{verify_entry}` against BOTH the patched tree and the original (patch reverted) tree. A trustworthy script FAILS on the original code and PASSES on the patched code. Design it to that contract.
Develop and run it yourself (with bash) until it reliably reflects the issue.

## 2. Critically review the patch (reasoning signal)
Independently of the script, reason — grounding every claim by READING the relevant source, not speculating:
- Does the patch fix the ROOT CAUSE in the issue, or only a symptom?
- Are there OTHER files / call sites / branches that must also change for a COMPLETE fix?
- **Value/type/boundary precision:** is every value the patch sets or returns of the EXACT expected TYPE (e.g. int vs str), at the right boundary (off-by-one, inclusive/exclusive), and exact precision? A value that looks right when printed/rendered but is the wrong type or off by one is a FAIL — call it out.

# Output — the LAST lines of your final message MUST be exactly one of:
VERDICT: PASS
   (the patch fully and correctly resolves the issue)
or
VERDICT: REVISE
CRITIQUE: <one tight, concrete, actionable paragraph for the Executor: what is wrong or incomplete, which file(s)/site(s) to change, and the correct expected behavior.>
"""

REVISE_PROMPT = """An independent reviewer checked your fix and it is NOT yet correct or complete. Revise your SOURCE edits to fully resolve the ORIGINAL issue.

Reviewer critique:
{critique}
{evidence}
Apply the necessary additional edits to the source now. Same rules as before: edit source only (no test files), keep a clean git diff, make the minimal change that fully resolves the issue. When you are confident it is correct, stop."""

SCRIPT_RETRY_PROMPT = """No runnable `{verify_entry}` was found — your verification did not produce one (or it could not be executed). This is REQUIRED: without it there is no primary signal and the patch cannot be trusted.

Write `{verify_entry}` NOW — a minimal, self-contained bash script under `{scratch}/` only. Keep it SHORT: import this project's own code, construct the exact object the issue is about, and assert its precise value AND type with `==` / `type(...)` (not a rendered/serialized proxy). It must EXIT 0 when the fixed behavior holds and NON-ZERO when the bug is present. Do not browse further — just produce and run the script. Then stop."""


# --------------------------------------------------------------------------- #
# cleanup
# --------------------------------------------------------------------------- #

def _cleanup(repo: Path, reviewer_artifacts: set[str], meta: dict, log) -> None:
    """Remove every file the Reviewer created, so the extracted patch is pure
    source. Files the Executor created are NOT in `reviewer_artifacts` and are
    preserved."""
    removed: list[str] = []
    for rel in sorted(reviewer_artifacts):
        if rel.startswith(SCRATCH):
            continue  # owned WorkspaceScratch removes this directory
        p = repo / rel
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed.append(rel)
        elif p.exists():
            try:
                p.unlink()
                removed.append(rel)
            except OSError:
                pass
    meta["artifacts_removed"] = removed
    if removed:
        log(f"[review] cleaned {len(removed)} reviewer artifact(s): {removed}")


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #

def run_review_loop(
    *,
    agent: Agent,
    llm,
    repo_dir,
    repo: str,
    problem_statement: str,
    max_rounds: int = 2,
    token_budget: int = 1_500_000,
    verify_timeout: int = 120,
    on_token=None,
    on_tool=None,
    log=print,
    workspace: WorkspaceExecution | None = None,
) -> dict:
    """Run the review->revise loop on top of an Executor `agent` that has already
    produced an initial patch in `repo_dir`. Mutates the working tree (via the
    Executor on revise) and returns review metadata. Always cleans up reviewer
    artifacts before returning, even on exception."""
    workspace = workspace or WorkspaceExecution.resolve(repo_dir)
    repo_dir = workspace.root
    scratch_owner = workspace.scratch(SCRATCH)
    repo_top = repo.split("/")[-1].replace("-", "_")

    base_tokens = llm.total_prompt_tokens + llm.total_completion_tokens
    meta: dict = {"enabled": True, "max_rounds": max_rounds, "rounds": [],
                  "final_decision": None, "budget_stop": False, "script_retries": 0,
                  "tokens_spent": 0, "artifacts_removed": []}
    reviewer_artifacts: set[str] = set()
    decision = "PASS"

    scratch_owner.__enter__()
    try:
        for rnd in range(1, max_rounds + 1):
            spent = (llm.total_prompt_tokens + llm.total_completion_tokens) - base_tokens
            if spent > token_budget:
                meta["budget_stop"] = True
                # Don't pretend the patch passed. If the LAST round's signal/decision
                # said the patch is NOT yet correct (AFTER_FAIL, or any REVISE), record
                # an honest BUDGET_ABORTED — patch is kept for grading but explicitly
                # flagged as never having passed verification. Only accept-as-PASS when
                # the last signal was actually benign.
                last = meta["rounds"][-1] if meta["rounds"] else None
                unresolved = (last is not None and
                              (last["signal"] in ("AFTER_FAIL", "SCRIPT_ERROR", "NO_SCRIPT")
                               or last["decision"] == "REVISE"))
                decision = "BUDGET_ABORTED" if unresolved else "PASS"
                log(f"[review] token budget hit ({spent}>{token_budget}); "
                    + (f"last signal {last['signal']}/{last['decision']} unresolved "
                       f"-> BUDGET_ABORTED (patch kept, NOT validated)" if unresolved
                       else "last signal benign -> accepting current patch"))
                break

            patch = _working_diff(repo_dir)
            extra_untracked = [f for f in _untracked(repo_dir) if not f.startswith(SCRATCH)]
            empty = patch.strip() == "" and not extra_untracked
            empty_note = (
                "\n**Note:** the Executor's patch is currently EMPTY (no change was made).\n"
                if empty else ""
            )

            reviewer = Agent(
                llm=llm,
                tools=_reviewer_tools(),
                max_context_tokens=agent.context.max_tokens,
                max_rounds=15,
                workspace=workspace,
                ledger=getattr(agent, "ledger", None),
                run_id=getattr(agent, "run_id", None),
                artifact_dir=getattr(getattr(agent, "runtime", None), "artifact_dir", None),
                manage_run_lifecycle=False,
                phase="reviewer",
                agent_id=f"reviewer-{rnd}",
            )
            task = REVIEW_TASK.format(
                repo=repo, repo_top=repo_top, problem_statement=problem_statement.strip(),
                patch=patch if patch.strip() else "(empty — the Executor made no edits)",
                empty_note=empty_note, verify_entry=VERIFY_ENTRY, scratch=SCRATCH,
            )

            pre = _untracked(repo_dir)
            verdict_text = reviewer.chat(task, on_token=on_token, on_tool=on_tool)
            reviewer_artifacts |= (_untracked(repo_dir) - pre)

            sig = _script_signal(repo_dir, verify_timeout)
            verdict, critique = _parse_verdict(verdict_text)

            # The before/after script is the PRIMARY signal — if the reviewer failed
            # to produce a runnable one, demand it once more before deciding, rather
            # than waving the patch through on a missing signal.
            if sig["signal"] == "NO_SCRIPT" and not empty:
                pre2 = _untracked(repo_dir)
                reviewer.chat(
                    SCRIPT_RETRY_PROMPT.format(verify_entry=VERIFY_ENTRY, scratch=SCRATCH),
                    on_token=on_token, on_tool=on_tool,
                )
                reviewer_artifacts |= (_untracked(repo_dir) - pre2)
                sig = _script_signal(repo_dir, verify_timeout)
                meta["script_retries"] = meta.get("script_retries", 0) + 1

            # ---- decision policy ----
            if empty:
                decision = "REVISE"
                critique = critique or ("The patch is empty — no change was made. "
                                        "Produce an actual source fix for the issue.")
            elif sig["signal"] == "AFTER_FAIL":
                decision = "REVISE"   # hard signal: patch fails its own verification
                critique = critique or ("Your verification of the issue still FAILS on the "
                                        "patched code — the fix does not resolve the issue.")
            elif sig["signal"] == "STRONG_PASS":
                decision = "PASS"     # reproduced-then-fixed: accept, early exit
            elif sig["signal"] in ("NO_SCRIPT", "SCRIPT_ERROR"):
                # No trustworthy primary signal even after a retry. Do NOT accept on the
                # script's behalf — only the reasoning path can vouch, and only if it
                # explicitly says PASS. Otherwise treat as unverified -> revise.
                decision = "PASS" if verdict == "PASS" else "REVISE"
                critique = critique or ("No runnable verification could confirm the fix; "
                                        "treat the patch as unverified and re-examine whether "
                                        "it truly resolves the issue (exact value/type/branch).")
            elif verdict == "REVISE":
                decision = "REVISE"
            elif verdict == "PASS":
                decision = "PASS"
            else:  # ambiguous + no strong script signal
                decision = "REVISE" if rnd < max_rounds else "PASS"

            meta["rounds"].append({
                "round": rnd,
                "signal": sig["signal"],
                "after_code": sig["after_code"],
                "before_code": sig["before_code"],
                "verdict": verdict,
                "empty": empty,
                "decision": decision,
                "critique": (critique or "")[:1000],
            })
            log(f"[review r{rnd}] script={sig['signal']} "
                f"(after={sig['after_code']},before={sig['before_code']}) "
                f"verdict={verdict} -> {decision}")

            if decision == "PASS" or rnd == max_rounds:
                break

            # ---- revise via the Executor (keeps its working context) ----
            evidence = ""
            if sig["signal"] == "AFTER_FAIL" and sig["after_out"]:
                evidence = ("\nYour verification still reports the bug on the patched code:\n"
                            f"{sig['after_out'][:800]}\n")
            agent.chat(
                REVISE_PROMPT.format(critique=critique or "(no detailed critique provided)",
                                     evidence=evidence),
                on_token=on_token, on_tool=on_tool,
            )

        meta["final_decision"] = decision
    finally:
        try:
            _cleanup(repo_dir, reviewer_artifacts, meta, log)
        finally:
            scratch_owner.__exit__(None, None, None)
            meta["artifacts_removed"].append(SCRATCH + "/")
        meta["tokens_spent"] = (llm.total_prompt_tokens + llm.total_completion_tokens) - base_tokens

    return meta
