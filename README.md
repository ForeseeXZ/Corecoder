# CoreCoder · 多 Agent 编码助手与 SWE-bench 编排有效性研究

> 本项目基于 [CoreCoder](README_CN.md)（一个把 Claude Code 架构压缩到约 1,400 行 Python 的教学级复现）二次开发，在其单 Agent 循环之上构建 **Planner–Executor–Reviewer 多 Agent 编排**，并用一套可开关、可消融、带命名空间隔离的评测流水线，系统性地回答一个问题：
>
> **在真实代码修复任务（SWE-bench）上，多 Agent 编排到底能不能提升解题率？边界在哪里？**
>
> 结论先行（诚实版）：**在本任务集 + 本模型上，编排带来的净提升有限；换强模型有提升但同样有限（flash 52% → pro 58%，+6pp）。根因是模型对这类问题的理解力天花板，编排无法弥补。** 这一发现与前沿研究一致。下文用完整的实验方法论、数据与归因来支撑这个结论。

---

## 1. 项目定位

- **是什么**：一个基于 CoreCoder 的多 Agent 编码助手 + 一套严谨的 SWE-bench 评测与消融框架。
- **研究目标**：不是刷分，而是**研究多 Agent 编排（规划 / 执行 / 自检）在 SWE-bench 上的有效性与边界**，并对"为什么没提升"做深入归因。
- **交付物**：双 benchmark baseline、四路消融矩阵、五个工程增量（每个都有诚实结论）、以及一个完整自洽的实验证据归档。
- **叙事重点**：实验方法论的严谨性 + 归因的深度 + 对"编排有效性边界"的真实发现。

---

## 2. 技术栈与评测环境

| 维度 | 选择 |
|---|---|
| 模型 | **DeepSeek V4 flash**（执行，快而省）/ **DeepSeek V4 pro**（规划，强推理） |
| 接入 | 纯 API，OpenAI 兼容接口（`base_url` + key），无本地权重 |
| 定价（CNY / 百万 token，输入/输出） | flash `1 / 2`，pro `3 / 6` —— pro 是 flash 的 3×/6× |
| 评测集 | HumanEval（164 题）+ **SWE-bench Verified Mini**（50 题 = django 25 + sphinx 25，`MariusHobbhahn/swe-bench-verified-mini`，split=test） |
| 评测方式 | **Docker 化**：每题在预构建实例镜像里、由 **SWE-bench 官方 harness** 注入官方测试评分；本仓库脚本只产 patch，不自行判定 |
| 隔离（接法 B） | 在容器外独立 checkout 到 `base_commit`，Agent 在该副本上原地改源码，事后 `git diff` 抽取为 `model_patch` |

### 评测红线（贯穿全项目）

> **Agent / Planner / Reviewer 任何时候都只能看到 `problem_statement`（+ 可选 hints），永不接触官方 `test_patch`。**

所有自检脚本由 Agent 自己根据问题描述编写，绝不读取官方测试；分级 patch 中也绝不含任何验证产物。这条红线是整个 benchmark 可信度的基石。

---

## 3. 评测体系与 Baseline

### 双 benchmark

| Benchmark | 模型 | 结果 |
|---|---|---|
| HumanEval（164 题） | flash | **161 / 164 = 98.17%** pass@1 |
| SWE-bench Verified Mini（50 题） | flash | **26 / 50 = 52%** |
| SWE-bench Verified Mini（50 题） | pro | **29 / 50 = 58%** |

> 数据出处：HumanEval 见 `eval/runs/_aggregate.json`；flash 52% 的权威来源是 `eval/runs/_swebench_results.jsonl`（50 条逐题判定，26 resolved）+ `eval/runs/_reports/`；pro 58% 见 `eval/runs/_swebench_pro_*`。

### Baseline 失败分析（关键洞察）

对 flash baseline 的失败逐题归因（见 `eval/BASELINE_ANALYSIS.md`）发现：

- **定位能力 0 失败**：没有一题是改错文件——文件级定位 100% 正确。
- **83% 的失败是"定位正确、却自信地改错 + 零自检"**：Agent 找到了正确的位置，做出一个看似合理但实际错误的修改，然后**不做任何自我验证**就收工。

这个洞察直接决定了后续增量的方向：既然瓶颈不是"找不到"，而是"改错且不自查"，那么第一个增量自然是 **Reviewer 自检**，第二个是 **Planner 规划**（先把"要改哪些地方"想清楚）。

---

