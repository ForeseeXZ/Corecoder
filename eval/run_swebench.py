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
RUNS_DIR = Path(os.environ.get("CORECODER_RUNS_DIR", EVAL_ROOT / "runs_mimo"))

DATASET = "MariusHobbhahn/swe-bench-verified-mini"
SPLIT = "test"
DEFAULT_AGENT_TIMEOUT_S = 900.0     # per-task agent wall-time hard limit
SWE_MAX_TOKENS = 16384              # SWE-bench patches need long output
DEFAULT_EVAL_MODEL = os.environ.get("CORECODER_MODEL", "mimo-v2.5")
MODEL_NAME_OR_PATH = f"corecoder-{DEFAULT_EVAL_MODEL}"
# Planner uses a STRONG model (planning needs reasoning); Executor stays on the
# fast model above. Override with CORECODER_PLANNER_MODEL / --planner-model.
DEFAULT_PLANNER_MODEL = os.environ.get("CORECODER_PLANNER_MODEL", "mimo-v2.5-pro")

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
              agent_timeout_s: float, *, reviewer: bool = False,
              review_cfg: dict | None = None, repo: str | None = None,
              problem_statement: str | None = None,
              planner: bool = False, plan_cfg: dict | None = None,
              compress: bool = False, compress_cfg: dict | None = None,
              mcp: bool = False, mcp_cfg: dict | None = None) -> dict:
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
    phase = {"name": "executor"}  # tags transcript entries executor vs reviewer

    def on_token(tok: str):
        transcript.append({"kind": "token", "text": tok, "phase": phase["name"]})

    def on_tool(name: str, kwargs: dict):
        transcript.append({"kind": "tool", "name": name, "args": kwargs,
                           "phase": phase["name"]})

    def review_log(msg: str):
        transcript.append({"kind": "log", "text": msg, "phase": phase["name"]})
        print(msg, flush=True)

    bash_tool._cwd = None
    cwd_before = os.getcwd()
    os.chdir(repo_dir)
    # Instantiate Agent AFTER chdir — system_prompt() bakes cwd into the system
    # message at construction time (so the agent "sees" the repo as its workdir).
    # compress=False -> baseline ContextManager path is byte-identical to before.
    # mcp=False -> in-process tools, baseline path unchanged. When on, the MCP
    # server subprocess inherits THIS cwd (the repo checkout) so its read/grep
    # resolve paths exactly like the in-process tools.
    _mcp_cfg = dict(mcp_cfg or {})
    _mcp_cfg.setdefault("cwd", str(repo_dir))
    agent = Agent(llm=llm, max_context_tokens=config.max_context_tokens,
                  compress=compress, compress_cfg=compress_cfg,
                  mcp=mcp, mcp_cfg=_mcp_cfg)

    prev_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(int(agent_timeout_s))

    t0 = time.monotonic()
    final_text = ""
    error = None
    timed_out = False
    review_meta = None
    plan_meta = None
    try:
        exec_prompt = agent_prompt
        # --- Planner phase (only when enabled; runs BEFORE the Executor) ---
        # A strong-model, read-only planning pass produces a structured repair
        # plan that is appended to the Executor's prompt. Baseline path untouched.
        if planner:
            from corecoder.plan import run_plan_phase
            pcfg = plan_cfg or {}
            phase["name"] = "planner"
            plan_block, plan_meta = run_plan_phase(
                config=config, repo_dir=repo_dir, repo=repo or "",
                problem_statement=problem_statement or "",
                model=pcfg.get("model", DEFAULT_PLANNER_MODEL),
                max_rounds=pcfg.get("max_rounds", 20),
                token_budget=pcfg.get("token_budget", 600_000),
                max_tokens=SWE_MAX_TOKENS,
                max_context_tokens=config.max_context_tokens,
                on_token=on_token, on_tool=on_tool, log=review_log,
            )
            phase["name"] = "executor"
            if plan_block:
                exec_prompt = agent_prompt + plan_block

        final_text = agent.chat(exec_prompt, on_token=on_token, on_tool=on_tool)
        # --- Reviewer self-check loop (only when enabled; baseline path untouched) ---
        if reviewer:
            from corecoder.review import run_review_loop
            cfg = review_cfg or {}
            phase["name"] = "reviewer"
            review_meta = run_review_loop(
                agent=agent, llm=llm, repo_dir=repo_dir, repo=repo or "",
                problem_statement=problem_statement or "",
                max_rounds=cfg.get("max_rounds", 2),
                token_budget=cfg.get("token_budget", 400_000),
                verify_timeout=cfg.get("verify_timeout", 120),
                on_token=on_token, on_tool=on_tool, log=review_log,
            )
    except AgentTimeoutError:
        timed_out = True
        error = f"agent timed out after {agent_timeout_s}s"
        final_text = final_text or "(timed out)"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        final_text = final_text or f"(agent error: {error})"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev_handler)
        # belt-and-suspenders: never let a verification scratch dir leak into the
        # extracted patch, even if the review loop was interrupted mid-flight.
        try:
            import shutil as _sh
            from corecoder.review import SCRATCH as _SCRATCH
            _scratch = Path(repo_dir) / _SCRATCH
            if _scratch.exists():
                _sh.rmtree(_scratch, ignore_errors=True)
        except Exception:
            pass
        # shut down the MCP server subprocess (if any) so no zombie is left —
        # no-op when mcp is off.
        try:
            agent.close()
        except Exception:
            pass
        os.chdir(cwd_before)
        bash_tool._cwd = None

    elapsed = time.monotonic() - t0

    # compression metadata (only present when --compress; the CompressionManager
    # carries a .stats dict, the baseline ContextManager does not).
    compress_meta = getattr(agent.context, "stats", None) if compress else None
    # MCP metadata: the bridge's stats dict (server up? discovered tools? how many
    # calls went over MCP vs fell back to builtin). None when mcp is off.
    mcp_meta = getattr(agent, "mcp_stats", None) if mcp else None

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
        "review": review_meta,
        "plan": plan_meta,
        "compress": compress_meta,
        "mcp": mcp_meta,
    }


