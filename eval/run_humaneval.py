"""HumanEval evaluation pipeline for CoreCoder agent.

Design:
- Agent solves each task in an isolated workdir; it ONLY sees the HumanEval
  `prompt` field (signature + docstring), never the `test` field.
- Agent must write its solution to `solution.py` in that workdir.
- Judging happens in a SEPARATE `judge/` dir: solution.py is copied there,
  appended with the official `test` field and a `check(entry_point)` call,
  and executed in a subprocess with a hard timeout.
- Per-task hard timeout on the agent itself via SIGALRM, so a hung problem
  cannot stall the whole batch.

Usage:
    python eval/run_humaneval.py            # run problem 0 (verbose, debug)
    python eval/run_humaneval.py 5          # run problem 5 (verbose)
    python eval/run_humaneval.py --all      # run all 164, one line per task
    python eval/run_humaneval.py --all --timeout 180
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# HF mirror fallback (covers non-interactive shells where ~/.bashrc isn't sourced)
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from datasets import load_dataset

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.llm import LLM
from corecoder.tools import bash as bash_tool

EVAL_ROOT = REPO_ROOT / "eval"
RUNS_DIR = Path(os.environ.get("CORECODER_RUNS_DIR", EVAL_ROOT / "runs_mimo"))
DEFAULT_EXEC_TIMEOUT_S = 10.0       # judge subprocess hard limit
DEFAULT_AGENT_TIMEOUT_S = 300.0     # per-task agent wall-time hard limit
DEFAULT_EVAL_MODEL = os.environ.get("CORECODER_MODEL", "mimo-v2.5")


class AgentTimeoutError(Exception):
    """Raised by SIGALRM when an agent.chat() exceeds the per-task budget."""


def _alarm_handler(signum, frame):
    raise AgentTimeoutError(f"agent exceeded per-task timeout")


AGENT_PROMPT_TEMPLATE = """You are solving a coding task. You are operating inside an empty, isolated working directory (see "Working directory" in the system prompt). Write a single file named `solution.py` IN THE CURRENT WORKING DIRECTORY containing a complete Python implementation of the function specified below.

Constraints:
- Use the relative path `solution.py` (do NOT use absolute paths or navigate to other directories).
- `solution.py` MUST include the same imports and the EXACT function signature shown below.
- Implement the function body so it satisfies the docstring (including any examples shown there).
- The file is self-contained Python; no external dependencies beyond the standard library.
- You may use the bash tool to test your own code, but do not `cd` out of the current directory.
- When you are confident `solution.py` is correct, stop.

Function specification:

