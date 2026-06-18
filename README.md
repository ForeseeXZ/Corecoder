# CoreCoder · 多 Agent 编码助手与 SWE-bench 消融实验框架

CoreCoder 是一个教学级 AI 编码 Agent 项目，在原始单 Agent 循环上扩展了
Planner / Executor / Reviewer 多 Agent 编排，并配套了一套可开关、可续跑、可归档的
HumanEval 与 SWE-bench Verified Mini 评测流水线。

这份 README 的目标不是做宣传页，而是让你能快速理解项目、把它放到阿里云服务器上跑起来，并知道如何组织消融实验。

---

## 1. 项目速读

这个仓库包含两层东西：

- **编码 Agent 本体**：`corecoder/` 下的最小 Claude Code 风格实现。核心是 `Agent.chat()` 循环：模型生成工具调用，工具执行后结果回填，再继续调用模型，直到模型输出最终文本。
- **评测与消融系统**：`eval/` 下的 HumanEval、SWE-bench 单题与批量 driver。SWE-bench 路线是“Agent 只产 patch，官方 harness 判定 resolved/unresolved”。

核心研究问题是：

> 在真实代码修复任务 SWE-bench 上，Planner、Reviewer、上下文压缩、MCP 工具解耦这些编排增量到底能不能稳定提升解题率？

仓库当前默认模型名是：

| 角色 | 默认模型 | 用途 |
|---|---|---|
| Executor | `mimo-v2.5` | 实际改代码，快模型 |
| Planner | `mimo-v2.5-pro` | 只读规划，强推理模型 |

历史归档结果里保留了一些 `deepseek-v4-*` 文件名，这是早期实验命名。代码现在通过 OpenAI 兼容 API 接任意模型，模型名以 `.env` / 环境变量为准。

---

## 2. 技术架构

### Agent 主循环

关键文件：

| 文件 | 作用 |
|---|---|
| `corecoder/agent.py` | 统一 Agent loop；Planner、Executor、Reviewer 都复用它 |
| `corecoder/llm.py` | OpenAI 兼容流式调用、tool call 解析、重试、token/成本统计 |
| `corecoder/config.py` | 从 `.env` / 环境变量读取模型、API key、base URL、上下文上限 |
| `corecoder/prompt.py` | 根据当前工具表生成 system prompt |
| `corecoder/tools/` | `bash` / `read_file` / `write_file` / `edit_file` / `glob` / `grep` / `agent` |
| `corecoder/context.py` | baseline always-on 上下文管理 |

默认工具集在 `corecoder/tools/__init__.py` 中注册。`bash` 工具带基础危险命令拦截和输出截断；读写类工具也会避开 `.env`、私钥、证书等敏感路径。

### 多 Agent 增量

| 增量 | 文件 | 开关 | 说明 |
|---|---|---|---|
| Planner | `corecoder/plan.py` | `--planner` / `CORECODER_PLANNER=1` | 只读工具 `read/grep/glob`，先产 JSON 修复计划，再交给 Executor |
| Reviewer | `corecoder/review.py` | `--reviewer` / `CORECODER_REVIEWER=1` | 写临时 `.cc_verify/verify.sh`，做 patched / original before-after 自检，失败则让 Executor 修订 |
| 多层压缩 | `corecoder/compress.py` | `--compress` / `CORECODER_COMPRESS=1` | 三层压缩：工具输出裁剪、LLM 摘要、结构化归档，并记录 token 回收 |
| MCP 工具 | `corecoder/mcp_server.py` + `mcp_bridge.py` | `--mcp` / `CORECODER_MCP=1` | 将 `read_file/grep` 放到独立 MCP server，Agent 动态发现工具；失败自动回退内置工具 |

这些增量默认全部关闭，baseline 路径保持干净。打开后会写入带后缀的独立结果命名空间，方便做消融对照。

---

## 3. 评测链路

### HumanEval

`eval/run_humaneval.py` 会让 Agent 在隔离工作目录里补全函数，再把生成代码放进官方测试入口执行。默认数据集是 `openai/openai_humaneval`。

### SWE-bench Verified Mini

`eval/run_swebench.py` 和 `eval/run_swebench_batch.py` 使用的是：

- 数据集：`MariusHobbhahn/swe-bench-verified-mini`
- split：`test`
- 规模：50 题，主要覆盖 `django` 与 `sphinx`

评测红线：

> Agent / Planner / Reviewer 只能看到 `problem_statement` 和可选 `hints_text`，绝不读取官方 `test_patch`。

