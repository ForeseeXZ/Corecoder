# SWE-bench Verified Mini — Baseline Failure Analysis

**Baseline:** CoreCoder agent + `deepseek-v4-flash`, 接法 B (container-outside checkout).
**Result:** resolved **26/50 = 0.52** (django 16/25 = 0.64, sphinx 10/25 = 0.40).
**Scope of this doc:** the **24 unresolved** instances. No re-evaluation was run — analysis is
derived entirely from existing artifacts under `eval/runs/` plus the dataset gold patches.

---

## 1. Method (how each fail was classified — objective, not vibes)

Two signals, both extractable from saved artifacts:

1. **Localization signal — agent patch files vs gold patch files.**
   Parse `+++ b/<file>` from `eval/runs/<id>/patch.diff` (agent) and from the dataset's
   `patch` field (gold). Compare the file sets:
   - no overlap → **① localization failure** (edited the wrong file entirely)
   - agent hit all gold files → localization fine; failure is **② logic**
   - agent hit *some but not all* gold files → **②+① partial** (found main file, missed secondary)

2. **Terminal-state signal — the agent's final message** (`summary.json → agent.final_text`):
   - `"(reached maximum tool-call rounds)"` → **③ non-convergence** (hit the 50-round cap)
   - empty `patch.diff` → **③ thrash/empty** (explored, never produced an edit)
   - a confident completion ("fix is complete / root cause is …") → **② confident-wrong**

Harness per-instance reports (`logs/run_evaluation/.../report.json`) give
`patch_successfully_applied` → catches **④ technical (apply/format)** failures.

Failure taxonomy used:
① localization · ② logic (right place, wrong/incomplete fix) · ③ non-convergence/divergence ·
④ patch technical (apply/format) · ⑤ objectively too hard.

---

## 2. The 24 unresolved instances

| # | instance | repo | patch | token | tools | time | class |
|--|--|--|--|--|--|--|--|
| 1 | django-11790 | dj | applied | 312k | 24 | 53s | ② logic |
| 2 | django-11848 | dj | applied | 47k | 8 | 29s | ② logic |
| 3 | django-11885 | dj | applied | 801k | 40 | 139s | ②+① partial (miss 1/2 files) |
| 4 | django-12273 | dj | applied | 257k | 23 | 66s | ② logic |
| 5 | django-12304 | dj | applied | 29k | 7 | 18s | ② logic |
| 6 | django-12308 | dj | applied | 105k | 18 | 37s | ② logic |
| 7 | django-12325 | dj | applied | 144k | 13 | 56s | ②+① partial (miss 1/2 files) |
| 8 | django-12406 | dj | applied | 788k | 31 | 125s | ②+① partial (miss 1/2 files) |
| 9 | django-12774 | dj | applied | 27k | 7 | 13s | ② logic |
| 10 | sphinx-10435 | sx | applied | 460k | 13 | 124s | ② logic |
| 11 | sphinx-10673 | sx | applied | 1.91M | 48 | 191s | ②+① partial (miss 1/3 files) |
| 12 | sphinx-11510 | sx | applied | 1.22M | 44 | 172s | ② logic |
| 13 | sphinx-7590 | sx | applied | 707k | 40 | 116s | ②+① partial (miss 2/3 files) |
| 14 | sphinx-7748 | sx | applied | 757k | 41 | 146s | ② logic |
| 15 | sphinx-7985 | sx | applied | 1.47M | 36 | 247s | ② logic |
| 16 | sphinx-8056 | sx | applied | 977k | 47 | 179s | ② logic |
| 17 | sphinx-8269 | sx | applied | 29k | 5 | 12s | ② logic |
| 18 | sphinx-8475 | sx | applied | 64k | 10 | 20s | ② logic |
| 19 | sphinx-8548 | sx | applied | 1.41M | 56 | 151s | ③ non-converge (max-rounds) |
| 20 | sphinx-8638 | sx | applied | 2.37M | 59 | 431s | ③ non-converge (max-rounds) |
| 21 | sphinx-9229 | sx | **empty** | 2.30M | 55 | 269s | ③ thrash/empty (max-rounds) |
| 22 | sphinx-9281 | sx | applied | 304k | 11 | 58s | ② logic |
| 23 | sphinx-9320 | sx | applied | 58k | 8 | 34s | ② logic |
| 24 | sphinx-9461 | sx | applied | 1.67M | 61 | 274s | ③ non-converge (max-rounds) |

Patch technical state: **23/24 applied cleanly, 1 empty (9229), 0 apply failures.**

---

## 3. Failure-mode distribution