## 4. 多 Agent 编排：Planner–Executor–Reviewer

### 设计

```
        problem_statement (+hints)
                  │
                  ▼
   ┌─────────── Planner ───────────┐   强模型 pro，只读工具(read/grep/glob)
   │  产出结构化修复计划(JSON)       │   有界探索：不可改源码 → 零污染
   └───────────────┬───────────────┘
                   ▼  计划文本注入
   ┌─────────── Executor ──────────┐   快模型 flash，全工具(含 edit/write/bash)
   │  按计划实现修复，原地改源码      │   复用统一 agent loop
   └───────────────┬───────────────┘
                   ▼  产出 patch
   ┌─────────── Reviewer ──────────┐   自写验证脚本(只据问题描述) → 跑 → 判定
   │  STRONG_PASS / 需修订 → 回环    │   PASS 则定稿，否则带反馈再执行
   └───────────────┬───────────────┘
                   ▼
              model_patch → 官方 harness 评分
```

### 几个工程要点

- **角色边界用"工具列表"区分**：Planner 只拿到只读工具（`read`/`grep`/`glob`），物理上无法改源码，因此**不存在 patch 污染**，红线天然成立；Executor 拿全套工具。
- **复用统一 agent loop**：三个角色都是同一个 `Agent` 类的实例，只是 system prompt + 工具集 + 模型不同——一套循环，三种人格。
- **模型分工**：规划需要强推理 → pro；实现要快要省 → flash。两者用**独立的 LLM 计数器**，token / 成本分开核算（定价不同）。
- **全部可开关、可消融**：每个增量都是 `--flag`（+ 环境变量）开关，**默认关闭，关闭时 baseline 代码路径逐字节不变**；不同组合写入**独立命名空间**，可断点续跑、互不覆盖。

---

## 5. 五个增量及其诚实结论

> 这是本项目的核心。每个增量都**真实存在、可运行、有数据**，但我们如实报告它们对解题率的影响——**大多数净提升≈0**，价值在于工程能力的体现与对"为什么没用"的归因。

### 5.1 Reviewer 自检 —— 净提升 ≈ 0

让 Executor 产出 patch 后，再由一个 Reviewer 角色**自己编写验证脚本**（只依据问题描述）跑一遍，PASS 才定稿、否则带反馈回修。

- **结果**：子集上净提升≈0，真正修复 0 道。
- **根因（自我盲区）**：Agent 自写的验证脚本**继承了与它本身相同的理解偏差**。典型个案 `django-11790`：Agent 验证"HTML 里有某属性"判定为 `STRONG_PASS`，但真正的 bug 是 int/str **类型错误**——它验证的观测点根本没对齐真实测试。于是产生 **false STRONG_PASS**：自检不仅没拦住错误，还给了错误一个"通过"的假信号。

### 5.2 Planner 规划 —— 净提升 ≈ 0

先由强模型 pro 做有界只读探索，产出"要改哪些文件、每处怎么改"的计划，再交给 Executor。意在解决"多文件修复漏掉第二处"的问题。

- **结果**：子集上净提升≈0。
- **根因（规划层自信误判）**：规划层**看到了第二个该改的文件，却在推理中把它排除了**——不是没找到，而是"自信地判断它不用改"。这与 baseline 的"定位对但自信改错"是同一种病在更高层的复现。

### 5.3 Planner 输出 JSON 化 —— 能力成立 + 一个意外发现