SWE-bench 单题流程：

1. 从本地 SWE-bench instance Docker 镜像的 `/testbed` 拷出源码。
2. `git reset --hard <base_commit>` 对齐题目 base commit。
3. Agent 在容器外独立 checkout 中修改源码。
4. `git diff` 抽取 `model_patch`。
5. 官方 `swebench.harness.run_evaluation` 在新容器内注入官方测试并判定。

批量脚本会自动按 batch 拉取镜像、跑 Agent 子进程、合并 predictions、调用官方 harness，并把结果追加到 JSONL。

---

## 4. 本地开发运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev,eval]"
cp .env.example .env
```

编辑 `.env`：

```bash
OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
OPENAI_API_KEY=replace-with-your-api-key

CORECODER_MODEL=mimo-v2.5
CORECODER_PLANNER_MODEL=mimo-v2.5-pro
CORECODER_RUNS_DIR=eval/runs_mimo
```

交互式使用：

```bash
corecoder
corecoder -p "read this project and summarize the main modules"
corecoder -m mimo-v2.5-pro -p "fix the failing test"
```

常用测试：

```bash
pytest
python eval/run_humaneval.py 0 --timeout 120
```

---

## 5. 阿里云服务器准备

推荐使用 Ubuntu 22.04/24.04，磁盘尽量给大一点。SWE-bench instance 镜像较大，全量 50 题跑多臂消融时很吃 Docker 空间。

安装系统依赖：

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip docker.io tmux jq
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
docker run --rm hello-world
```

安装项目：

```bash
git clone <your-repo-url> CoreCoder
cd CoreCoder
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[eval]"
cp .env.example .env
```

网络注意：

- 脚本里默认设置了 `HF_ENDPOINT=https://hf-mirror.com`，可缓解 Hugging Face 访问问题。
- SWE-bench 镜像来自 Docker Hub，例如 `swebench/sweb.eval.x86_64.django_1776_django-11790:latest`。如果服务器拉 Docker Hub 慢，需要先配置阿里云容器镜像加速器。
- `eval/run_swebench.py` 单题脚本要求镜像已经在本机；`eval/run_swebench_batch.py` 会按 batch 自动 `docker pull`。

磁盘注意：

- 当前批量脚本中的 `clean_batch_images(...)` 调用被注释掉了，日志里的 `cleaned` 不代表真的删掉镜像。
- 如果磁盘紧，跑完一臂后手动执行：

```bash
docker image prune -af
docker system df
```

---

## 6. 推荐任务执行方法

建议在服务器上用 `tmux` 跑，先 smoke test，再跑子集消融，最后再全量。

```bash
tmux new -s corecoder-ablation
cd CoreCoder
source .venv/bin/activate
mkdir -p logs
export CORECODER_RUNS_DIR=eval/runs_aliyun
```

### 6.1 Smoke test

先确认 API、数据集、Docker、harness 都通：

```bash
python eval/run_humaneval.py 0 --timeout 120 | tee logs/humaneval_0.log

python eval/run_swebench_batch.py \
  -i django__django-12050 \
  --batch-size 1 \
  --agent-concurrency 1 \
  --max-workers 1 \
  --timeout 600 \
  2>&1 | tee logs/swebench_smoke.log
```

### 6.2 子集消融

`--subset` 会读取 `eval/dev_subset.json` 的 core 任务；`--include-optional` 会额外加入压力题。不开 `--force` 时会自动跳过已经评分的题，适合断点续跑。

Planner 默认使用 `.env` / 环境变量里的 `CORECODER_PLANNER_MODEL`；临时换模型时再加 `--planner-model <model>`。

```bash
# baseline
python eval/run_swebench_batch.py --subset \
  --batch-size 4 --agent-concurrency 2 --max-workers 2 --timeout 900 \
  2>&1 | tee logs/subset_baseline.log

# + Reviewer
python eval/run_swebench_batch.py --subset --reviewer \
  --batch-size 4 --agent-concurrency 2 --max-workers 2 --timeout 900 \
  2>&1 | tee logs/subset_reviewer.log

# + Planner
python eval/run_swebench_batch.py --subset --planner \
  --batch-size 4 --agent-concurrency 2 --max-workers 2 --timeout 900 \
  2>&1 | tee logs/subset_planner.log

# + Planner + Reviewer
python eval/run_swebench_batch.py --subset --planner --reviewer \
  --batch-size 4 --agent-concurrency 2 --max-workers 2 --timeout 900 \
  2>&1 | tee logs/subset_plan_reviewer.log

# + 多层上下文压缩
python eval/run_swebench_batch.py --subset --compress \
  --batch-size 4 --agent-concurrency 2 --max-workers 2 --timeout 900 \
  2>&1 | tee logs/subset_compress.log

# + MCP 工具解耦
python eval/run_swebench_batch.py --subset --mcp \
  --batch-size 4 --agent-concurrency 2 --max-workers 2 --timeout 900 \
  2>&1 | tee logs/subset_mcp.log
```

