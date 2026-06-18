"""Batched SWE-bench Verified Mini driver for CoreCoder (方案 B, pipelined).

Orchestrates the full 50-instance run (or any subset) in small batches so disk
stays bounded. Per batch:
  1. pull the batch's prebuilt instance images (skip ones already local)
  2. run the CoreCoder agent on each instance to produce a patch
     — as an ISOLATED SUBPROCESS per instance (reuses run_swebench.py single
       mode), concurrency configurable (default 2). Subprocess isolation is
       required because run_agent uses os.chdir + SIGALRM, which are not
       thread-safe; separate processes also match 接法 B's physical isolation.
  3. consolidate the batch's predictions into one .jsonl
  4. grade with the official harness (--max_workers 4, --cache_level env)
  5. docker rmi the batch's instance images to release disk, then next batch

Features:
  - per-instance failure isolation (one bad task never stops the batch/run)
  - one progress line per task
  - resume: instances already graded (in _results.jsonl) are skipped
  - everything lands under eval/runs_mimo/ by default

Usage:
  python eval/run_swebench_batch.py --all                 # all 50
  python eval/run_swebench_batch.py -i id1 id2 id3        # specific ids
  python eval/run_swebench_batch.py -i id1 ... --batch-size 8 --agent-concurrency 2
  python eval/run_swebench_batch.py --all --force         # ignore resume, redo all
"""

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "eval"))

# reuse the single-instance pipeline + constants
import run_swebench as rs

EVAL_ROOT = REPO_ROOT / "eval"
RUNS_DIR = Path(os.environ.get("CORECODER_RUNS_DIR", EVAL_ROOT / "runs_mimo"))
RESULTS_PATH = RUNS_DIR / "_swebench_results.jsonl"
AGGREGATE_PATH = RUNS_DIR / "_swebench_aggregate.json"
REPORTS_DIR = RUNS_DIR / "_reports"

RUN_ID_PREFIX = "corecoder_full"

# dev subset (fast Reviewer-iteration ruler) — written to its own result namespace
# so it never collides with the full-50 results and can be resumed independently.
DEV_SUBSET_PATH = EVAL_ROOT / "dev_subset.json"
SUBSET_RESULTS_PATH = RUNS_DIR / "_subset_results.jsonl"
SUBSET_AGGREGATE_PATH = RUNS_DIR / "_subset_aggregate.json"
SUBSET_RUN_ID_PREFIX = "corecoder_subset"

# subset WITH the Reviewer loop on — kept in its own `_rev` namespace so the
# Reviewer numbers never overwrite the baseline subset (compare side by side).
SUBSET_REV_RESULTS_PATH = RUNS_DIR / "_subset_rev_results.jsonl"
SUBSET_REV_AGGREGATE_PATH = RUNS_DIR / "_subset_rev_aggregate.json"
SUBSET_REV_RUN_ID_PREFIX = "corecoder_subset_rev"

# subset WITH the Planner on (and optionally +Reviewer) — own namespaces so the
# four ablation arms (baseline / +reviewer / +planner / +planner+reviewer) never
# overwrite each other and can each be resumed independently.
SUBSET_PLAN_RESULTS_PATH = RUNS_DIR / "_subset_plan_results.jsonl"
SUBSET_PLAN_AGGREGATE_PATH = RUNS_DIR / "_subset_plan_aggregate.json"
SUBSET_PLAN_RUN_ID_PREFIX = "corecoder_subset_plan"

SUBSET_PLAN_REV_RESULTS_PATH = RUNS_DIR / "_subset_plan_rev_results.jsonl"
SUBSET_PLAN_REV_AGGREGATE_PATH = RUNS_DIR / "_subset_plan_rev_aggregate.json"
SUBSET_PLAN_REV_RUN_ID_PREFIX = "corecoder_subset_plan_rev"


def load_subset_ids(include_optional: bool = False) -> list[str]:
    """Core 9 ids from dev_subset.json (optional_stress excluded by default)."""
    spec = json.loads(DEV_SUBSET_PATH.read_text())
    ids = [e["instance_id"] for e in spec.get("core", [])]
    if include_optional:
        ids += [e["instance_id"] for e in spec.get("optional_stress", [])]
    return ids


