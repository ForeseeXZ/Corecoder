"""SWE-bench (Verified Mini) evaluation pipeline for CoreCoder agent — "接法 B".

Design (physically isolated from the grader):
- For each task we prepare an INDEPENDENT working checkout of the repo at the
  task's `base_commit`, OUTSIDE any eval container. The agent edits that copy.
- The agent is given ONLY `problem_statement` (optionally `hints_text`).
  It NEVER sees `patch` (gold answer) or `test_patch` (official tests) — this
  isolation is the credibility line for the whole benchmark.
- After the agent finishes we extract its work as a git diff -> `model_patch`.
- We emit SWE-bench prediction records (instance_id / model_name_or_path /
  model_patch) to a predictions.jsonl. The official SWE-bench harness then runs
  that patch in a fresh per-instance container, injects the official tests, and
  decides resolved/unresolved. This script does NOT grade.

Why extract the checkout from the prebuilt instance image instead of cloning:
- We pull that image anyway for grading, so it's zero extra network (relevant
  behind the GFW), and the source tree is byte-identical to the grader's, which
  guarantees the diff applies cleanly. We reset --hard to base_commit because
  the image HEAD sits on a different (env-setup) commit.

Usage:
    # single instance (default = first in dataset, django__django-11790)
    python eval/run_swebench.py
    python eval/run_swebench.py -i django__django-11790
    python eval/run_swebench.py -i django__django-11790 --timeout 900
    # multiple
    python eval/run_swebench.py -i django__django-11790 sphinx-doc__sphinx-7757
    # consolidated predictions go to eval/predictions.jsonl by default
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
RUNS_DIR = EVAL_ROOT / "runs"

DATASET = "MariusHobbhahn/swe-bench-verified-mini"
SPLIT = "test"
DEFAULT_AGENT_TIMEOUT_S = 900.0     # per-task agent wall-time hard limit
SWE_MAX_TOKENS = 16384              # SWE-bench patches need long output
DEFAULT_EVAL_MODEL = os.environ.get("CORECODER_MODEL", "deepseek-v4-flash")
MODEL_NAME_OR_PATH = f"corecoder-{DEFAULT_EVAL_MODEL}"

# files we ask the agent to leave alone (the grader injects its own tests)
NAMESPACE_REPLACE = ("__", "_1776_")  # SWE-bench Docker Hub naming convention


class AgentTimeoutError(Exception):
    """Raised by SIGALRM when an agent.chat() exceeds the per-task budget."""


def _alarm_handler(signum, frame):
    raise AgentTimeoutError("agent exceeded per-task timeout")


AGENT_PROMPT_TEMPLATE = """You are CoreCoder, fixing a real bug in the `{repo}` repository.

The full source code of the repository is in your current working directory — it is a git checkout at the exact base commit for this issue. Explore it with your tools (grep, glob, read), find the root cause, and fix it by editing the SOURCE files in place.

# The issue to fix