把 Planner 的输出从自由文本改为**结构化 JSON**：`{files:[{path,why}], steps:[{target,change,expected}], completeness_check}`，配合**稳健解析**（剥离 ```json 围栏 / 前后散文 → `json.loads` → 校验形状）与**三级降级**（合法 JSON → 渲染成可读计划；解析失败 → 退回文本路径；空 → 退回无计划 baseline），保证**一次格式错误绝不让任务崩**。

- **目标**：不是提分（瓶颈是理解力），而是让"结构化 JSON 计划"这一能力真实、可解析、可讲清，并量化 **JSON 格式遵循率**。
- **意外发现**：在子集上，**flash 的 JSON 遵循率 9/10，反而高于 pro 的 5/10**。强模型在结构化输出的格式纪律上未必占优——这是一个值得记录的反直觉观察。

### 5.4 多层上下文压缩 —— 触发设计要匹配真实压力

实现三层分级压缩，按"上下文占用 / 上限"比例触发：① 工具输出裁剪（~0.55，近无损）→ ② 早期轮次 LLM 摘要（~0.72，保留近 N 轮原文）→ ③ 结构化归档（~0.88，四段式 brief）。带完整插桩（各层回收 token、overhead、peak/final）。

- **发现**：CoreCoder 本身已有一个 always-on 的基础裁剪，**把单轮上下文长期压在很低的位置**；因此在本任务集上，标准阈值（0.55/0.72/0.88）**根本不会触发**——便宜任务的上下文压力远没到。
- **结论**：压缩策略的**触发阈值必须匹配实际的上下文压力剖面**。本任务集的瓶颈是"题难"而非"上下文爆"，所以这套机制是为更长、更不收敛的会话（如烧到百万 token 的 sphinx 难题）准备的杠杆，而非通用提分手段。诚实口径：本项目度量的是"插桩多层压缩 vs 基础裁剪"的增量，不是"压缩 vs 零压缩"。

### 5.5 MCP 工具解耦 —— 架构价值，功能等价不掉分

把 `read` / `grep` 从 Agent 进程内解耦成一个**独立的 MCP（Model Context Protocol）server**（FastMCP，stdio）。Agent 作为 **MCP client**，启动时通过协议**动态发现**（`list_tools`）server 暴露的工具及其 JSON-Schema 并加载，运行时经协议调用——配套一个**异步/同步桥**（后台事件循环线程 + 常驻 session + 同步包装）与**完整降级**（server 起不来 / 调用失败 → 自动退回内置工具）。

- **价值**：贴合 Agent 工程"工具即 MCP 服务"的标配做法——工具与 Agent **解耦、协议标准化、可被任意 MCP client 复用**。
- **数据**：子集 10 题，server 启动 10/10，**108 次 MCP 调用全部成功，fallback = 0**，功能与内置工具等价、不掉分。
- **诚实口径**：server 与 Agent 同机同 cwd，收益是**架构解耦与可复用性，不是性能**；动态发现让 LLM 工具契约的 schema 来自 server，对照不如"仅改执行路径"干净——这点已在 metadata 记清。

---

## 6. 四路消融矩阵

在 dev 子集的**共同 9 题**上（按 `instance_id` 去重后取四臂交集，保证公平对照）：

| 编排臂 | 解题数（共同 9 题） | 相对 baseline |
|---|:---:|:---:|
| baseline（纯 Executor） | **4 / 9** | — |
| + Reviewer | 5 / 9 | +1 |
| + Planner | 5 / 9 | +1 |
| + Planner + Reviewer | 5 / 9 | +1 |

> **诚实解读**：三个编排臂都只比 baseline 多解 1 题，且**多解的是同一题**（`django-12304`）。结合 5.1 / 5.2 的逐题归因，这个 +1 是 **run-to-run variance（运行间抖动），不是某个增量带来的真实修复**。换言之，四路消融在该子集上**没有产生稳定的净提升**。
>
> 完整 50 题的权威数字仍是：**flash 26/50 = 52%**。各臂逐题判定见 `eval/runs/_subset*_results.jsonl`。

---

## 7. 核心发现（全文结论）

1. **多 Agent 编排在本任务 + 本模型上净提升有限**：Reviewer、Planner、二者叠加，在子集上均无稳定增益（§6），根因是"自我盲区"与"自信误判"（§5.1–5.2）。
2. **换强模型有提升，但同样有限**：flash 52% → pro 58%，**+6pp**。提升来自模型本身更强的理解力，而非编排。
3. **真正的天花板是模型对这类题的理解力**：baseline 已经 100% 定位正确，失败几乎全在"改对的地方做错的修改且不自查"——这是**理解 / 推理层**的问题，**编排（更多的角色、更多的轮次）无法弥补一个模型读不懂题的根本短板**。
4. **与前沿研究一致**：已有研究表明，即便对很强的模型，编排类组件带来的提升也仅约 2pp 量级。本项目在更小的开源模型上独立复现了"编排有效性边界"这一现象。

> 这正是本项目的价值所在：不是"我把分数刷高了"，而是**用严谨的消融与归因，划出了多 Agent 编排在真实代码修复任务上的有效性边界**。

---

## 8. 工程能力总结

- **可开关消融设计**：每个增量都是 `--flag` + 环境变量，**默认关、关闭时 baseline 逐字节不变**；新能力以"旁挂"方式接入，不污染对照。
- **命名空间隔离**：消融组合（`_rev` / `_plan` / `_comp` / `_mcp`）+ 模型维度（Planner 模型 `_plan_flashplanner`、Executor 模型 `_swebench_pro`）各自独立成文件，**flash 与 pro 臂互不覆盖**，可干净对比。
- **断点续跑**：逐题判定追加写入 `_results.jsonl`，重跑自动跳过已评分题（`--force` 才重做）。
- **Docker 评测链路**：从实例镜像抽取 checkout → reset 到 `base_commit` → Agent 改码 → `git diff` 抽 patch → 官方 harness 容器内评分，全链路自动化，`--cache_level env` 控制磁盘。
- **诚实的成本核算**：压缩自身的摘要 LLM 调用计入 `overhead_tokens`；Planner(pro) 与 Executor(flash) 分开计价。

---

## 9. 如何复现

### 准备

```bash
# 1) 安装依赖（建议 conda/venv）
pip install -e .
pip install mcp            # 仅 --mcp 增量需要