# ----------------------------- small helpers -----------------------------

def df_root() -> str:
    out = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout
    return out.strip().splitlines()[-1]


def chunk(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def image_present(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0


def image_size(image: str) -> str:
    out = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Size}}"],
        capture_output=True, text=True)
    if out.returncode != 0:
        return "?"
    try:
        b = int(out.stdout.strip())
        return f"{b / 1e9:.2f}GB"
    except ValueError:
        return "?"


def pull_image(image: str, retries: int = 3) -> tuple[bool, str]:
    if image_present(image):
        return True, ""
    last_err = ""
    for attempt in range(1, retries + 1):
        proc = subprocess.run(["docker", "pull", image],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return True, ""
        last_err = proc.stderr.strip()
        print(f"      pull attempt {attempt}/{retries} failed: "
              f"{last_err.splitlines()[-1:] or ['?']}", flush=True)
        time.sleep(2 * attempt)
    return False, last_err


def _docker_not_found(error: str) -> bool:
    text = error.lower()
    return any(s in text for s in (
        "not found",
        "manifest unknown",
        "repository does not exist",
    ))


def build_instance_image(instance: dict, image: str) -> bool:
    """Build a missing SWE-bench instance image locally.

    Docker Hub does not always have every prebuilt `swebench/sweb.eval...`
    image. The official harness can still materialize the same image from the
    dataset metadata, which lets the rest of this driver keep using one tag.
    """
    namespace = image.split("/", 1)[0] if "/" in image else None
    try:
        import docker
        from swebench.harness.docker_build import build_instance_images

        client = docker.from_env()
        print("      remote image missing; building locally with swebench harness ...",
              flush=True)
        build_instance_images(
            client,
            [instance],
            force_rebuild=False,
            max_workers=1,
            namespace=namespace,
            tag="latest",
        )
    except Exception as e:
        print(f"      local build failed: {type(e).__name__}: {e}", flush=True)
        return False
    if image_present(image):
        return True
    print(f"      local build finished but expected tag is missing: {image}",
          flush=True)
    return False


def ensure_image(instance: dict, image: str) -> tuple[bool, str]:
    if image_present(image):
        return True, "cached"
    ok, err = pull_image(image)
    if ok:
        return True, "pulled"
    if _docker_not_found(err) and build_instance_image(instance, image):
        return True, "built"
    return False, "FAILED"


def load_completed(results_path: Path) -> dict:
    """instance_id -> result record, for instances already graded (resumeable)."""
    done = {}
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("resolved") is not None:
                done[rec["instance_id"]] = rec
    return done


def append_result(rec: dict, results_path: Path):
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ablation_suffix(args) -> str:
    suffix = ""
    if args.planner:
        suffix += "_plan"
        # The default strong planner keeps the bare _plan name; any other
        # model appends a short tag, e.g. mimo-v2.5 -> _plan_v2.5planner.
        if args.planner_model != rs.DEFAULT_PLANNER_MODEL:
            model_tag = args.planner_model.split("-")[-1] or "altplanner"
            suffix += f"_{model_tag}planner"
    if args.reviewer:
        suffix += "_rev"
    if args.compress:
        suffix += "_comp"
    if args.mcp:
        suffix += "_mcp"
    return suffix


# ----------------------------- agent phase -----------------------------

def run_agent_subprocess(instance_id: str, timeout_s: float, with_hints: bool,
                         reviewer: bool = False, review_cfg: dict | None = None,
                         planner: bool = False, plan_cfg: dict | None = None,
                         compress: bool = False, compress_cfg: dict | None = None,
                         mcp: bool = False, mcp_cfg: dict | None = None) -> dict:
    """Run the agent for ONE instance via run_swebench.py in a child process.

    Returns a small dict read back from RUNS_DIR/<id>/{summary,prediction}.json.
    Never raises — any failure becomes a record with patch_empty=True.
    """
    base = RUNS_DIR / instance_id
    cmd = [
        sys.executable, str(EVAL_ROOT / "run_swebench.py"),
        "-i", instance_id,
        "--timeout", str(timeout_s),
        "--out", str(base / "_single_prediction.jsonl"),
    ]
    if with_hints:
        cmd.append("--with-hints")
    if planner:
        pcfg = plan_cfg or {}
        cmd.append("--planner")
        cmd += ["--planner-model", str(pcfg.get("model", rs.DEFAULT_PLANNER_MODEL))]
        cmd += ["--planner-max-rounds", str(pcfg.get("max_rounds", 20))]
        cmd += ["--planner-token-budget", str(pcfg.get("token_budget", 600_000))]
    if reviewer:
        cfg = review_cfg or {}
        cmd.append("--reviewer")
        cmd += ["--review-max-rounds", str(cfg.get("max_rounds", 2))]
        cmd += ["--reviewer-token-budget", str(cfg.get("token_budget", 1_500_000))]
        cmd += ["--verify-timeout", str(cfg.get("verify_timeout", 120))]
    if compress:
        ccfg = compress_cfg or {}
        cmd.append("--compress")
        if "snip_at" in ccfg:
            cmd += ["--compress-snip-at", str(ccfg["snip_at"])]
        if "summarize_at" in ccfg:
            cmd += ["--compress-summarize-at", str(ccfg["summarize_at"])]
        if "collapse_at" in ccfg:
            cmd += ["--compress-collapse-at", str(ccfg["collapse_at"])]
        if "keep_recent" in ccfg:
            cmd += ["--compress-keep-recent", str(ccfg["keep_recent"])]
    if mcp:
        mcfg = mcp_cfg or {}
        cmd.append("--mcp")
        if "startup_timeout" in mcfg:
            cmd += ["--mcp-startup-timeout", str(mcfg["startup_timeout"])]
        if "call_timeout" in mcfg:
            cmd += ["--mcp-call-timeout", str(mcfg["call_timeout"])]

    # hard wall on the subprocess slightly above the agent's own SIGALRM budget
    proc_timeout = timeout_s + 180
    subproc_err = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=proc_timeout, cwd=str(REPO_ROOT))
        if proc.returncode != 0:
            subproc_err = (proc.stderr.strip().splitlines()[-3:] or ["nonzero exit"])
    except subprocess.TimeoutExpired:
        subproc_err = [f"subprocess hard-killed after {proc_timeout}s"]

    summary_path = base / "summary.json"
    pred_path = base / "prediction.json"

    if pred_path.exists():
        pred = json.loads(pred_path.read_text())
        model_patch = pred.get("model_patch", "")
    else:
        model_patch = ""

    if summary_path.exists():
        s = json.loads(summary_path.read_text())
        agent = s.get("agent", {})
        patch_info = s.get("patch", {})
        review = s.get("review")
        plan = s.get("plan")
        compress_meta = s.get("compress")
        mcp_meta = s.get("mcp")
    else:
        agent, patch_info, review, plan, compress_meta, mcp_meta = (
            {}, {}, None, None, None, None)

    # planner runs on a separate (pro) model, so its tokens/cost are tracked
    # separately in the summary's plan block — fold them into the headline totals.
    plan_tok = (plan or {}).get("tokens_spent", 0) or 0
    plan_cost = (plan or {}).get("cost_cny") or 0.0

    return {
        "instance_id": instance_id,
        "model_patch": model_patch,
        "patch_empty": model_patch.strip() == "",
        "n_files": patch_info.get("n_files"),
        "prompt_tokens": agent.get("prompt_tokens", 0),
        "completion_tokens": agent.get("completion_tokens", 0),
        "plan_tokens": plan_tok,
        "cost_cny": (agent.get("estimated_cost_cny") or 0.0) + plan_cost,
        "agent_elapsed_s": agent.get("elapsed_s", 0.0),
        "agent_error": agent.get("error"),
        "subproc_error": subproc_err,
        "review": review,
        "plan": plan,
        "compress": compress_meta,
        "mcp": mcp_meta,
    }