| class | count | share | django | sphinx |
|--|--|--|--|--|
| ① pure localization (wrong file) | **0** | 0% | 0 | 0 |
| ② logic (right place, wrong/incomplete fix) | **20** | **83%** | 9 | 11 |
| &nbsp;&nbsp;— pure logic (right files, wrong content) | 15 | | 6 | 9 |
| &nbsp;&nbsp;— partial (found main file, missed secondary files) | 5 | | 3 | 2 |
| ③ non-convergence / divergence (max-rounds / empty) | **4** | 17% | 0 | 4 |
| ④ patch technical (apply / format) | **0** | 0% | 0 | 0 |
| ⑤ objectively too hard | not a separate bucket; absorbed into the high-token ② / ③ cases | | | |

### Two hard facts that drive the decision

1. **File-level localization never failed (0/24).** Every non-empty agent patch touched at least
   one gold file. The agent *finds the right place*.
2. **20/24 (83%) are "confident-wrong with zero self-check."** The agent ends with a completion
   declaration ("fix is complete / root cause is …"), having edited the right file, but the fix is
   wrong or incomplete — and **nothing in the loop ever re-examines the patch before stopping.**
   Example: django-12304 — 18s, 7 tools, found the root cause (template engine calling an enum
   class), made one edit, declared done; the fix was wrong and no step caught it.

---

## 4. Why sphinx is worse (60% fail vs django 36%)

It is **not** that a different failure class lives in sphinx. It is the **same dominant ② amplified**,
plus a **non-convergence tail unique to sphinx**:

- **All 4 ③ non-convergence fails are sphinx** (8548 / 8638 / 9229 / 9461) — each hit the 50-round
  cap after burning 1.4–2.4M tokens, still wrong or empty. django has zero.
- **sphinx fail median = 977k tokens vs django fail median = 144k (~7×).** All 7 instances over
  1M tokens are sphinx. sphinx fails average 161s vs sphinx passes 62s.
- sphinx gold patches are slightly larger (median 36 vs 21 lines); multi-file ratio is similar
  (sx 4/15, dj 3/9). So the code is somewhat harder, but the dominant driver is
  **"hard-to-reason code → exploration blows up (context bloat) → still ships a wrong fix."**

One line: **django fails = cheap, fast, confidently-wrong; sphinx fails = expensive thrashing that
is still wrong (+ a 4-task tail that never converges).**

---

## 5. First increment — which to build (data-backed)

| candidate | failures it directly hits | verdict |
|--|--|--|
| **Search SubAgent** | ① localization = **0 tasks** | ❌ **lowest priority.** File-level localization is already 100% solved; a search subagent's core payoff (find the right file/function) addresses a non-problem. |
| **Context compression** | ③ non-converge (4) + the 7 >1M-token thrash runs | ⚠️ **secondary / enabler.** Helps the expensive sphinx tail and cost, but **compression does not turn a wrong fix into a correct one.** |
| **Planner-Executor-Reviewer** | ② confident-wrong **20 (83%)** + ②+① partial 5 (Planner scopes multi-file) + ③ 4 (Planner decomposes/bounds exploration) | ✅ **first.** The only option that targets the majority failure mode head-on. |

**Decision: build Planner-Executor-Reviewer, leading with the Reviewer (self-check / critique loop).**

- The dominant failure is **83% "right place, confidently wrong, never reviewed."** The agent
  currently `return`s the moment it thinks it's done — no "does this patch actually satisfy the
  issue / what about edge cases / is there another site to change" step. **The Reviewer fills exactly
  that gap.**
- The Executor's localization is already strong (0 wrong-file), so within PER the **Planner/Executor
  are not the bottleneck — the Reviewer is the high-leverage piece**, sitting on top of a reliable
  locator so a review→revise loop has a solid base.
- Planner (second priority) additionally covers the **5 partial/multi-file** misses (scope "all sites
  that must change" up front) and helps the **4 sphinx non-convergence** cases (decompose the task,
  bound exploration).

### Two honest caveats (they shape the Reviewer design)

1. **The Reviewer is a reasoning critic, not a test runner.** Under 接法 B the agent never sees the
   official `test_patch`, so the Reviewer must judge correctness by reasoning about the
   problem_statement and edge cases, not by executing the grader. Its ceiling is bounded by reasoning
   quality — it will not flip all 20, but it is the only increment that even attacks the majority mode.
2. **The 4 ③ tasks are not "out of rounds" — they are thrashing.** They already burned ~2M tokens
   before hitting the cap; simply raising `max_rounds` would only burn more. Covering the sphinx tail
   needs Planner decomposition + (later) context compression, not more rounds.

---

## 6. Development subset for fast Reviewer iteration

A fixed 9-task ruler lives in [`eval/dev_subset.json`](dev_subset.json): 5 ② confident-wrong fails
(Reviewer's primary target), 3 resolved tasks (regression guard — don't break what already passes),
and 1 sphinx ③ hard task (convergence boundary; an optional 2nd stress task is listed there).
Rationale per task is in that file.