def run_one(instance: dict, agent_timeout_s: float, with_hints: bool,
            reviewer: bool = False, review_cfg: dict | None = None,
            planner: bool = False, plan_cfg: dict | None = None,
            compress: bool = False, compress_cfg: dict | None = None,
            mcp: bool = False, mcp_cfg: dict | None = None) -> dict:
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

    agent_result = run_agent(
        repo, agent_prompt, transcript_path, agent_timeout_s,
        reviewer=reviewer, review_cfg=review_cfg,
        repo=instance["repo"], problem_statement=instance["problem_statement"],
        planner=planner, plan_cfg=plan_cfg,
        compress=compress, compress_cfg=compress_cfg,
        mcp=mcp, mcp_cfg=mcp_cfg,
    )

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
        "plan": agent_result.get("plan"),
        "review": agent_result.get("review"),
        "compress": agent_result.get("compress"),
        "mcp": agent_result.get("mcp"),
    }
    # convenience: total cost across BOTH models (executor flash + planner pro),
    # since the "agent" block above is executor-only.
    _plan = agent_result.get("plan") or {}
    _exec_cost = agent_result.get("estimated_cost") or 0.0
    _plan_cost = _plan.get("cost_cny") or 0.0
    summary["total_cost_cny"] = _exec_cost + _plan_cost
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
    pl = s.get("plan")
    if pl and pl.get("enabled"):
        print(f"\n--- planner ({pl.get('model')}): "
              f"{pl.get('n_planned_files')} file(s) planned, "
              f"json_ok={pl.get('json_parse_ok')}"
              f"{'' if pl.get('json_parse_ok') else '/fallback='+str(pl.get('json_fallback'))}, "
              f"{pl.get('tool_calls')} tool calls, "
              f"{pl.get('tokens_spent')} tok"
              f"{', BUDGET-OVER' if pl.get('budget_stop') else ''}"
              f"{', ERROR='+str(pl.get('error')) if pl.get('error') else ''} ---")
        print(f"  planned files: {pl.get('planned_files')}")
        if pl.get("plan_text"):
            print(f"  plan:\n{pl['plan_text']}")
    cm = s.get("compress")
    if cm and cm.get("enabled"):
        lc = cm.get("layer_counts", {})
        print(f"\n--- compress: layers fired "
              f"L1={lc.get('1_tool_snip', 0)} "
              f"L2={lc.get('2_summarize', 0)} "
              f"L3={lc.get('3_archive', 0)}  |  "
              f"reclaimed={cm.get('total_reclaimed_tokens', 0)} tok, "
              f"overhead={cm.get('overhead_tokens', 0)} tok "
              f"({cm.get('overhead_llm_calls', 0)} calls)  |  "
              f"peak={cm.get('peak_tokens', 0)} final={cm.get('final_tokens', 0)} "
              f"(cap {cm.get('max_tokens', 0)}) ---")
        for ev in cm.get("events", []):
            print(f"  · {ev['layer']}: {ev['tokens_before']}->{ev['tokens_after']} "
                  f"(-{ev['reclaimed']}) @ratio {ev['ratio_before']}, "
                  f"msgs {ev['n_messages_before']}->{ev['n_messages_after']}")
    mc = s.get("mcp")
    if mc and mc.get("enabled"):
        if mc.get("server_started"):
            print(f"\n--- mcp: server up in {mc.get('startup_s')}s, "
                  f"discovered {mc.get('discovered_tools')}, "
                  f"replaced builtins {mc.get('replaced_builtins')}  |  "
                  f"calls via MCP={mc.get('tool_calls', 0)}, "
                  f"fallback={mc.get('fallback_calls', 0)} "
                  f"(fallback_triggered={mc.get('fallback')}) ---")
        else:
            print(f"\n--- mcp: server FAILED to start "
                  f"({mc.get('error')}) -> ran on builtin tools (fallback) ---")
    rv = s.get("review")
    if rv:
        print(f"\n--- reviewer: {len(rv.get('rounds', []))} round(s), "
              f"final={rv.get('final_decision')}, "
              f"tokens={rv.get('tokens_spent')}"
              f"{', BUDGET-STOP' if rv.get('budget_stop') else ''} ---")
        for r in rv.get("rounds", []):
            print(f"  r{r['round']}: script={r['signal']} "
                  f"(after={r['after_code']},before={r['before_code']}) "
                  f"verdict={r['verdict']} -> {r['decision']}")
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
    # --- Reviewer self-check loop (off by default; baseline path is unchanged) ---
    parser.add_argument("--reviewer", action="store_true",
                        default=os.environ.get("CORECODER_REVIEWER", "") not in ("", "0"),
                        help="enable the Planner-Executor-Reviewer self-check loop "
                             "(also via env CORECODER_REVIEWER=1)")
    parser.add_argument("--review-max-rounds", type=int, default=2,
                        help="max review->revise iterations (default 2)")
    parser.add_argument("--reviewer-token-budget", type=int, default=1_500_000,
                        help="token ceiling for the whole review stage (default 1.5M; "
                             "must be high enough for a revise round to actually finish)")
    parser.add_argument("--verify-timeout", type=int, default=120,
                        help="per-run timeout for the reviewer's verify.sh (default 120s)")
    # --- Planner phase (off by default; baseline path is unchanged) ---
    parser.add_argument("--planner", action="store_true",
                        default=os.environ.get("CORECODER_PLANNER", "") not in ("", "0"),
                        help="enable the strong-model Planner pass before the Executor "
                             "(also via env CORECODER_PLANNER=1)")
    parser.add_argument("--planner-model", type=str, default=DEFAULT_PLANNER_MODEL,
                        help=f"model for the Planner pass (default {DEFAULT_PLANNER_MODEL}; "
                             f"the Executor stays on {DEFAULT_EVAL_MODEL})")
    parser.add_argument("--planner-max-rounds", type=int, default=20,
                        help="hard cap on Planner tool-call rounds (bounds exploration; "
                             "default 20)")
    parser.add_argument("--planner-token-budget", type=int, default=600_000,
                        help="advisory token budget for the Planner pass, logged when "
                             "exceeded (hard bound is --planner-max-rounds; default 600k)")
    # --- Multi-layer context compression (off by default; baseline path unchanged) ---
    parser.add_argument("--compress", action="store_true",
                        default=os.environ.get("CORECODER_COMPRESS", "") not in ("", "0"),
                        help="enable the instrumented multi-layer context compression "
                             "(also via env CORECODER_COMPRESS=1). Off -> the basic "
                             "always-on ContextManager (baseline) is used unchanged.")
    parser.add_argument("--compress-snip-at", type=float, default=None,
                        help="Layer-1 (tool-output trim) trip ratio of max_context "
                             "(default 0.55)")
    parser.add_argument("--compress-summarize-at", type=float, default=None,
                        help="Layer-2 (LLM summary) trip ratio (default 0.72)")
    parser.add_argument("--compress-collapse-at", type=float, default=None,
                        help="Layer-3 (structured archive) trip ratio (default 0.88)")
    parser.add_argument("--compress-keep-recent", type=int, default=None,
                        help="turns kept verbatim by Layer-2 summary (default 8)")
    # --- MCP: tools served by a standalone MCP server (off by default; baseline
    # path is unchanged — agent uses its in-process tools). ---
    parser.add_argument("--mcp", action="store_true",
                        default=os.environ.get("CORECODER_MCP", "") not in ("", "0"),
                        help="decouple read_file/grep into a standalone MCP server and "
                             "have the agent discover+call them over MCP (stdio). Also "
                             "via env CORECODER_MCP=1. Falls back to builtin tools if the "
                             "server can't start or a call fails.")
    parser.add_argument("--mcp-startup-timeout", type=float, default=None,
                        help="seconds to wait for the MCP server handshake before "
                             "falling back to builtins (default 30)")
    parser.add_argument("--mcp-call-timeout", type=float, default=None,
                        help="per-call timeout for an MCP tool before falling back to "
                             "the builtin (default 60)")
    args = parser.parse_args(argv)

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
    # only carry keys the user explicitly set; the CompressionManager supplies
    # sensible defaults for the rest (keeps the cfg small + tunable later).
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

    ds = load_data()
    if args.instance_ids is None:
        targets = [ds[0]["instance_id"]]
    else:
        targets = args.instance_ids
    print(f"[swebench] dataset={DATASET} split={SPLIT} ({len(ds)} instances)")
    print(f"[swebench] running {len(targets)}: {targets}")
    print(f"[swebench] model={DEFAULT_EVAL_MODEL} max_tokens={SWE_MAX_TOKENS} "
          f"timeout={args.timeout}s with_hints={args.with_hints} "
          f"planner={args.planner}"
          + (f" ({args.planner_model}, max_rounds={args.planner_max_rounds}, "
             f"budget={args.planner_token_budget})" if args.planner else "")
          + f" reviewer={args.reviewer}"
          + (f" (max_rounds={args.review_max_rounds}, "
             f"budget={args.reviewer_token_budget}, "
             f"verify_timeout={args.verify_timeout}s)" if args.reviewer else "")
          + f" compress={args.compress}"
          + (f" (cfg={compress_cfg or 'defaults'})" if args.compress else "")
          + f" mcp={args.mcp}"
          + (f" (cfg={mcp_cfg or 'defaults'})" if args.mcp else ""))

    predictions: list[dict] = []
    for instance_id in targets:
        instance = get_instance(ds, instance_id)
        summary, prediction, agent_result = run_one(
            instance, agent_timeout_s=args.timeout, with_hints=args.with_hints,
            reviewer=args.reviewer, review_cfg=review_cfg,
            planner=args.planner, plan_cfg=plan_cfg,
            compress=args.compress, compress_cfg=compress_cfg,
            mcp=args.mcp, mcp_cfg=mcp_cfg)
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
