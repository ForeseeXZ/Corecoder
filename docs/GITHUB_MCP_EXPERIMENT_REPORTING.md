# GitHub MCP Experiment Reporting

This document describes CodePilot's GitHub MCP reporting path for experiment
tracking.  The repository contains a transport-independent report renderer and
GitHub MCP payload examples that can be published to tracking issues or PR
comments through a configured MCP client.

## Motivation

CodePilot produces many local artifacts while running SWE-bench experiments:
`_results.jsonl`, `_aggregate.json`, per-instance patches, transcripts, and
archived tarballs.  On remote machines these files are easy to lose or mix
together.  GitHub issues and PR comments are a better long-lived experiment log:
they preserve model names, modes, artifacts, reruns, and manual corrections near
the code.

## Flow

1. `eval/run_swebench_batch.py` writes local result files as it does today.
2. `eval/github_mcp_report.py` renders a Markdown report from the aggregate
   JSON or result JSONL.
3. A GitHub MCP client can publish that Markdown to a tracking issue or PR
   comment using issue/comment tools.
4. If GitHub MCP is unavailable, the Markdown report remains a local artifact
   and the experiment itself is not blocked.

## Usage

Generate a report from an aggregate file:

```bash
python eval/github_mcp_report.py \
  --aggregate eval/runs_mimo/merged_clean_manual_corrected/ablation20_random_plan_rev_merged_aggregate.json \
  --run-name ablation20_random_plan_rev \
  --model mimo-v2.5 \
  --mode planner+reviewer \
  --artifact artifacts/ablation_results_20260621-123232.tar.gz \
  --out examples/github_mcp/plan_rev_issue_comment.md
```

Generate a report directly from JSONL:

```bash
python eval/github_mcp_report.py \
  --results eval/runs_mimo/merged_clean_manual_corrected/mimo_v25_pro_full_merged_results.jsonl \
  --run-name mimo_v25_pro_full \
  --model mimo-v2.5-pro \
  --mode executor-only \
  --out examples/github_mcp/pro_full_issue_comment.md
```

## GitHub MCP Shape

The example config in `examples/github_mcp/github_mcp_config.example.json`
enables a narrow GitHub MCP surface:

- `repos`: read repository context and files.
- `issues`: create or update experiment tracking issues.
- `pull_requests`: attach summaries to experiment or implementation PRs.
- `actions`: inspect CI status when evaluation jobs are automated.

The integration should not enable every GitHub capability by default.  Toolsets
keep the model's available actions smaller and make permission review easier.

## Fallback Policy

GitHub publishing should be best-effort:

- If the MCP server cannot start, keep the Markdown report locally.
- If posting times out, retry once and write the intended payload to disk.
- If the target issue is missing, do not create surprise public state unless the
  workflow was explicitly configured to do so.
- Do not put API keys, `.env` contents, or raw transcripts into GitHub comments.

## Interview Framing

The concise framing is:

> I added a GitHub MCP reporting boundary for experiment management.  The local
> runner produces aggregate metrics, the report renderer converts them into a
> stable issue/comment payload, and GitHub MCP is responsible for the external
> publishing step.  This keeps experiment summarization separate from GitHub
> credentials and write permissions.