{problem_statement}
{hints_block}
# Rules (important)
- Make the MINIMAL change needed to resolve the issue described above.
- Edit SOURCE code only. Do NOT modify, create, or delete any test files — e.g. anything under a `tests/` or `test/` directory, or files named `test_*.py`, `*_test.py`, `tests.py`, or `conftest.py`. The project's own official test suite will be used to verify your fix, and any change you make to tests will be discarded.
- Do not assume you can run the project's test suite as a grader; it may not even be installed in this environment. Rely on reading and understanding the code to produce a correct fix.
- Keep your edits as a clean set of in-place file modifications so they form a valid git diff against the checkout (use the standard repository layout; do not move the project root).
- When you are confident the source fix is complete and correct, stop.
"""


def load_data():
    return load_dataset(DATASET, split=SPLIT)


def get_instance(ds, instance_id: str | None) -> dict:
    if instance_id is None:
        return ds[0]
    for row in ds:
        if row["instance_id"] == instance_id:
            return row
    raise SystemExit(f"instance_id not found in dataset: {instance_id}")


def setup_dirs(instance_id: str) -> tuple[Path, Path]:
    base = RUNS_DIR / instance_id
    if base.exists():
        shutil.rmtree(base)
    repo = base / "repo"
    repo.mkdir(parents=True)
    return base, repo


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def instance_image_tag(instance_id: str) -> str:
    norm = instance_id.replace(*NAMESPACE_REPLACE)
    return f"swebench/sweb.eval.x86_64.{norm}:latest"


def prepare_repo(instance: dict, repo_dir: Path) -> dict:
    """Materialize an independent checkout of the repo at base_commit.

    Source: the prebuilt instance Docker image's /testbed tree. Then hard-reset
    to base_commit (the image HEAD is on a different env-setup commit).
    Returns a dict with prep diagnostics; raises on hard failure.
    """
    instance_id = instance["instance_id"]
    base_commit = instance["base_commit"]
    image = instance_image_tag(instance_id)

    info: dict = {"image": image, "base_commit": base_commit}

    # image must already be present locally (we don't pull big images here)
    inspect = _run(["docker", "image", "inspect", image])
    if inspect.returncode != 0:
        raise RuntimeError(
            f"instance image not found locally: {image}\n"
            f"Pull it first:  docker pull {image}"
        )

    # copy /testbed out of a throwaway container
    cid = _run(["docker", "create", image]).stdout.strip()
    if not cid:
        raise RuntimeError(f"docker create failed for {image}")
    try:
        cp = _run(["docker", "cp", f"{cid}:/testbed/.", str(repo_dir)])
        if cp.returncode != 0:
            raise RuntimeError(f"docker cp failed: {cp.stderr.strip()}")
    finally:
        _run(["docker", "rm", "-f", cid])

    # align to base_commit and clean any image-state cruft
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    reset = _run(["git", "-C", str(repo_dir), "reset", "--hard", base_commit], env=env)
    if reset.returncode != 0:
        raise RuntimeError(
            f"git reset --hard {base_commit} failed: {reset.stderr.strip()}"
        )
    _run(["git", "-C", str(repo_dir), "clean", "-fdx"], env=env)

    head = _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip()
    info["head_after_reset"] = head
    info["head_matches_base"] = (head == base_commit)
    if not info["head_matches_base"]:
        raise RuntimeError(
            f"checkout HEAD {head} != base_commit {base_commit}"
        )
    return info


def extract_model_patch(repo_dir: Path, base_commit: str) -> str:
    """Return the agent's work as a git diff against base_commit.

    `git add -A` then `diff --cached` so new/deleted files are captured too.
    Uses core.fileMode=false to ignore permission-bit noise.
    """
    _run(["git", "-C", str(repo_dir), "add", "-A"])
    diff = _run([
        "git", "-C", str(repo_dir), "-c", "core.fileMode=false",
        "diff", "--cached", base_commit,
    ])
    return diff.stdout


def run_agent(repo_dir: Path, agent_prompt: str, transcript_path: Path,
              agent_timeout_s: float) -> dict:
    config = Config.from_env()
    if not config.api_key:
        raise RuntimeError("No API key. Check .env at repo root.")
    config.model = DEFAULT_EVAL_MODEL

    # max_tokens=16384 set HERE (not in .env) — SWE patches need long output.
    llm = LLM(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=SWE_MAX_TOKENS,
    )

    transcript: list = []

    def on_token(tok: str):
        transcript.append({"kind": "token", "text": tok})

    def on_tool(name: str, kwargs: dict):
        transcript.append({"kind": "tool", "name": name, "args": kwargs})

    bash_tool._cwd = None
    cwd_before = os.getcwd()
    os.chdir(repo_dir)
    # Instantiate Agent AFTER chdir — system_prompt() bakes cwd into the system
    # message at construction time (so the agent "sees" the repo as its workdir).
    agent = Agent(llm=llm, max_context_tokens=config.max_context_tokens)

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


def run_one(instance: dict, agent_timeout_s: float, with_hints: bool) -> dict:
    """Prepare repo, run agent, extract patch, write trace + prediction record."""
    instance_id = instance["instance_id"]
    base_commit = instance["base_commit"]
    base, repo = setup_dirs(instance_id)
    transcript_path = base / "transcript.jsonl"

    # --- isolation line: only problem_statement (+ optional hints) reach the agent ---
    hints = (instance.get("hints_text") or "").strip()
    hints_block = ""
    if with_hints and hints:
        hints_block = f"\n# Additional context / hints\n\n{hints}\n"
    agent_prompt = AGENT_PROMPT_TEMPLATE.format(
        repo=instance["repo"],
        problem_statement=instance["problem_statement"].strip(),
        hints_block=hints_block,
    )

    prep = prepare_repo(instance, repo)

    agent_result = run_agent(repo, agent_prompt, transcript_path, agent_timeout_s)

    model_patch = extract_model_patch(repo, base_commit)
    (base / "patch.diff").write_text(model_patch)

    prediction = {
        "instance_id": instance_id,
        "model_name_or_path": MODEL_NAME_OR_PATH,
        "model_patch": model_patch,
    }
    (base / "prediction.json").write_text(
        json.dumps(prediction, indent=2, ensure_ascii=False)
    )

    patch_lines = model_patch.count("\n")
    files_changed = [
        ln[len("+++ b/"):] for ln in model_patch.splitlines()
        if ln.startswith("+++ b/")
    ]

    summary = {
        "instance_id": instance_id,
        "repo": instance["repo"],
        "base_commit": base_commit,
        "with_hints": with_hints,
        "prep": prep,
        "patch": {
            "empty": model_patch.strip() == "",
            "n_lines": patch_lines,
            "files_changed": files_changed,
            "n_files": len(files_changed),
        },
        "agent": {
            "model": agent_result["model"],
            "elapsed_s": agent_result["elapsed_s"],
            "prompt_tokens": agent_result["prompt_tokens"],
            "completion_tokens": agent_result["completion_tokens"],
            "total_tokens": agent_result["prompt_tokens"] + agent_result["completion_tokens"],
            "estimated_cost_cny": agent_result["estimated_cost"],
            "tool_call_count": len(agent_result["tool_calls"]),
            "timed_out": agent_result.get("timed_out", False),
            "error": agent_result.get("error"),
            "final_text": agent_result["final_text"],
        },
    }
    (base / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )
    return summary, prediction, agent_result


def _print_verbose(summary: dict, prediction: dict, agent_result: dict, base: Path):
    s = summary
    print(f"\n=== {s['instance_id']}  ({s['repo']} @ {s['base_commit'][:12]}) ===")
    print(f"workdir: {base / 'repo'}")
    print(f"prep: image={s['prep']['image']}")
    print(f"      HEAD aligned to base_commit: {s['prep']['head_matches_base']}")
    print(f"\n--- agent finished ---")
    a = s["agent"]
    print(f"model: {a['model']}   timed_out={a['timed_out']}   error={a['error']}")
    print(f"elapsed: {a['elapsed_s']:.1f}s")
    print(f"tokens: prompt={a['prompt_tokens']} completion={a['completion_tokens']} "
          f"total={a['total_tokens']}")
    if a["estimated_cost_cny"] is not None:
        print(f"cost: ~¥{a['estimated_cost_cny']:.4f}")
    print(f"tool calls: {a['tool_call_count']}")
    for entry in agent_result["tool_calls"]:
        kw = entry["args"]
        brief = ", ".join(f"{k}={repr(v)[:60]}" for k, v in kw.items())
        if len(brief) > 160:
            brief = brief[:160] + "..."
        print(f"  > {entry['name']}({brief})")
    print(f"\n--- model_patch ({s['patch']['n_files']} files, "
          f"{s['patch']['n_lines']} lines) ---")
    print(prediction["model_patch"] or "(EMPTY PATCH)")


def write_predictions(predictions: list[dict], out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for p in predictions:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"\npredictions ({len(predictions)}) written: {out_path}")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="SWE-bench Verified Mini eval (接法 B) for CoreCoder agent.")
    parser.add_argument("-i", "--instance_ids", nargs="+", default=None,
                        help="instance ids to run (default: first in dataset)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_AGENT_TIMEOUT_S,
                        help=f"per-task agent wall-time timeout "
                             f"(default {DEFAULT_AGENT_TIMEOUT_S}s)")
    parser.add_argument("--with-hints", action="store_true",
                        help="also feed hints_text to the agent "
                             "(off by default for credibility)")
    parser.add_argument("--out", type=Path, default=EVAL_ROOT / "predictions.jsonl",
                        help="consolidated predictions.jsonl path")
    args = parser.parse_args(argv)

    ds = load_data()
    if args.instance_ids is None:
        targets = [ds[0]["instance_id"]]
    else:
        targets = args.instance_ids
    print(f"[swebench] dataset={DATASET} split={SPLIT} ({len(ds)} instances)")
    print(f"[swebench] running {len(targets)}: {targets}")
    print(f"[swebench] model={DEFAULT_EVAL_MODEL} max_tokens={SWE_MAX_TOKENS} "
          f"timeout={args.timeout}s with_hints={args.with_hints}")

    predictions: list[dict] = []
    for instance_id in targets:
        instance = get_instance(ds, instance_id)
        summary, prediction, agent_result = run_one(
            instance, agent_timeout_s=args.timeout, with_hints=args.with_hints)
        base = RUNS_DIR / instance_id
        _print_verbose(summary, prediction, agent_result, base)
        predictions.append(prediction)

    write_predictions(predictions, args.out)
    print("\n=== done ===")
    for p in predictions:
        empty = "EMPTY" if not p["model_patch"].strip() else "has-patch"
        print(f"  {p['instance_id']}: {empty}")


if __name__ == "__main__":
    main()