### 6.3 全量 50 题

子集确认后再跑全量：

```bash
# 默认 Executor 模型全量 baseline
python eval/run_swebench_batch.py --all \
  --batch-size 8 --agent-concurrency 2 --max-workers 4 --timeout 900 \
  2>&1 | tee logs/full_baseline.log

# 强模型 Executor 全量 baseline，输出进入独立 _swebench_pro_* 命名空间
CORECODER_MODEL=mimo-v2.5-pro \
python eval/run_swebench_batch.py --all \
  --batch-size 8 --agent-concurrency 2 --max-workers 4 --timeout 900 \
  2>&1 | tee logs/full_pro.log
```

如果要后台跑单条命令：

```bash
nohup bash -lc 'source .venv/bin/activate && python eval/run_swebench_batch.py --subset --planner --reviewer --batch-size 4 --agent-concurrency 2 --max-workers 2 --timeout 900' \
  > logs/subset_plan_reviewer.nohup.log 2>&1 &
```

---

## 7. 输出文件怎么看

默认输出目录由 `CORECODER_RUNS_DIR` 控制，未设置时是 `eval/runs_mimo/`。

关键产物：

| 路径 | 说明 |
|---|---|
| `<runs>/<instance_id>/repo/` | 单题临时 checkout，Agent 在这里改代码 |
| `<runs>/<instance_id>/summary.json` | 单题摘要：模型、token、cost、patch、plan/review/compress/mcp 元数据 |
| `<runs>/<instance_id>/prediction.json` | SWE-bench prediction record |
| `<runs>/_subset*_results.jsonl` | 子集逐题评分结果，可断点续跑 |
| `<runs>/_subset*_aggregate.json` | 子集聚合结果 |
| `<runs>/_swebench*_results.jsonl` | 全量逐题评分结果 |
| `<runs>/_swebench*_aggregate.json` | 全量聚合结果 |
| `<runs>/_reports/` | 官方 harness 生成的报告 JSON |

常见命名空间：

| 命令组合 | 子集结果文件 |
|---|---|
| baseline | `_subset_results.jsonl` |
| `--reviewer` | `_subset_rev_results.jsonl` |
| `--planner` | `_subset_plan_results.jsonl` |
| `--planner --reviewer` | `_subset_plan_rev_results.jsonl` |
| `--compress` | `_subset_comp_results.jsonl` |
| `--mcp` | `_subset_mcp_results.jsonl` |

全量结果类似：默认是 `_swebench_results.jsonl`；如果 Executor 模型不是默认 `mimo-v2.5`，会追加模型后缀，例如 `_swebench_pro_results.jsonl`。

---

## 8. 已归档实验结论

仓库中保留了历史实验结果与分析文档：

- `README_RESEARCH_NOTES.md`
- `eval/runs/_aggregate.json`
- `eval/runs/_swebench_results.jsonl`
- `eval/runs/_swebench_pro_results.jsonl`
- `eval/runs/_subset*_results.jsonl`
- `eval/BASELINE_ANALYSIS.md`
- `eval/REVIEWER_EXPERIMENT.md`

主要历史结果：

| Benchmark | 结果 |
|---|---:|
| HumanEval | 161 / 164 = 98.17% |
| SWE-bench Verified Mini，flash/默认快模型 | 26 / 50 = 52% |
| SWE-bench Verified Mini，pro/强模型 | 29 / 50 = 58% |

关键发现：

1. baseline 失败并不是主要输在文件定位，历史分析里文件级定位基本正确。
2. 大量失败属于“找到了正确位置，但自信地做了错误修改，而且没有可靠自检”。
3. Reviewer 自写验证脚本会继承模型自己的理解偏差，容易出现 false `STRONG_PASS`。
4. Planner 能把探索前置，但如果模型在规划层自信误判，仍会漏掉真正需要的第二处修改。
5. 在历史子集上，Planner / Reviewer / 二者叠加没有产生稳定净提升；强模型本身带来的收益更明显但仍有限。
6. 多层压缩和 MCP 的主要价值是工程能力与可观测性：压缩适合长会话压力，MCP 适合工具协议解耦；它们不是直接刷分按钮。