```python
{prompt}
```
"""


def load_humaneval():
    return load_dataset(
        "openai/openai_humaneval",
        split="test",
        download_mode="reuse_cache_if_exists",
    )


def setup_dirs(task_id: str) -> tuple[Path, Path]:
    safe_id = task_id.replace("/", "_")
    base = RUNS_DIR / safe_id
    if base.exists():
        shutil.rmtree(base)
    work = base / "work"
    judge_dir = base / "judge"
    work.mkdir(parents=True)
    judge_dir.mkdir(parents=True)
    return work, judge_dir


def run_agent(workdir: Path, agent_prompt: str, transcript_path: Path,
              agent_timeout_s: float) -> dict:
    config = Config.from_env()
    if not config.api_key:
        raise RuntimeError("No API key. Check .env at repo root.")
    config.model = DEFAULT_EVAL_MODEL

    llm = LLM(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )

    transcript: list = []

    def on_token(tok: str):
        transcript.append({"kind": "token", "text": tok})

    def on_tool(name: str, kwargs: dict):
        transcript.append({"kind": "tool", "name": name, "args": kwargs})

    bash_tool._cwd = None
    cwd_before = os.getcwd()
    os.chdir(workdir)
    # Instantiate Agent AFTER chdir — system_prompt() bakes cwd into the system
    # message at construction time.
    agent = Agent(llm=llm, max_context_tokens=config.max_context_tokens)

    # per-task hard timeout
    prev_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(int(agent_timeout_s))

    t0 = time.monotonic()
    final_text = ""
    error = None
    timed_out = False
    try:
        final_text = agent.chat(agent_prompt, on_token=on_token, on_tool=on_tool)
    except AgentTimeoutError:
        timed_out = True
        error = f"agent timed out after {agent_timeout_s}s"
        final_text = "(timed out)"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        final_text = f"(agent error: {error})"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev_handler)
        os.chdir(cwd_before)
        bash_tool._cwd = None

    elapsed = time.monotonic() - t0

    with transcript_path.open("w") as f:
        for entry in transcript:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    return {
        "final_text": final_text,
        "elapsed_s": elapsed,
        "prompt_tokens": llm.total_prompt_tokens,
        "completion_tokens": llm.total_completion_tokens,
        "estimated_cost": llm.estimated_cost,
        "tool_calls": [t for t in transcript if t["kind"] == "tool"],
        "model": config.model,
        "timed_out": timed_out,
        "error": error,
    }


def judge(workdir: Path, judge_dir: Path, official_test: str, entry_point: str,
          timeout_s: float = DEFAULT_EXEC_TIMEOUT_S) -> dict:
    solution_src = workdir / "solution.py"
    if not solution_src.exists():
        return {"status": "fail", "reason": "no solution.py produced"}

    solution_code = solution_src.read_text()
    runner_code = (
        solution_code
        + "\n\n# --- injected by judge ---\n"
        + official_test
        + f"\n\ncheck({entry_point})\n"
    )
    runner_path = judge_dir / "runner.py"
    runner_path.write_text(runner_code)
    shutil.copy(solution_src, judge_dir / "solution.py")

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(runner_path)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(judge_dir),
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "fail",
            "reason": f"judge timeout after {timeout_s}s",
            "elapsed_s": time.monotonic() - t0,
        }

    elapsed = time.monotonic() - t0
    if proc.returncode == 0:
        return {"status": "pass", "elapsed_s": elapsed}
    return {
        "status": "fail",
        "reason": "non-zero exit",
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
        "elapsed_s": elapsed,
    }


def run_one(task: dict, agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S) -> dict:
    """Run one task end-to-end. Never raises — every failure becomes a summary."""
    work, judge_dir = setup_dirs(task["task_id"])
    transcript_path = work.parent / "transcript.jsonl"

    agent_prompt = AGENT_PROMPT_TEMPLATE.format(prompt=task["prompt"])
    try:
        agent_result = run_agent(work, agent_prompt, transcript_path, agent_timeout_s)
    except Exception as e:
        agent_result = {
            "final_text": f"(setup error)",
            "elapsed_s": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "estimated_cost": 0.0,
            "tool_calls": [],
            "model": DEFAULT_EVAL_MODEL,
            "timed_out": False,
            "error": f"setup-error: {type(e).__name__}: {e}",
        }

    if agent_result.get("error"):
        j = {"status": "fail", "reason": agent_result["error"]}
    else:
        j = judge(work, judge_dir, task["test"], task["entry_point"])

    summary = {
        "task_id": task["task_id"],
        "entry_point": task["entry_point"],
        "judge": j,
        "agent": {
            "model": agent_result["model"],
            "elapsed_s": agent_result["elapsed_s"],
            "prompt_tokens": agent_result["prompt_tokens"],
            "completion_tokens": agent_result["completion_tokens"],
            "estimated_cost_cny": agent_result["estimated_cost"],
            "tool_call_count": len(agent_result["tool_calls"]),
            "timed_out": agent_result.get("timed_out", False),
            "error": agent_result.get("error"),
        },
    }
    (work.parent / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )
    return summary, agent_result


def _print_single_verbose(task: dict, summary: dict, agent_result: dict, work: Path):
    print(f"\n=== {task['task_id']}  entry_point={task['entry_point']} ===")
    print(f"workdir: {work}")
    print(f"\n--- agent finished ---")
    print(f"model: {agent_result['model']}")
    print(f"elapsed: {agent_result['elapsed_s']:.2f}s")
    print(f"tokens: prompt={agent_result['prompt_tokens']}  "
          f"completion={agent_result['completion_tokens']}  "
          f"total={agent_result['prompt_tokens'] + agent_result['completion_tokens']}")
    if agent_result["estimated_cost"] is not None:
        print(f"cost: ~¥{agent_result['estimated_cost']:.4f}")
    print(f"tool calls: {len(agent_result['tool_calls'])}")
    for entry in agent_result["tool_calls"]:
        kw = entry["args"]
        brief = ", ".join(f"{k}={repr(v)[:50]}" for k, v in kw.items())
        if len(brief) > 140:
            brief = brief[:140] + "..."
        print(f"  > {entry['name']}({brief})")
    solution_path = work / "solution.py"
    print(f"\n--- solution.py ---")
    print(solution_path.read_text() if solution_path.exists() else "(NOT PRODUCED)")
    print(f"\n--- judge result ---")
    print(json.dumps(summary["judge"], indent=2, ensure_ascii=False))


def run_all(agent_timeout_s: float):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    progress_path = RUNS_DIR / "_progress.jsonl"
    aggregate_path = RUNS_DIR / "_aggregate.json"

    ds = load_humaneval()
    n = len(ds)
    print(f"[batch] running {n} problems, model={DEFAULT_EVAL_MODEL}, "
          f"per-task timeout={agent_timeout_s}s")
    print(f"[batch] progress → {progress_path}")
    print(f"[batch] aggregate → {aggregate_path}")
    print()

    passes = 0
    fails: list[str] = []
    total_prompt_tok = 0
    total_completion_tok = 0
    total_cost = 0.0
    total_agent_time = 0.0
    per_task: list[dict] = []

    progress_f = progress_path.open("w")
    t_batch_start = time.monotonic()

    for i in range(n):
        task = ds[i]
        try:
            summary, _agent_result = run_one(task, agent_timeout_s=agent_timeout_s)
        except Exception as e:
            # ultimate safety net — run_one shouldn't raise but if it does, log + move on
            summary = {
                "task_id": task["task_id"],
                "entry_point": task["entry_point"],
                "judge": {"status": "fail", "reason": f"runner-crashed: {e!r}"},
                "agent": {
                    "model": DEFAULT_EVAL_MODEL,
                    "elapsed_s": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "estimated_cost_cny": 0.0,
                    "tool_call_count": 0,
                    "timed_out": False,
                    "error": f"runner-crashed: {e!r}",
                },
            }

        passed = summary["judge"]["status"] == "pass"
        if passed:
            passes += 1
        else:
            fails.append(summary["task_id"])

        pt = summary["agent"]["prompt_tokens"]
        ct = summary["agent"]["completion_tokens"]
        cost = summary["agent"]["estimated_cost_cny"] or 0.0
        total_prompt_tok += pt
        total_completion_tok += ct
        total_cost += cost
        total_agent_time += summary["agent"]["elapsed_s"]

        status = "PASS" if passed else "FAIL"
        reason = summary["judge"].get("reason") or ""
        reason_tail = f" ({reason[:60]})" if (not passed and reason) else ""

        line = (
            f"[{i+1:3d}/{n}] {summary['task_id']:<14} {status}  "
            f"tok={pt}+{ct}  agent={summary['agent']['elapsed_s']:5.1f}s  "
            f"cost=¥{cost:.4f}  pass@1={passes/(i+1):.3f}{reason_tail}"
        )
        print(line, flush=True)

        prog_entry = {
            "i": i + 1,
            "task_id": summary["task_id"],
            "status": status,
            "elapsed_s": summary["agent"]["elapsed_s"],
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cost_cny": cost,
            "pass_at_1_running": passes / (i + 1),
            "judge_reason": summary["judge"].get("reason"),
            "agent_error": summary["agent"].get("error"),
        }
        progress_f.write(json.dumps(prog_entry, ensure_ascii=False) + "\n")
        progress_f.flush()
        per_task.append({
            "task_id": summary["task_id"],
            "status": status,
            **summary["agent"],
            "judge_reason": summary["judge"].get("reason"),
        })

    progress_f.close()
    wall = time.monotonic() - t_batch_start

    aggregate = {
        "model": DEFAULT_EVAL_MODEL,
        "total_problems": n,
        "passes": passes,
        "fails": len(fails),
        "pass_at_1": passes / n,
        "fail_task_ids": fails,
        "wall_time_s": wall,
        "total_prompt_tokens": total_prompt_tok,
        "total_completion_tokens": total_completion_tok,
        "total_tokens": total_prompt_tok + total_completion_tok,
        "total_cost_cny": total_cost,
        "total_agent_time_s": total_agent_time,
        "avg_agent_time_s": total_agent_time / n,
        "per_task_timeout_s": agent_timeout_s,
        "judge_exec_timeout_s": DEFAULT_EXEC_TIMEOUT_S,
        "started_at_unix": t_batch_start,
        "per_task": per_task,
    }
    aggregate_path.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False))

    print()
    print("=" * 70)
    print(f"DONE  pass@1 = {passes}/{n} = {passes/n:.4f}")
    print(f"wall: {wall:.1f}s ({wall/60:.1f} min)   "
          f"avg agent/task: {total_agent_time/n:.2f}s")
    print(f"tokens: prompt={total_prompt_tok} completion={total_completion_tok} "
          f"sum={total_prompt_tok + total_completion_tok}")
    print(f"cost: ¥{total_cost:.4f}")
    print(f"fails ({len(fails)}): {fails}")
    print(f"aggregate written: {aggregate_path}")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="HumanEval eval for CoreCoder agent.")
    parser.add_argument("idx", nargs="?", type=int, default=None,
                        help="problem index for single-task mode (default 0)")
    parser.add_argument("--all", action="store_true",
                        help="run all 164 problems with per-line progress")
    parser.add_argument("--timeout", type=float, default=DEFAULT_AGENT_TIMEOUT_S,
                        help=f"per-task agent timeout in seconds "
                             f"(default {DEFAULT_AGENT_TIMEOUT_S})")
    args = parser.parse_args(argv)

    if args.all:
        run_all(args.timeout)
        return

    idx = args.idx if args.idx is not None else 0
    ds = load_humaneval()
    print(f"loaded {len(ds)} problems, running idx={idx} ({ds[idx]['task_id']})")
    task = ds[idx]
    summary, agent_result = run_one(task, agent_timeout_s=args.timeout)
    work = RUNS_DIR / task["task_id"].replace("/", "_") / "work"
    _print_single_verbose(task, summary, agent_result, work)
    print(f"\n=== final ===  {summary['task_id']}: {summary['judge']['status'].upper()}")


if __name__ == "__main__":
    main()
