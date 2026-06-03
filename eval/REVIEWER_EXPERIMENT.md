# Reviewer self-check experiment (archive)

Status: **paused — net ≈ 0 on its own, kept as a switchable ablation arm.**
Scope: first increment on top of the SWE-bench Verified Mini baseline (26/50 = 0.52).
Dev ruler: the 9-task subset (`eval/dev_subset.json`), baseline **4/9 resolved**.

This document records the Reviewer experiment honestly, including the negative
result. It is an archive, not an endorsement. Code stays in the tree behind the
`--reviewer` flag (off by default) so it can serve as a controlled comparison
arm for later increments.

---

## 1. Design

A Planner-Executor-Reviewer loop where, after the Executor produces a candidate
patch, an **independent Reviewer sub-agent** (clean context, isolated from the
Executor's self-confirmation bias) judges the patch with two signals:

1. **PRIMARY — self-written verification, run before/after (deterministic).**
   The Reviewer writes an executable `.cc_verify/verify.sh` that is supposed to
   exercise the exact behavior in the issue and exit 0 iff the bug is fixed. The
   orchestrator (Python, *not* the model) then runs it twice:
   - AFTER  — on the patched tree (expect exit 0)
   - BEFORE — with the patch `git stash`-ed away (expect exit ≠ 0)

   Classification: `before-fail & after-pass → STRONG_PASS`; `after-fail →
   AFTER_FAIL` (must revise); both-pass / no-baseline → `WEAK`; no script →
   `NO_SCRIPT`. The risky git ops live in Python so the working tree can't be
   corrupted and the extracted patch stays clean.

2. **AUXILIARY — reasoning critique.** The same Reviewer reasons about root-cause
   vs symptom, multi-file completeness, and value/type/boundary correctness, and
   emits `VERDICT: PASS|REVISE` + a concrete critique for the Executor.

Loop: review → (revise via the Executor) → review, capped by `max_rounds` and a
token budget. **Isolation red line:** the Reviewer and its script see only the
`problem_statement` and the repo source — never the official `test_patch`. That
is the credibility line for the whole benchmark and was never crossed.

Patch hygiene: every file the *Reviewer* creates is tracked and removed before
the patch is extracted (plus `.cc_verify/` is deleted wholesale), so a
verification artifact can never leak into the graded diff. Verified clean on
11790 (`artifacts_removed: [".cc_verify/"]`, graded diff = 1 source file).

Code: `corecoder/review.py`; wired into `eval/run_swebench.py` and
`eval/run_swebench_batch.py` behind `--reviewer` (subset results routed to a
separate `_subset_rev` namespace).

---

## 2. Results — two subset rounds, both 5/9, true Reviewer fixes = 0

| Run | resolved | resolved set | tokens | cost |
|-----|----------|--------------|--------|------|
| Baseline (no reviewer) | **4/9** | 12050, 12713, 12774, 9698 | 2.13M | ¥2.19 |
| Round 1 (reviewer v1) | **5/9** | + 12304 | 9.69M | ¥9.91 |
| Round 2 (reviewer v2, post-fixes) | **5/9** | + 12304 (same set) | 8.55M | ¥8.81 |

- **0 regressions** in either round (both resolved sets are a superset of the
  baseline 4).
- The single delta over baseline is **django-12304**, and it is **not** a
  Reviewer fix:
  - Round 1: 12304 verified `STRONG_PASS` in **1 round with no revise** — the
    Executor's first patch was already correct. This is Executor run-to-run
    **variance**, not anything the Reviewer did.
  - Round 2: 12304 ran a revise, but Round 1 already proved it resolves *without*
    a revise, so the flip is not attributable to the Reviewer either.
- **Count of baseline-unresolved tasks converted to resolved by a Reviewer-driven
  revise: 0 (in both rounds).** Every revise the Reviewer triggered on a still-
  broken task (11790, 8269, 8475, 8548) left it unresolved.

Round 2 used the v2 fixes (see §4): review budget 400k→1.5M, an honest
`BUDGET_ABORTED` status instead of a silent PASS when the budget runs out with an
unresolved signal, a bounded "read existing tests" step, and a one-shot retry +
no-default-pass when the script is missing. Those fixes made the loop **more
honest** (REVISE/BUDGET_ABORTED now surface instead of fake PASS) and let the
AFTER_FAIL revises actually run to completion — but they **did not add any fixing
power**: still 5/9, still 0 true fixes.

---

## 3. Why pure self-verification fails on its own — the self-blindspot

The baseline failure mode (see `BASELINE_ANALYSIS.md`) is "right file,
confidently wrong, zero self-check." The Reviewer adds the self-check — but when
it lets the agent **write its own verification**, the check inherits the same
wrong mental model that produced the bug. The Executor misunderstands the issue
and edits accordingly; the Reviewer (or the Executor on revise) writes a script
from that **same misunderstanding**; the script and the patch then *agree with
each other* and produce a **false `STRONG_PASS`** — while the real, hidden test
disagrees. Self-verification cannot see its own blind spot.

Two confirmed cases:

- **django-11790 (false STRONG_PASS via wrong observable).** Real bug: the patch
  set `widget.attrs['maxlength'] = str(value)` — a **string**, where the gold
  test asserts the **integer** `255` (`'255' != 255`). The Reviewer's verify
  script asserted on the **rendered HTML** (`maxlength="150"`), which is *always*
  a string regardless of the Python type — so it structurally could not tell int
  from str. before=absent→after=present ⇒ `STRONG_PASS` ⇒ accepted. Official
  result: **unresolved**. The check tested a surface proxy, not the core
  mechanism.

- **sphinx-8475 (false STRONG_PASS after a revise).** Round 1 signal was a
  correct `AFTER_FAIL` (the patch failed its own verification) → the Reviewer
  triggered a revise. The Executor revised both the patch *and* (implicitly) the
  understanding the next script was built on; Round 2 then verified
  `STRONG_PASS` (after=0, before=1) → accepted as PASS. The task is still
  **unresolved**: the revised patch and the revised script were authored from the
  same still-incorrect understanding, so they agreed while the real test did not.

A secondary, mechanical failure (now fixed, but illustrative) was that the
heavier "study the tests, assert types" prompt frequently caused the Reviewer to
burn its budget and **not produce a runnable script at all** (`NO_SCRIPT`), at
which point the old logic waved the patch through. Even after fixing that
(bounded reading + mandatory script + retry + no-default-pass), the underlying
self-blindspot remained and the score did not move.

---

## 4. Conclusion & disposition

- **Net contribution of pure self-verification, alone: ≈ 0.** Two rounds, 5/9
  each vs baseline 4/9, where the +1 is Executor variance (12304), **0**
  baseline-unresolved tasks fixed by the Reviewer, **0** regressions, at roughly
  **4× the token cost** (¥2.2 → ¥8.8–9.9 on the subset).
- **Root limitation:** a self-authored check shares the agent's blind spot, so it
  cannot catch the errors that matter most (it confidently confirms them —
  11790, 8475). Making the loop honest (BUDGET_ABORTED, no-default-pass) stops it
  from *lying*, but does not give it independent ground truth.
- **Disposition:** keep the Reviewer in the tree behind `--reviewer` (off by
  default) as a **switchable ablation arm** — useful as a controlled comparison
  and as a host for future verification signals that are *not* self-authored.
- **Next direction (not designed here):** move to the **Planner**, which attacks
  a *different* failure mode than self-check — incomplete fixes that miss
  required edits across multiple files, and non-convergence — rather than trying
  to harden a check that is fundamentally blind to the agent's own mistakes.

### v2 fixes already in the code (for the ablation arm)

1. Review token budget default **400k → 1.5M** (a revise round can finish).
2. On budget exhaustion with a last signal of AFTER_FAIL / SCRIPT_ERROR /
   NO_SCRIPT / REVISE → record **`BUDGET_ABORTED`** (patch kept but flagged
   "not validated"), instead of a silent `PASS`.
3. "Study existing tests" step is **bounded** (≤ 1–2 test functions) so it can't
   starve the script.
4. A missing `verify.sh` triggers **one focused retry**; `NO_SCRIPT` /
   `SCRIPT_ERROR` are treated as untrusted (→ REVISE unless the reasoning path
   explicitly says PASS), never a default pass.