---

## 9. 仓库结构

```text
corecoder/
  agent.py          # 统一 agent loop
  llm.py            # OpenAI 兼容 LLM 接入、流式 tool calls、token/cost 统计
  config.py         # .env / 环境变量配置
  prompt.py         # system prompt 生成
  context.py        # baseline 上下文管理
  compress.py       # 可开关的三层上下文压缩
  plan.py           # Planner：只读探索 + JSON 修复计划
  review.py         # Reviewer：自写 verify.sh + before/after 自检
  mcp_server.py     # MCP server，暴露 read_file/grep
  mcp_bridge.py     # 同步 Agent loop 到异步 MCP client 的桥接与 fallback
  tools/            # bash/read/write/edit/glob/grep/agent 工具

eval/
  run_humaneval.py          # HumanEval 评测
  run_swebench.py           # SWE-bench 单题：产 patch，不自行评分
  run_swebench_batch.py     # SWE-bench 批量：拉镜像、跑 Agent、调用官方 harness
  dev_subset.json           # 快速消融子集与 optional stress 题
  BASELINE_ANALYSIS.md      # baseline 失败归因
  REVIEWER_EXPERIMENT.md    # Reviewer 实验记录
  runs/                     # 已归档历史结果

article/
  00-index.md
  01-architecture-overview.md
  02-agent-loop.md
  03-tool-system.md
  04-context-compression.md
  05-streaming-executor.md
  06-multi-agent.md
  07-hidden-features.md
```

---

## 10. 常用环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 空 | OpenAI 兼容 API key |
| `OPENAI_BASE_URL` | 空 | OpenAI 兼容 API base URL |
| `CORECODER_MODEL` | `mimo-v2.5` | Executor / CLI 默认模型 |
| `CORECODER_PLANNER_MODEL` | `mimo-v2.5-pro` | Planner 默认模型 |
| `CORECODER_RUNS_DIR` | `eval/runs_mimo` | 评测输出目录 |
| `CORECODER_MAX_TOKENS` | `4096` | CLI 默认单次输出上限；SWE-bench 脚本内部使用 `16384` |
| `CORECODER_MAX_CONTEXT` | `128000` | Agent 上下文上限 |
| `CORECODER_PROVIDER` | `openai` | 设为 `litellm` 可走 LiteLLM |
| `CORECODER_PRICING_CNY_PER_M` | 空 | JSON 形式覆盖模型价格，例如 `{"mimo-v2.5":[1,2]}` |
| `CORECODER_REVIEWER` | 空 | 设为 `1` 等同默认开启 `--reviewer` |
| `CORECODER_PLANNER` | 空 | 设为 `1` 等同默认开启 `--planner` |
| `CORECODER_COMPRESS` | 空 | 设为 `1` 等同默认开启 `--compress` |
| `CORECODER_MCP` | 空 | 设为 `1` 等同默认开启 `--mcp` |

---

## 11. 复现实验时的注意事项

- 保持 `--with-hints` 默认关闭，除非你明确要做 hints 条件实验。
- 不要让 Agent、Planner、Reviewer 读取官方 `test_patch`，这是评测可信度红线。
- 每次新实验建议设置新的 `CORECODER_RUNS_DIR`，避免和历史结果混在一起。
- 不加 `--force` 会断点续跑；加 `--force` 会重跑并覆盖同命名空间的判断。
- `--agent-concurrency` 控制同时跑几个 Agent 子进程，`--max-workers` 控制官方 harness 的 Docker worker 数。阿里云小机器建议先用 `1/1` 或 `2/2`。
- 如果 API 速率限制严格，降低 `--agent-concurrency`，或者把每个消融臂拆到不同时间段顺序跑。
- 如果 Docker 镜像占满磁盘，手工 `docker image prune -af`，或在 `eval/run_swebench_batch.py` 中恢复 `clean_batch_images(list(images.values()))`。

---

## 致谢

本项目基于 [CoreCoder](README_CN.md) 的教学级 Python 复现继续扩展。多 Agent 编排、SWE-bench 消融 driver、Reviewer / Planner / 压缩 / MCP 增量与实验归因，是本仓库在此基础上的主要工作。