# 2) 配置 API（仓库根目录 .env）
#    OPENAI_API_KEY=...           # DeepSeek key
#    OPENAI_BASE_URL=...          # DeepSeek OpenAI 兼容端点
```

### HumanEval

```bash
python eval/run_humaneval.py            # flash，pass@1 ≈ 0.98
```

### SWE-bench：单题（最快验证）

```bash
python eval/run_swebench.py -i django__django-11790 --timeout 900
# 强模型执行：
CORECODER_MODEL=deepseek-v4-pro python eval/run_swebench.py -i django__django-11790 --timeout 900
```

### SWE-bench：子集消融（各增量开关）

```bash
# baseline 子集
python eval/run_swebench_batch.py --subset --agent-concurrency 2 --timeout 900
# 各增量（默认关，开了才走新路径；各自独立命名空间）
python eval/run_swebench_batch.py --subset --reviewer            # + Reviewer 自检
python eval/run_swebench_batch.py --subset --planner --planner-model deepseek-v4-pro     # + Planner(pro)
python eval/run_swebench_batch.py --subset --planner --planner-model deepseek-v4-flash   # JSON 遵循率对照
python eval/run_swebench_batch.py --subset --compress            # + 多层压缩
python eval/run_swebench_batch.py --subset --mcp                 # + MCP 工具解耦
```

### SWE-bench：全量 50 题

```bash
# flash baseline -> _swebench_*
python eval/run_swebench_batch.py --all --batch-size 10 --agent-concurrency 2 --max-workers 4 --timeout 900
# pro baseline   -> _swebench_pro_*（独立命名空间，不覆盖 flash）
CORECODER_MODEL=deepseek-v4-pro python eval/run_swebench_batch.py --all --batch-size 10 --agent-concurrency 2 --max-workers 4 --timeout 900
```

---

## 10. 仓库结构（关键部分）

```
corecoder/
  agent.py          # 统一 agent loop（Planner/Executor/Reviewer 共用）
  plan.py           # Planner：结构化 JSON 计划 + 稳健解析/降级
  review.py         # Reviewer：自检验证回环
  compress.py       # 多层上下文压缩（插桩、可开关）
  mcp_server.py     # read/grep 的 MCP server（FastMCP, stdio）
  mcp_bridge.py     # 异步/同步桥 + 动态工具发现 + 降级
  context.py        # 基础 always-on 上下文管理（baseline）
  tools/            # read/grep/glob/edit/write/bash/agent
  llm.py            # DeepSeek 接入 + token/成本核算
eval/
  run_humaneval.py          # HumanEval 评测
  run_swebench.py           # SWE-bench 单题（接法 B）
  run_swebench_batch.py     # 批量 driver（消融开关 + 命名空间 + 续跑 + 评分）
  BASELINE_ANALYSIS.md      # baseline 失败逐题归因
  REVIEWER_EXPERIMENT.md    # Reviewer 增量实验记录
  runs/
    _swebench_*             # flash 全量证据（26/50）
    _swebench_pro_*         # pro 全量证据（29/50）
    _subset*_*             # 各消融臂证据
    _reports/              # 官方 harness 报告
```

> 注：per-instance 的 `repo/`、`transcript.jsonl`、`summary/patch/prediction` 等大体量、可重生的产物已 gitignore，仓库只保留**小而自洽的 aggregate / report / results JSON**作为实验证据。

---

## 致谢

本项目的底座是 [CoreCoder](README_CN.md)（Claude Code 架构的教学级 Python 复现）。在此之上的多 Agent 编排、SWE-bench 评测框架、五个增量与全部实验 / 归因为本项目的工作。
