"""Render GitHub MCP experiment reports from local CodePilot results.

The report body is designed for GitHub issue/comment publishing through a
configured GitHub MCP client.  Keeping the renderer separate from the transport
lets evaluation runs produce stable, reviewable reports before any credentialed
GitHub write is attempted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def aggregate_from_results(rows: list[dict]) -> dict:
    total = len(rows)
    resolved = sum(1 for r in rows if bool(r.get("resolved")))
    patch_empty = sum(1 for r in rows if bool(r.get("patch_empty")))
    prompt_tokens = sum(int(r.get("prompt_tokens") or 0) for r in rows)
    completion_tokens = sum(int(r.get("completion_tokens") or 0) for r in rows)
    return {
        "total": total,
        "resolved": resolved,
        "patch_empty": patch_empty,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def normalize_aggregate(data: dict) -> dict:
    total = (
        data.get("total")
        or data.get("n")
        or data.get("num_instances")
        or data.get("count")
        or 0
    )
    resolved = data.get("resolved", data.get("passed", data.get("success", 0)))
    patch_empty = data.get("patch_empty", data.get("empty_patch", 0))
    prompt_tokens = data.get("prompt_tokens", data.get("total_prompt_tokens", 0))
    completion_tokens = data.get(
        "completion_tokens", data.get("total_completion_tokens", 0)
    )
    return {
        "total": int(total or 0),
        "resolved": int(resolved or 0),
        "patch_empty": int(patch_empty or 0),
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
    }


def pct(num: int, den: int) -> str:
    if den <= 0:
        return "n/a"
    return f"{num / den:.1%}"


def render_markdown(
    *,
    run_name: str,
    model: str,
    mode: str,
    metrics: dict,
    artifact: str | None,
    notes: str | None,
) -> str:
    total = metrics["total"]
    resolved = metrics["resolved"]
    token_total = metrics["prompt_tokens"] + metrics["completion_tokens"]

    lines = [
        f"## CodePilot Experiment Report: {run_name}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Model | `{model}` |",
        f"| Mode | `{mode}` |",
        f"| Resolved | `{resolved}/{total}` ({pct(resolved, total)}) |",
        f"| Empty patches | `{metrics['patch_empty']}` |",
        f"| Prompt tokens | `{metrics['prompt_tokens']:,}` |",
        f"| Completion tokens | `{metrics['completion_tokens']:,}` |",
        f"| Total tokens | `{token_total:,}` |",
    ]
    if artifact:
        lines.append(f"| Artifact | `{artifact}` |")
    if notes:
        lines.extend(["", "### Notes", "", notes])
    lines.extend(
        [
            "",
            "### GitHub MCP Publish Target",
            "",
            "This Markdown body is compatible with GitHub MCP issue/comment "
            "publishing. The report renderer stays transport-independent so "
            "metrics can be inspected before a credentialed GitHub write.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a GitHub MCP experiment report."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--aggregate", type=Path, help="Path to aggregate JSON")
    group.add_argument("--results", type=Path, help="Path to results JSONL")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--model", default="mimo-v2.5")
    parser.add_argument("--mode", default="baseline")
    parser.add_argument("--artifact")
    parser.add_argument("--notes")
    parser.add_argument("--out", type=Path, help="Write Markdown to this file")
    args = parser.parse_args()

    if args.aggregate:
        metrics = normalize_aggregate(load_json(args.aggregate))
    else:
        metrics = aggregate_from_results(load_jsonl(args.results))

    markdown = render_markdown(
        run_name=args.run_name,
        model=args.model,
        mode=args.mode,
        metrics=metrics,
        artifact=args.artifact,
        notes=args.notes,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")
    else:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