# ----------------------------- harness phase -----------------------------

def grade_batch(predictions_path: Path, run_id: str, max_workers: int) -> dict:
    """Run the official harness on a batch's predictions; return resolved map."""
    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "-d", rs.DATASET, "-s", rs.SPLIT,
        "-p", str(predictions_path),
        "-id", run_id,
        "--max_workers", str(max_workers),
        "--cache_level", "env",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    # harness writes <model>.<run_id>.json into cwd
    report_name = f"{rs.MODEL_NAME_OR_PATH}.{run_id}.json"
    report_path = REPO_ROOT / report_name
    resolved_map = {}
    if report_path.exists():
        report = json.loads(report_path.read_text())
        for iid in report.get("resolved_ids", []):
            resolved_map[iid] = True
        for iid in report.get("unresolved_ids", []):
            resolved_map[iid] = False
        for iid in report.get("error_ids", []):
            resolved_map[iid] = False
        for iid in report.get("empty_patch_ids", []):
            resolved_map.setdefault(iid, False)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(report_path), str(REPORTS_DIR / report_name))
    else:
        print("      [warn] harness report not found; tail of harness stderr:")
        for ln in proc.stderr.strip().splitlines()[-8:]:
            print(f"        {ln}")
    return resolved_map


def clean_batch_images(images: list[str]):
    for img in images:
        subprocess.run(["docker", "rmi", "-f", img], capture_output=True)


