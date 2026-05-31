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
  - everything lands under eval/runs/

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
RUNS_DIR = EVAL_ROOT / "runs"
RESULTS_PATH = RUNS_DIR / "_swebench_results.jsonl"
AGGREGATE_PATH = RUNS_DIR / "_swebench_aggregate.json"
REPORTS_DIR = RUNS_DIR / "_reports"

RUN_ID_PREFIX = "corecoder_full"


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


def pull_image(image: str, retries: int = 3) -> bool:
    if image_present(image):
        return True
    for attempt in range(1, retries + 1):
        proc = subprocess.run(["docker", "pull", image],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return True
        print(f"      pull attempt {attempt}/{retries} failed: "
              f"{proc.stderr.strip().splitlines()[-1:] or ['?']}", flush=True)
        time.sleep(2 * attempt)
    return False


def load_completed() -> dict:
    """instance_id -> result record, for instances already graded (resumeable)."""
    done = {}
    if RESULTS_PATH.exists():
        for line in RESULTS_PATH.read_text().splitlines():
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


def append_result(rec: dict):
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ----------------------------- agent phase -----------------------------

def run_agent_subprocess(instance_id: str, timeout_s: float, with_hints: bool) -> dict:
    """Run the agent for ONE instance via run_swebench.py in a child process.

    Returns a small dict read back from eval/runs/<id>/{summary,prediction}.json.
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
    else:
        agent, patch_info = {}, {}

    return {
        "instance_id": instance_id,
        "model_patch": model_patch,
        "patch_empty": model_patch.strip() == "",
        "n_files": patch_info.get("n_files"),
        "prompt_tokens": agent.get("prompt_tokens", 0),
        "completion_tokens": agent.get("completion_tokens", 0),
        "cost_cny": agent.get("estimated_cost_cny") or 0.0,
        "agent_elapsed_s": agent.get("elapsed_s", 0.0),
        "agent_error": agent.get("error"),
        "subproc_error": subproc_err,
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
               timeout_s: float, max_workers: int, with_hints: bool, force: bool):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    completed = {} if force else load_completed()
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
        run_id = f"{RUN_ID_PREFIX}_b{bi:02d}"
        images = {iid: rs.instance_image_tag(iid) for iid in batch}
        print(f"\n{'='*72}\n[batch {bi}/{len(batches)}] {batch}\n{'='*72}")

        # --- 1. pull images ---
        print(f"[batch {bi}] pulling {len(batch)} images ...")
        image_ok = {}
        for iid in batch:
            img = images[iid]
            had = image_present(img)
            ok = pull_image(img)
            image_ok[iid] = ok
            tag = "cached" if had else ("pulled " + image_size(img) if ok else "FAILED")
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
                pool.submit(run_agent_subprocess, iid, timeout_s, with_hints): iid
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
                "agent_elapsed_s": r.get("agent_elapsed_s", 0.0),
                "agent_error": r.get("agent_error"),
                "subproc_error": r.get("subproc_error"),
            }
            if resolved is None:
                grand["driver_issues"].append(f"{iid}: no harness verdict")
            append_result(rec)
            if rec["resolved"]:
                grand["resolved"] += 1
            else:
                grand["unresolved"] += 1
            grand["tokens"] += rec["prompt_tokens"] + rec["completion_tokens"]
            grand["cost"] += rec["cost_cny"]
            grand["agent_time"] += rec["agent_elapsed_s"]
            verdict = "RESOLVED" if rec["resolved"] else "unresolved"
            print(f"   = {iid:<32} {verdict}", flush=True)

        # --- 5. clean batch images ---
        clean_batch_images(list(images.values()))
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
    AGGREGATE_PATH.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False))

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
    print(f"aggregate: {AGGREGATE_PATH}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Batched SWE-bench Mini driver for CoreCoder.")
    p.add_argument("-i", "--instance_ids", nargs="+", default=None,
                   help="explicit instance ids (overrides --all)")
    p.add_argument("--all", action="store_true", help="run all 50 instances")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--agent-concurrency", type=int, default=2)
    p.add_argument("--timeout", type=float, default=900.0,
                   help="per-task agent wall-time (s)")
    p.add_argument("--max-workers", type=int, default=4, help="harness workers")
    p.add_argument("--with-hints", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore resume state")
    args = p.parse_args(argv)

    ds = rs.load_data()
    all_ids = [row["instance_id"] for row in ds]
    if args.instance_ids:
        targets = args.instance_ids
    elif args.all:
        targets = all_ids
    else:
        p.error("specify --all or -i <ids...>")

    unknown = [t for t in targets if t not in set(all_ids)]
    if unknown:
        p.error(f"unknown instance ids: {unknown}")

    run_driver(targets, ds, args.batch_size, args.agent_concurrency,
               args.timeout, args.max_workers, args.with_hints, args.force)


if __name__ == "__main__":
    main()