# ----------------------------- driver -----------------------------

def run_driver(targets: list[str], ds, batch_size: int, agent_concurrency: int,
               timeout_s: float, max_workers: int, with_hints: bool, force: bool,
               results_path: Path = RESULTS_PATH, aggregate_path: Path = AGGREGATE_PATH,
               run_id_prefix: str = RUN_ID_PREFIX,
               reviewer: bool = False, review_cfg: dict | None = None,
               planner: bool = False, plan_cfg: dict | None = None,
               compress: bool = False, compress_cfg: dict | None = None,
               mcp: bool = False, mcp_cfg: dict | None = None):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    completed = {} if force else load_completed(results_path)
    if completed:
        skip = [t for t in targets if t in completed]
        print(f"[resume] {len(skip)} already graded, skipping: {skip}")
        targets = [t for t in targets if t not in completed]
    if not targets:
        print("[driver] nothing to do (all targets already graded).")
        return

    id2inst = {row["instance_id"]: row for row in ds}
    batches = chunk(targets, batch_size)

    df_start = df_root()
    print(f"[driver] {len(targets)} instances in {len(batches)} batches "
          f"(size={batch_size}, agent_concurrency={agent_concurrency}, "
          f"harness max_workers={max_workers})")
    print(f"[disk ] before: {df_start}")
    t_run = time.monotonic()

    grand = {"resolved": 0, "unresolved": 0, "tokens": 0, "cost": 0.0,
             "agent_time": 0.0, "driver_issues": []}

    for bi, batch in enumerate(batches, 1):
        run_id = f"{run_id_prefix}_b{bi:02d}"
        images = {iid: rs.instance_image_tag(iid) for iid in batch}
        print(f"\n{'='*72}\n[batch {bi}/{len(batches)}] {batch}\n{'='*72}")

        # --- 1. pull images ---
        print(f"[batch {bi}] pulling {len(batch)} images ...")
        image_ok = {}
        for iid in batch:
            img = images[iid]
            ok, source = ensure_image(id2inst[iid], img)
            image_ok[iid] = ok
            tag = source if not ok else f"{source} {image_size(img)}"
            print(f"   - {iid:<32} {tag}", flush=True)

        # --- 2. agent phase (subprocess per instance, concurrency-limited) ---
        print(f"[batch {bi}] running agents (concurrency={agent_concurrency}) ...")
        agent_results: dict[str, dict] = {}
        runnable = [iid for iid in batch if image_ok[iid]]
        for iid in batch:
            if not image_ok[iid]:
                grand["driver_issues"].append(f"{iid}: image pull failed")

        with concurrent.futures.ThreadPoolExecutor(max_workers=agent_concurrency) as pool:
            # threads here only WAIT on subprocesses (the real isolation is the
            # child process), so this is safe.
            fut2id = {
                pool.submit(run_agent_subprocess, iid, timeout_s, with_hints,
                            reviewer, review_cfg, planner, plan_cfg,
                            compress, compress_cfg, mcp, mcp_cfg): iid
                for iid in runnable
            }
            for fut in concurrent.futures.as_completed(fut2id):
                iid = fut2id[fut]
                try:
                    agent_results[iid] = fut.result()
                except Exception as e:
                    agent_results[iid] = {
                        "instance_id": iid, "model_patch": "", "patch_empty": True,
                        "prompt_tokens": 0, "completion_tokens": 0, "cost_cny": 0.0,
                        "agent_elapsed_s": 0.0, "agent_error": f"driver: {e!r}",
                        "subproc_error": [repr(e)],
                    }
                r = agent_results[iid]
                flag = "EMPTY" if r["patch_empty"] else f"{r.get('n_files')}f"
                err = r.get("agent_error") or (r.get("subproc_error") and "subproc-err")
                print(f"   · {iid:<32} patch={flag:<6} "
                      f"tok={r['prompt_tokens']}+{r['completion_tokens']} "
                      f"agent={r['agent_elapsed_s']:.0f}s"
                      f"{'  ['+str(err)+']' if err else ''}", flush=True)

        # --- 3. consolidate predictions for this batch ---
        batch_pred_path = RUNS_DIR / f"_batch_b{bi:02d}_predictions.jsonl"
        with batch_pred_path.open("w") as f:
            for iid in batch:
                r = agent_results.get(iid)
                patch = r["model_patch"] if r else ""
                f.write(json.dumps({
                    "instance_id": iid,
                    "model_name_or_path": rs.MODEL_NAME_OR_PATH,
                    "model_patch": patch,
                }, ensure_ascii=False) + "\n")

        # --- 4. grade with harness ---
        print(f"[batch {bi}] grading with harness (run_id={run_id}) ...")
        resolved_map = grade_batch(batch_pred_path, run_id, max_workers)

        # --- record per-instance results ---
        for iid in batch:
            r = agent_results.get(iid, {})
            resolved = resolved_map.get(iid)
            if resolved is None and iid not in image_ok:
                resolved = False
            rec = {
                "instance_id": iid,
                "repo": id2inst[iid]["repo"],
                "batch": bi,
                "resolved": bool(resolved) if resolved is not None else None,
                "patch_empty": r.get("patch_empty", True),
                "image_ok": image_ok.get(iid, False),
                "prompt_tokens": r.get("prompt_tokens", 0),
                "completion_tokens": r.get("completion_tokens", 0),
                "cost_cny": r.get("cost_cny", 0.0),
                "plan_tokens": r.get("plan_tokens", 0),
                "agent_elapsed_s": r.get("agent_elapsed_s", 0.0),
                "agent_error": r.get("agent_error"),
                "subproc_error": r.get("subproc_error"),
                "review": r.get("review"),
                "plan": r.get("plan"),
                "compress": r.get("compress"),
                "mcp": r.get("mcp"),
            }
            if resolved is None:
                grand["driver_issues"].append(f"{iid}: no harness verdict")
            append_result(rec, results_path)
            if rec["resolved"]:
                grand["resolved"] += 1
            else:
                grand["unresolved"] += 1
            grand["tokens"] += (rec["prompt_tokens"] + rec["completion_tokens"]
                                + rec.get("plan_tokens", 0))
            grand["cost"] += rec["cost_cny"]
            grand["agent_time"] += rec["agent_elapsed_s"]
            verdict = "RESOLVED" if rec["resolved"] else "unresolved"
            pl = rec.get("plan")
            pl_note = ""
            if pl and pl.get("enabled"):
                pl_note = (f"  [plan {pl.get('n_planned_files')}f"
                           f" json={'ok' if pl.get('json_parse_ok') else 'fb:'+str(pl.get('json_fallback'))}"
                           f"{'/ERR' if pl.get('error') else ''},"
                           f" {pl.get('tokens_spent',0)}tok]")
            rv = rec.get("review")
            rv_note = ""
            if rv and rv.get("enabled"):
                rounds = rv.get("rounds", [])
                sigs = ",".join(str(rd.get("signal")) for rd in rounds) or "-"
                rv_note = (f"  [review {len(rounds)}rd {sigs}"
                           f" -> {rv.get('final_decision')}, {rv.get('tokens_spent',0)}tok]")
            cm = rec.get("compress")
            cm_note = ""
            if cm and cm.get("enabled"):
                lc = cm.get("layer_counts", {})
                cm_note = (f"  [compress L1/2/3="
                           f"{lc.get('1_tool_snip',0)}/{lc.get('2_summarize',0)}/"
                           f"{lc.get('3_archive',0)}, -{cm.get('total_reclaimed_tokens',0)}tok,"
                           f" peak {cm.get('peak_tokens',0)}]")
            mc = rec.get("mcp")
            mc_note = ""
            if mc and mc.get("enabled"):
                if mc.get("server_started"):
                    mc_note = (f"  [mcp {mc.get('tool_calls',0)}call/"
                               f"{mc.get('fallback_calls',0)}fb]")
                else:
                    mc_note = "  [mcp FAILED->builtin]"
            print(f"   = {iid:<32} {verdict}{pl_note}{rv_note}{cm_note}{mc_note}",
                  flush=True)

        # --- 5. clean batch images ---
        # clean_batch_images(list(images.values()))
        print(f"[batch {bi}] cleaned {len(images)} instance images")
        print(f"[disk ] after batch {bi}: {df_root()}")

    wall = time.monotonic() - t_run
    df_end = df_root()
    total = grand["resolved"] + grand["unresolved"]
    aggregate = {
        "dataset": rs.DATASET,
        "model": rs.MODEL_NAME_OR_PATH,
        "n_graded": total,
        "resolved": grand["resolved"],
        "unresolved": grand["unresolved"],
        "pass_rate": grand["resolved"] / total if total else 0.0,
        "total_tokens": grand["tokens"],
        "total_cost_cny": grand["cost"],
        "total_agent_time_s": grand["agent_time"],
        "wall_time_s": wall,
        "driver_issues": grand["driver_issues"],
        "df_before": df_start,
        "df_after": df_end,
    }
    aggregate_path.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False))

    print(f"\n{'#'*72}")
    print(f"DONE  resolved {grand['resolved']}/{total} = "
          f"{aggregate['pass_rate']:.3f}")
    print(f"wall: {wall:.0f}s ({wall/60:.1f} min)   "
          f"agent total: {grand['agent_time']:.0f}s")
    print(f"tokens: {grand['tokens']}   cost: ¥{grand['cost']:.4f}")
    print(f"[disk ] before: {df_start}")
    print(f"[disk ] after : {df_end}")
    if grand["driver_issues"]:
        print(f"DRIVER ISSUES ({len(grand['driver_issues'])}):")
        for x in grand["driver_issues"]:
            print(f"  ! {x}")
    else:
        print("driver issues: none")
    print(f"aggregate: {aggregate_path}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Batched SWE-bench Mini driver for CoreCoder.")
    p.add_argument("-i", "--instance_ids", nargs="+", default=None,
                   help="explicit instance ids (overrides --all)")
    p.add_argument("--all", action="store_true", help="run all 50 instances")
    p.add_argument("--subset", action="store_true",
                   help="run the dev subset (core 9 from eval/dev_subset.json) into a "
                        "separate result namespace (_subset_results.jsonl)")
    p.add_argument("--include-optional", action="store_true",
                   help="with --subset, also run optional_stress tasks")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--agent-concurrency", type=int, default=2)
    p.add_argument("--timeout", type=float, default=900.0,
                   help="per-task agent wall-time (s)")
    p.add_argument("--max-workers", type=int, default=4, help="harness workers")
    p.add_argument("--with-hints", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore resume state")
    # --- Reviewer self-check loop (forwarded to each run_swebench.py subprocess) ---
    p.add_argument("--reviewer", action="store_true",
                   default=os.environ.get("CORECODER_REVIEWER", "") not in ("", "0"),
                   help="enable the Planner-Executor-Reviewer self-check loop per "
                        "instance (also via env CORECODER_REVIEWER=1). With --subset, "
                        "results go to the separate _subset_rev namespace.")
    p.add_argument("--review-max-rounds", type=int, default=2,
                   help="max review->revise iterations per instance (default 2)")
    p.add_argument("--reviewer-token-budget", type=int, default=1_500_000,
                   help="token ceiling for the whole review stage (default 1.5M; "
                        "high enough for a revise round to actually finish)")
    p.add_argument("--verify-timeout", type=int, default=120,
                   help="per-run timeout for the reviewer's verify.sh (default 120s)")
    # --- Planner phase (forwarded to each run_swebench.py subprocess) ---
    p.add_argument("--planner", action="store_true",
                   default=os.environ.get("CORECODER_PLANNER", "") not in ("", "0"),
                   help="enable the strong-model Planner pass before the Executor per "
                        "instance (also via env CORECODER_PLANNER=1). With --subset, "
                        "results go to the _subset_plan (or _subset_plan_rev) namespace.")
    p.add_argument("--planner-model", type=str, default=rs.DEFAULT_PLANNER_MODEL,
                   help=f"model for the Planner pass (default {rs.DEFAULT_PLANNER_MODEL}; "
                        f"the Executor stays on {rs.DEFAULT_EVAL_MODEL})")
    p.add_argument("--planner-max-rounds", type=int, default=20,
                   help="hard cap on Planner tool-call rounds (default 20)")
    p.add_argument("--planner-token-budget", type=int, default=600_000,
                   help="advisory Planner token budget, logged when exceeded "
                        "(hard bound is --planner-max-rounds; default 600k)")
    # --- Multi-layer context compression (forwarded to each subprocess) ---
    p.add_argument("--compress", action="store_true",
                   default=os.environ.get("CORECODER_COMPRESS", "") not in ("", "0"),
                   help="enable the instrumented multi-layer context compression per "
                        "instance (also via env CORECODER_COMPRESS=1). With --subset, "
                        "results go to a `_comp`-suffixed namespace. Off -> the basic "
                        "always-on ContextManager (baseline) is used unchanged.")
    p.add_argument("--compress-snip-at", type=float, default=None,
                   help="Layer-1 (tool-output trim) trip ratio (default 0.55)")
    p.add_argument("--compress-summarize-at", type=float, default=None,
                   help="Layer-2 (LLM summary) trip ratio (default 0.72)")
    p.add_argument("--compress-collapse-at", type=float, default=None,
                   help="Layer-3 (structured archive) trip ratio (default 0.88)")
    p.add_argument("--compress-keep-recent", type=int, default=None,
                   help="turns kept verbatim by Layer-2 summary (default 8)")
    # --- MCP tool server (forwarded to each subprocess) ---
    p.add_argument("--mcp", action="store_true",
                   default=os.environ.get("CORECODER_MCP", "") not in ("", "0"),
                   help="serve read_file/grep from a standalone MCP server per instance "
                        "and have the agent discover+call them over MCP (also via env "
                        "CORECODER_MCP=1). With --subset, results go to a `_mcp`-suffixed "
                        "namespace. Off -> in-process tools (baseline) are used unchanged.")
    p.add_argument("--mcp-startup-timeout", type=float, default=None,
                   help="seconds to wait for the MCP server handshake (default 30)")
    p.add_argument("--mcp-call-timeout", type=float, default=None,
                   help="per-call MCP tool timeout before builtin fallback (default 60)")
    args = p.parse_args(argv)

    review_cfg = {
        "max_rounds": args.review_max_rounds,
        "token_budget": args.reviewer_token_budget,
        "verify_timeout": args.verify_timeout,
    }
    plan_cfg = {
        "model": args.planner_model,
        "max_rounds": args.planner_max_rounds,
        "token_budget": args.planner_token_budget,
    }
    compress_cfg = {}
    if args.compress_snip_at is not None:
        compress_cfg["snip_at"] = args.compress_snip_at
    if args.compress_summarize_at is not None:
        compress_cfg["summarize_at"] = args.compress_summarize_at
    if args.compress_collapse_at is not None:
        compress_cfg["collapse_at"] = args.compress_collapse_at
    if args.compress_keep_recent is not None:
        compress_cfg["keep_recent"] = args.compress_keep_recent

    mcp_cfg = {}
    if args.mcp_startup_timeout is not None:
        mcp_cfg["startup_timeout"] = args.mcp_startup_timeout
    if args.mcp_call_timeout is not None:
        mcp_cfg["call_timeout"] = args.mcp_call_timeout

    ds = rs.load_data()
    all_ids = [row["instance_id"] for row in ds]

    # subset runs go to their own result namespace (won't be skipped by full-50 resume)
    results_path, aggregate_path, run_id_prefix = (
        RESULTS_PATH, AGGREGATE_PATH, RUN_ID_PREFIX)
    if args.subset:
        targets = load_subset_ids(include_optional=args.include_optional)
        # Ablation namespaces are COMPOSED from the active flags so any combo of
        # planner / reviewer / compress lands in its own resumable file and never
        # overwrites another arm. Fixed order (plan, rev, comp) keeps the existing
        # names byte-identical for back-compat: ""/_rev/_plan/_plan_rev, with
        # _comp / _plan_comp / _rev_comp / _plan_rev_comp added on top.
        suffix = ablation_suffix(args)
        results_path = RUNS_DIR / f"_subset{suffix}_results.jsonl"
        aggregate_path = RUNS_DIR / f"_subset{suffix}_aggregate.json"
        run_id_prefix = f"corecoder_subset{suffix}"
        print(f"[subset] {len(targets)} tasks from {DEV_SUBSET_PATH.name} "
              f"(optional={'on' if args.include_optional else 'off'}, "
              f"planner={(args.planner_model if args.planner else 'off')}, "
              f"reviewer={'on' if args.reviewer else 'off'}, "
              f"compress={'on' if args.compress else 'off'}, "
              f"mcp={'on' if args.mcp else 'off'}) -> "
              f"namespace _subset{suffix}")
    elif args.instance_ids:
        targets = args.instance_ids
    elif args.all:
        targets = all_ids
    else:
        p.error("specify --subset, --all, or -i <ids...>")

    # Full / single-instance runs (NOT --subset): tag the namespace by the
    # EXECUTOR model and active ablation flags so full-50 arms never overwrite
    # each other. Defaults keep the bare _swebench names; examples:
    #   _swebench_results.jsonl
    #   _swebench_pro_results.jsonl
    #   _swebench_plan_rev_results.jsonl
    #   _swebench_pro_plan_rev_results.jsonl
    if not args.subset:
        exec_model = rs.DEFAULT_EVAL_MODEL
        default_exec = os.environ.get("CORECODER_DEFAULT_EXECUTOR_MODEL", "mimo-v2.5")
        model_suffix = "" if exec_model == default_exec else "_" + exec_model.split("-")[-1]
        suffix = model_suffix + ablation_suffix(args)
        if suffix:
            results_path = RUNS_DIR / f"_swebench{suffix}_results.jsonl"
            aggregate_path = RUNS_DIR / f"_swebench{suffix}_aggregate.json"
            run_id_prefix = f"corecoder_full{suffix}"
        print(f"[full] executor model={exec_model} -> namespace "
              f"{results_path.name} (run_id prefix {run_id_prefix})")

    unknown = [t for t in targets if t not in set(all_ids)]
    if unknown:
        p.error(f"unknown instance ids: {unknown}")

    run_driver(targets, ds, args.batch_size, args.agent_concurrency,
               args.timeout, args.max_workers, args.with_hints, args.force,
               results_path=results_path, aggregate_path=aggregate_path,
               run_id_prefix=run_id_prefix,
               reviewer=args.reviewer, review_cfg=review_cfg,
               planner=args.planner, plan_cfg=plan_cfg,
               compress=args.compress, compress_cfg=compress_cfg,
               mcp=args.mcp, mcp_cfg=mcp_cfg)


if __name__ == "__main__":
    main()
