# CoreCoder 可靠性改造下一步工作计划 V2

> 日期：2026-08-30  
> 当前自研分支：`my-baseline`  
> 当前提交：`68143a37136baef9b45b6750e2778e75e55687e1`  
> 官方上游：`he-yufeng/CoreCoder` `v0.4.1` / `a03ef36412e432fc49d972d4007b36ce44ec5d9a`  
> 前序分析：`docs/handoff/2026-08-30-corecoder-agent-reliability-analysis.md`  
> 状态：计划与测试设计，尚未实施产品改造

## 0. 决策摘要

下一步不按旧版 T00→T12 原样展开，也不直接合并官方 `v0.4.1`。采用以下策略：

1. 先恢复可验证的 Git 与 Python 基线；当前 index 损坏且工作树中的 `README.md` 已出现 NUL 数据。
2. 从官方 `v0.4.1` 按行为选择性移植可靠性修复和测试，不覆盖本地 Planner、Reviewer、MCP、Compression 与 SWE-bench 实验路径。
3. 将旧 T02/T03/T05/T12 的相关部分合并为一个 `ToolRuntime + RunLedger` 纵向闭环，先建立结构化结果语义，再记录事件。
4. 将 Workspace 提前；当前文档推荐的相对 `CORECODER_RUNS_DIR` 与 `os.chdir()` 组合可导致 repo 路径二次解析。
5. 先做最小 `RepairRunController`，只持有 Workspace、验证、环境健康与 Finish Reason；无进展策略作为其内部 Implementation。
6. 将 Verification Contract 与 Verification Harness 合成同一纵向闭环，复用现有 Reviewer 的 before/after Implementation。
7. 暂缓完整 Working State；只有 Run Ledger 和压缩实验证明结构化状态确有收益后再引入，避免双重事实源。
8. 最后执行 Planner/Reviewer/Controller 消融；无稳定收益的 Adapter 应删除，而不是继续叠加角色。

## 1. 约束与已确认事实

### 1.1 仓库状态

- `origin/my-baseline` 与本地 HEAD 相同，没有未拉取的自研提交。
- 已配置的 `public-origin` 是 `tomsshagen/CoreCoder`，不是官方上游。
- 当前自研历史从 `public-origin/my-baseline` 分叉，与官方 `main` 没有 merge base；禁止普通 merge/pull。
- 官方源码已只读隔离到 `.tmp/upstream-corecoder-20260830`，用于逐文件比较。
- 当前 Git index 报 `bad signature 0x00000000`；任何重建、删除或覆盖 index 的动作均需先保护现场。
- 当前系统 Python 缺少 `openai`，仓库 `venv` 缺少 `pytest`；全仓 pytest 还会误收集 `agent_compare_mimo` 下的实验 fixture。

### 1.2 架构约束

- 通用 `Agent.chat()` 同时服务 CLI、HumanEval、SWE-bench、Planner、Reviewer 和子 Agent；Repair Run 状态机不得侵入所有调用者。
- Tool Interface 当前只返回 `str`，无法可靠区分成功、非零退出、超时、阻止、异常与截断。
- 同一 Model Turn 的多个工具调用无 effect 区分地并行执行。
- Runner transcript 记录 token、工具开始和部分 log，但不记录 Tool Observation。
- Reviewer 已实现 patched/original before-after 执行；问题是 Verification Contract 在看到 patch 后生成，且错误语义仍是文本拼接。
- Planner/Reviewer/Executor 的 token 使用存在共享计数，当前不能可靠做角色归因。

### 1.3 Deep Module 选择

本计划只优先深化以下 Module：

| Module | Depth 与 deletion test | 计划定位 |
|---|---|---|
| WorkspaceExecution | 删除后 root、cwd、Git、snapshot、cleanup 知识会散回 runner、Reviewer、Bash | P1 |
| ToolRuntime | 删除后调度、effect、错误分类、Tool Observation 会散回 Agent 和各工具 | P2 |
| RunLedger | 删除后 ID、顺序、artifact、归约会散回 Agent、runner、LLM | P2 |
| RepairRunController | 删除后门禁、环境健康、停止和 Finish Reason 会散回 runner、Reviewer | P3 |
| VerificationHarness | 删除后 before/after、环境分类、清理与结果归约会散回 Reviewer/runner | P4 |

下列内容不独立建 Module：

- 旧 T03：并入 RunLedger 的 Implementation。
- 旧 T08：并入 RepairRunController 的内部策略。
- Evidence Ledger：不创建；Run Ledger 是唯一事实源。
- Working State：如需要，只做 Run Ledger 的可重建 projection。
- Planner/Reviewer 消融：属于实验，不创建新的生产 Module。

## 2. 官方 v0.4.1 纳入策略

### 2.1 必须选择性移植

| 上游改动 | 本地落点 | 纳入阶段 | 原因与注意事项 |
|---|---|---|---|
| 移除 `_exec_tool()` 对全局 `get_tool()` 的 fallback | `corecoder/agent.py` | P1 | 当前受限 Agent 可通过未暴露的工具名称取得全局 Tool，形成 Planner/Reviewer/子 Agent 能力泄漏；工具只能从实例能力集解析 |
| 中断后补齐 pending tool replies | `corecoder/agent.py` | P1 | 防止 Ctrl+C 后产生非法消息历史；保留本地 MCP/Compression 初始化 |
| 用参数绑定区分 bad arguments 与工具内部 TypeError | `corecoder/agent.py` | P1 | 为后续 ToolResult 错误分类建立准确基线 |
| Context safe split，避免 orphan tool reply | `corecoder/context.py`、`corecoder/compress.py` | P1 | 上游只改 ContextManager；本地两条压缩路径都必须覆盖 |
| `stream_options` 只在 BadRequest 时降级 | `corecoder/llm.py` | P1 | 避免瞬态错误经历两轮完整重试 |
| usage 空字段归零、APIError 状态安全读取 | `corecoder/llm.py` | P1 | 避免计数与错误处理崩溃 |
| ScriptedLLM 思路及完整 Agent loop fixture | 首选 `tests/fakes/`；是否进入产品代码由 P1 决定 | P1 | T01 的离线确定性测试基础；不要求引入上游 demo |
| session ID/损坏文件修复 | `corecoder/session.py` | P1 | 低耦合可靠性修复，可独立回滚 |
| grep 扫描截断显式报告 | `corecoder/tools/grep.py` | P1 | 防止“未找到”与“未扫描完”混淆 |
| Ruff 与 Windows CI 约束 | `pyproject.toml`、CI | P0/P1 | 提供跨平台快速反馈；不复制上游发布流水线 |

### 2.2 与后续 Module 一起移植

| 上游改动 | 阶段 | 原因 |
|---|---|---|
| Bash thread-local cwd | P2 | 本地 runner 仍直接写 `bash_tool._cwd`，必须与 WorkspaceExecution 一起迁移 |
| 连续 `cd a && cd b` 顺序解析 | P2 | 应纳入 Workspace/Bash 回归测试，而不是孤立改动 |
| 更完整危险命令模式 | P2 | 与 ToolRuntime blocked 结果语义一起验收 |
| tool/文件 UTF-8 显式处理 | P1/P2 | 可低风险移植，但需要 Windows/中文路径测试 |

### 2.3 暂不纳入

| 上游内容 | 决策 | 原因 |
|---|---|---|
| TodoWriteTool | 暂缓 | 与未来 Working State/无进展提示可能重叠，尚无 SWE-bench 收益证据 |
| `demo.py` 与演示图片 | 不纳入生产路径 | 可借鉴测试思想，但不影响 Repair Run 可靠性 |
| 上游 README/文章整体替换 | 不纳入 | 本地 README 描述 CodePilot 实验架构；只能修复损坏后再人工融合 |
| 删除本地 safety.py | 禁止 | 本地工具安全检查属于自研增量，上游缺失不代表应删除 |
| 删除 Planner/Reviewer/MCP/Compression | 等 P6 消融 | 先建立可靠实验事实，再应用 deletion test |
| 整包 `agent.py`/`context.py` 覆盖 | 禁止 | 会丢失本地实验开关、统计和 Adapter |
| 上游损坏 tool JSON 静默转 `{}` | 禁止作为最终语义 | P2 必须保留原始片段并产生显式 parse error，不能伪装成空参数 |
| 删除 `Agent.close()` | 禁止 | 当前 MCP Adapter 需要显式释放外部进程 |

## 3. 目标流程与依赖

```text
P0 现场恢复与绿基线
 └─ P1 上游可靠性补丁 + Agent 合约测试
     └─ P2 WorkspaceExecution + ToolRuntime + RunLedger
         └─ P3 最小 RepairRunController + FinishReason
             └─ P4 VerificationContract + VerificationHarness
                 └─ P5 No-progress policy
                     └─ P6 固定任务集消融与发布判断
                         └─ P7 可选 WorkingState projection
```

每个阶段都必须满足：

- 一个可观察的端到端行为，而不是只新增类型或抽象类；
- 先有失败 fixture，再改 Implementation；
- 保留旧 baseline Experiment Arm 或明确迁移策略；
- 失败类别、测试输出和回滚点可追踪；
- 当前阶段退出门槛未满足时，不进入下一阶段。

## 4. 分阶段工作计划

### P0：保护现场并建立绿基线

**目标**：获得不丢用户数据、可重复测试、可比较上游的工作位置。

**工作内容**：

1. 记录 branch、HEAD、remotes、损坏 `.git/index` 哈希和工作树文件清单。
2. 备份损坏 index，不直接删除或覆盖。
3. 对 tracked 文件执行 HEAD blob 与工作树哈希对照，单独保存 modified/missing/NUL/untracked 清单。
4. 重点保护新文档、实验 aggregate、可能的 staged 内容和损坏的 `README.md`。
5. 在新的干净 clone 或明确的恢复工作树中继续实施；原目录保留为只读证据源。
6. 创建可重复 Python 环境并安装 `.[dev]`；不要依赖当前系统 Python 或残缺 `venv`。
7. 将 pytest 收集范围固定为 `tests/`，排除 `agent_compare_mimo`、`eval/runs*`、`.tmp`。
8. 增加最小 CI：Windows + Ubuntu，Python 3.10 与 3.13，运行 pytest、ruff、compileall。

**P0 测试与验收**：

- `git status --short`、`git diff`、`git diff --cached` 均成功。
- 恢复工作树的 HEAD、branch 与期望一致。
- 工作树差异清单可逐项映射到备份文件或明确用户改动。
- `README.md` 不含 NUL；恢复前后版本有独立备份。
- `python -m pytest tests -q` 能完成收集，不访问网络。
- `python -m compileall -q corecoder tests` 通过。
- `python -m ruff check corecoder tests` 通过或形成已批准的基线例外清单。
- CI 在 Windows/Ubuntu 两个平台至少各成功一次。

**Artifacts**：`recovery-manifest.json`、index 备份位置、环境版本清单、pytest/ruff/compileall 报告。

**回滚**：删除新的恢复工作位置即可；不得修改原损坏目录中的 index。

### P1：上游可靠性补丁与 Agent 合约测试

**目标**：先把当前 Agent loop 和上游已修复的边缘行为冻结成 L0 合约。

**工作内容**：

1. 建立 Scripted LLM、Fake Tool、Fake Clock 与无网络 fixture。
2. 冻结当前 Agent 消息顺序、普通结束、max rounds、单/多工具结果顺序。
3. 先复现受限 Agent 回退全局 Tool、pending tool replies、中断后 orphan message、内部 TypeError 误分类、压缩 orphan reply、null usage 等问题。
4. 按 2.1 表逐项手工移植上游补丁；每个行为一个小提交或可独立回滚 patch。
5. 将上游测试意图适配到本地扩展路径；不得直接覆盖本地测试文件。
6. 增加 session、grep 截断、UTF-8/中文路径和损坏数据测试。

**P1 关键测试**：

| ID | Fixture | 断言 |
|---|---|---|
| A01 | Scripted LLM：tool call → text | 消息角色与 tool_call_id 顺序稳定，最终文本一致 |
| A02 | Scripted LLM：无限相同 tool call | 恰好达到 max rounds，返回旧 baseline 终止文本 |
| A03 | 工具执行中 KeyboardInterrupt | 每个 pending call 恰有一条 `[interrupted]` tool reply |
| A04 | 受限 Agent 只提供 read/grep，模型调用 bash/edit/未知名称 | 均返回 unknown/denied；不得从全局 registry 解析或执行 |
| A05 | execute 参数缺失/多余 | 返回 bad arguments，不调用工具 Implementation |
| A06 | 工具内部抛 TypeError | 归类为 executing error，不误报 bad arguments |
| A07 | 两个并行 Fake Tool | 结果按原 tool call ID 顺序写入，而非完成顺序 |
| A08 | 压缩切分点落在一个 assistant 的多个 tool replies 中 | 整组 reply 按 call ID 保留，不存在 orphan 或缺失 |
| A09 | baseline 与 CompressionManager 两条路径 | 相同 orphan 不变量均成立 |
| L01 | provider 拒绝 stream_options | 仅 BadRequest 触发无该参数的第二次请求 |
| L02 | rate limit/timeout/connection error | 遵循一次重试序列，不因 fallback 加倍请求 |
| L03 | usage.prompt/completion 为 null | 总计数保持整数且不崩溃 |
| S01 | 两次同秒默认 session | ID 不冲突，内容各自可回读 |
| S02 | `../`、绝对路径、Windows 反斜杠、超长 ID | 结果始终位于 session 目录内 |
| S03 | 损坏 JSON session | load 返回明确缺失结果，不抛未处理异常 |
| G01 | grep 超过文件扫描上限 | 结果显式包含 incomplete/truncated 信号 |
| G02 | 搜索根路径祖先名为 build | 根内真实文件仍被扫描 |

**退出门槛**：

- 所有 P1 测试在 Windows/Ubuntu 重复运行 10 次无抖动。
- 不新增网络依赖。
- CLI、HumanEval、SWE-bench 默认关闭增量时的 Interface 保持兼容。
- 上游补丁清单中的每一项都有来源、适配说明和测试 ID。
- Planner、Reviewer 和子 Agent 的实际可执行能力与各自实例 Tool 列表完全一致。

### P2：WorkspaceExecution、ToolRuntime 与 RunLedger 最小闭环

**目标**：一次工具调用从确定 Workspace 到结构化 Tool Observation，再到持久化 Run Ledger 全链路可解释。

**工作内容**：

1. WorkspaceExecution 集中持有绝对 root、基线 commit、Git 命令、Workspace Snapshot、scratch 和 cleanup。
2. 启动时立即 `resolve()`；后续任何 cwd 改变不得重新解释 root。
3. 删除 runner 对进程全局 `os.chdir()` 和 `bash_tool._cwd` 的隐式依赖，或把它们收敛为 WorkspaceExecution 内部 Implementation。
4. ToolRuntime 将工具执行结果区分为 success、bad arguments、non-zero、timeout、blocked、exception、cancelled、truncated。
5. 每个 Tool 声明 read、write 或 process effect；纯 read 可并行，write/process 默认串行。
6. 子 Agent 获得显式能力集；effect 不是权限替代品。
7. Run Ledger 记录 run/model turn/tool started/tool finished/snapshot/compression/abort 事件。
8. 大输出写独立 artifact；Ledger 保存摘要、哈希、长度、截断原因和引用。
9. 旧 transcript 保留读取兼容，但新 summary 必须由 Run Ledger 归约生成。
10. Model Adapter 的损坏/分片 tool JSON 归一化为显式 parse error，保留脱敏后的原始片段；重复或缺失 call ID 有固定行为。

**P2 关键测试**：

| ID | Fixture | 断言 |
|---|---|---|
| W01 | 从 repo 外、repo 内、不同 cwd 启动 | Workspace root 与 patch hash 完全一致 |
| W02 | 相对 `CORECODER_RUNS_DIR` + runner chdir 场景 | Reviewer/Git/scratch 不产生二次拼接路径 |
| W03 | tracked 修改 + Executor untracked 文件 | Snapshot 同时捕获二者，cleanup 不删除 Executor 文件 |
| W04 | Reviewer scratch 与临时 helper | 最终 patch 不包含它们 |
| W05 | 损坏/非 Git/权限不足 Workspace | 分类为 workspace error，不进入代码验证 |
| T01 | Fake Tool 返回各类结构化结果 | Tool Observation 分类、模型可见文本和 artifact 一致 |
| T02 | 两个 read Adapter 使用 Barrier | 两者确实同时进入 Implementation |
| T03 | read+write、write+write、bash+edit | write/process 不并行，执行顺序可预测 |
| T04 | 结果完成顺序与调用顺序相反 | Tool Observation 仍按 call ID 对齐 |
| T05 | 子 Agent 缺 write/bash 能力 | 无法通过名称、全局 registry 或 agent 递归绕过 |
| T06 | Bash blocked/non-zero/timeout/UTF-8 | 状态不依赖字符串解析，模型仍收到可读内容 |
| T07 | 分片/损坏 JSON、重复/缺失 call ID | 可重建成功调用；失败保留 raw fragment/hash，不静默转空参数 |
| R01 | 正常单工具 Run | started/finished 一一配对，ID 与 round 完整 |
| R02 | 多工具并行 Run | 并行组、调度顺序和完成顺序均可重建 |
| R03 | 工具中断或 Run 取消 | 允许未完成 call，但必须有 run_aborted/cancelled 解释 |
| R04 | 超长 stdout/stderr | artifact 哈希可验证，Ledger 不嵌入全部大文本 |
| R05 | 同一 fixture 两次归约 | summary 字段和计数完全一致 |
| R06 | 旧 transcript fixture | 兼容读取并明确标记 observation_unknown |

**并发测试规则**：

- 不用短 `sleep` 推断并发；使用 `Barrier`、`Event` 和受控 executor。
- 每个测试设硬超时，失败时输出线程栈和当前事件。
- 并发组内允许完成顺序不同，但 Model Turn 的 tool reply 顺序必须符合 provider 消息约束。

**退出门槛**：

- 100% 正常完成的 tool_started 有 tool_finished。
- 100% 受控取消的未闭合调用由 run_aborted/cancelled 解释。
- summary 与 Run Ledger 归约一致率 100%。
- 不再从 Bash 文本猜测 exit code 或 timeout。
- 不同 cwd 的 Workspace Snapshot 一致。

### P3：最小 RepairRunController 与完成门禁

**目标**：把“Model Turn 结束”和“Repair Run 已验证”分离。

**工作内容**：

1. RepairRunController 只管理 diagnose/patch/verify/finish 四个必要阶段。
2. 通用 `Agent.chat()` 保持文本式 Interface；Controller 位于 SWE-bench/代码修复 Seam。
3. Controller 只依据 Workspace Snapshot、Verification Result、环境健康和预算生成 Finish Reason。
4. 初始 Finish Reason 集合：verified、unresolved、no_progress、budget_exhausted、environment_error、model_error、cancelled。
5. 默认保留旧 baseline Experiment Arm，用 feature flag 做 paired 对照。
6. 每次状态变化写 Run Ledger，状态可由事件重建。

**P3 关键测试**：

| ID | 输入 | 期望 Finish Reason |
|---|---|---|
| C01 | 模型声称完成、无 patch | unresolved |
| C02 | 有 patch、未运行验证 | unresolved |
| C03 | 有 patch、验证因依赖缺失无法启动 | environment_error |
| C04 | provider 429 重试耗尽 | model_error |
| C05 | 工具 timeout 但后续恢复并验证通过 | verified，Ledger 保留 timeout 事实 |
| C06 | before fail + after pass + 回归通过 | verified |
| C07 | 达到全局预算、存在未验证 patch | budget_exhausted |
| C08 | 用户/runner 取消 | cancelled |
| C09 | 相同 Run Ledger 重放 | 得到相同阶段与 Finish Reason |

**退出门槛**：

- 无 patch、无验证、环境损坏均不可能产生 verified。
- 任一 Finish Reason 都能定位到 Run Ledger 中的决定性事件。
- CLI/HumanEval 不启用 Controller 时行为不变。

### P4：Verification Contract 与 Verification Harness

**目标**：在看到最终 patch 前冻结验证目标，并用确定性 Implementation 区分基线与修改后行为。

**工作内容**：

1. Diagnose 阶段生成最小 reproducer/contract，并在基线 Workspace Snapshot 上执行。
2. contract 冻结后才允许进入 Patch；修订必须产生新版本和原因事件。
3. Harness 在干净基线和固定 patched snapshot 上执行同一 contract。
4. 区分 assertion failure、timeout、dependency failure、runner failure、permission、non-discriminating check。
5. Reviewer 只寻找反例、漏改范围和 contract 缺陷；不直接产生最终 Finish Reason。
6. Reviewer scratch 与测试 helper 始终位于隔离目录，最终 patch 与验证前 snapshot 一致。
7. 无法本地复现时标记 oracle_strength=weak；默认不能产生强 verified。

**P4 最小验证 fixture**：

| Fixture | Before | After | 环境 | 期望 |
|---|---:|---:|---|---|
| V01 区分性行为 | fail | pass | healthy | strong evidence |
| V02 非区分性检查 | pass | pass | healthy | non_discriminating |
| V03 未修复 | fail | fail | healthy | behavior_failed |
| V04 回归 | fail | pass | healthy，但 regression fail | not verified |
| V05 依赖缺失 | none | none | dependency error | environment_error |
| V06 runner 崩溃 | none | none | harness error | harness_error，不默认 PASS |
| V07 超时 | timeout | timeout/pass | healthy | timeout 分类，不当 assertion failure |
| V08 新文件 patch | fail | pass | healthy | snapshot/cleanup 保留新源文件 |
| V09 reviewer 生成额外文件 | fail | pass | healthy | extra 文件不进入最终 patch |
| V10 contract 被修改 | fail | pass | healthy | 新版本事件与原因必需 |
| V11 false-pass 11790 fixture | pass 或 surface-only | pass | healthy | 不得 strong evidence |
| V12 Enum 12304 fixture | fail | 表面 pass/类型错 | healthy | 精确类型/成员关系断言失败 |

**P4 平台要求**：

- L0：临时小型 Git repo，Windows/Ubuntu 均运行，不需要 Docker。
- L1：使用脱敏的完整 Run Ledger fixture，不调用远程模型。
- L2：Linux/WSL + Docker，运行 1 个简单成功、1 个 false-pass、1 个不收敛历史任务。
- Windows 不直接承诺现有 `SIGALRM`/bash 路径；如需原生支持，另建跨平台 Adapter 并单独验收。

**退出门槛**：

- before fail + after pass 是唯一 strong behavior evidence。
- 环境和 Harness 错误均不会解释为代码成功/失败。
- 验证后 patch hash 与验证前固定 snapshot 一致。
- `django__django-11790` 类 surface proxy 被稳定拦截。

### P5：无进展策略

**目标**：在不依赖自由文本“新假设”的情况下，控制重复搜索和累计 token。

**第一版只使用可观察信号**：

- 重复 tool name + 规范化参数；
- 新路径/符号数量；
- Workspace Snapshot hash 变化；
- Verification Result 改善；
- environment error 是否被解决；
- Model Turn 与 token 阶段预算。

**策略**：

1. 连续 N 个 Model Turn 无新证据时请求一次收敛总结并切换策略。
2. 再次达到阈值则 no_progress；context compression 不重置计数。
3. environment_error 不计为代码假设失败。
4. 每个 stop decision 写入所使用的指标和阈值。

**P5 测试**：

| ID | 序列 | 断言 |
|---|---|---|
| N01 | 无限相同 read | 远低于 50 rounds 停止为 no_progress |
| N02 | 相同文件不同新行区间 | 规范化后按配置判断是否新证据 |
| N03 | 持续发现新符号、无 patch | 不过早停止，但受 diagnose 预算限制 |
| N04 | patch hash 连续变化且验证改善 | 不误杀 |
| N05 | 依赖错误后成功修复环境 | progress 恢复，不计代码失败 |
| N06 | compression 多次触发 | 无进展计数不重置 |
| N07 | 子 Agent 重复父 Agent 搜索 | 全 Run 维度能识别重复 signature |
| N08 | 临界阈值前后 | 无 off-by-one，Ledger 给出精确依据 |

**退出门槛**：

- 重复 read fixture 的 tool calls 和 token 至少下降 50%。
- 正常长任务 fixture 零误杀。
- `sphinx-doc__sphinx-8548`/`9461` 录制 fixture 能解释停止原因。

### P6：角色消融与发布判断

**目标**：判断哪些 Adapter 提供稳定 Leverage，哪些应删除。

**Experiment Arms**：

1. 旧 baseline；
2. 上游可靠性补丁 + Workspace/ToolRuntime/RunLedger；
3. + RepairRunController/VerificationHarness，无 Planner/Reviewer；
4. + Reviewer 反例搜索；
5. + 动态 Planner；
6. 必要时 + No-progress policy 单独消融。

**任务集**：

- 成功保护：现有 resolved 任务 5 个；
- 自信错误：`django__django-11790`、`django__django-12304`；
- 多文件 partial：至少 3 个；
- 不收敛：`sphinx-doc__sphinx-8548`、`sphinx-doc__sphinx-9461`；
- 固定不可变随机 20 题 manifest；
- 完整 50 题仅在 20 题门槛通过后运行。

**控制变量**：

- 相同 base commit、Docker image、问题文本、模型、温度、max tokens、超时和题目顺序；
- 每个 Experiment Arm 独立输出目录，禁止覆盖；
- provider/rate-limit/environment failure 单独统计并可剔除重跑；
- 20 题至少 3 次独立重复；不能重复时明确标为弱证据；
- 不读取 gold patch 或隐藏 test_patch 生成 production Verification Contract。

**指标**：

- correctness：resolved rate、false verified、partial fix、strong-contract coverage；
- convergence：tool calls、重复 signature、no progress/max round、首次 patch/验证轮次；
- cost：按角色 prompt/completion token、模型成本、Harness 执行时间；
- observability：event 配对、summary 一致、Finish Reason 可解释率；
- regression：成功保护任务通过率和额外开销。

**统计与发布门槛**：

- 报告逐题 paired 结果、绝对差、相对差与 bootstrap 95% CI；小样本不只报告平均值。
- false verified fixture 必须为 0。
- Run Ledger/summary 一致率和可解释 Finish Reason 均为 100%。
- 成功保护任务不得稳定回退；单次波动需重复确认。
- 若 resolved rate 无提升，只有在 false verified、成本或可归因性显著改善时才发布为可靠性版本。
- Planner/Reviewer 若无稳定收益，应删除对应 Adapter 或默认关闭。
- L3 运行前预登记容忍度；默认建议为 20 题 × 3 次后 paired resolved delta 的 bootstrap 95% CI 下界不低于 `-5pp`。
- 指定 no-progress stress 任务的 tool calls 或累计 token 中位数至少下降 30%；否则不得发布“收敛改善”结论。
- environment error 超过预登记上限的实验批次无资格用于 resolved 对比，必须修复环境后重跑。

### P7：可选 Working State projection

仅在以下全部成立时启动：

- P2–P6 已完成；
- compression 后确实存在关键证据丢失；
- Run Ledger 无法通过简单 projection 满足 Prompt；
- 至少两个调用者需要相同结构化状态。

Working State 只能从 Run Ledger 重建，不得成为第二事实源。第一版不得自动把自由文本中的“假设/置信度”伪装成确定性事实。

## 5. 完整测试体系

### 5.1 测试层级

| 层级 | 目的 | 网络/模型 | 平台 | 每次 PR |
|---|---|---|---|---|
| L0 | Module Interface、错误语义、并发、状态归约 | 禁止网络；Scripted/Fake Adapter | Windows + Ubuntu | 必须 |
| L1 | 完整事件回放、历史回归、schema 兼容 | 禁止远程模型 | Windows + Ubuntu | 关键路径必须 |
| L2 | 单题真实 checkout/Docker smoke | 允许远程模型 | Linux/WSL | 阶段里程碑 |
| L3 | 固定 manifest paired ablation | 允许远程模型 | 固定 Linux 环境 | 发布前 |

### 5.2 标准 fixture 库

- `ScriptedModel`: 确定性 Model Turn 序列、分片 tool JSON、usage 和 provider error。
- `FakeTool`: 可配置 effect、Barrier、结果、异常、耗时和输出规模。
- `TempGitWorkspace`: 固定 base、tracked/untracked、scratch、权限与损坏状态。
- `LedgerFixture`: 完整、取消、旧 transcript、schema 错误和大 artifact 引用。
- `VerificationRepo`: before/after、回归、依赖缺失、runner 崩溃与 timeout 小仓库。
- `ProgressSequence`: 重复 read、新证据、patch 改善、environment recovery 序列。
- `HistoricalCase`: 11790、12304、8548、9461 的脱敏 issue/patch/trace 摘要，不包含 gold oracle。

fixture 必须：

- 不依赖真实时间；使用 Fake Clock 或单调时钟注入点。
- 不依赖固定绝对路径；使用临时目录并显式测试中文/空格路径。
- 不包含 API key、用户目录或远端响应中的敏感内容。
- 带 schema/version 和来源说明。

### 5.3 错误分类覆盖

| 类别 | 必测值 |
|---|---|
| Model | bad tool JSON、429、timeout、connection、5xx、4xx、空 usage、流中断 |
| Tool | bad arguments、blocked、non-zero、timeout、exception、cancelled、truncated |
| Workspace | not repo、index corrupt、permission、path escape、cleanup failure、snapshot mismatch |
| Verification | assertion fail、non-discriminating、dependency、timeout、runner、permission、regression |
| Controller | verified、unresolved、no_progress、budget_exhausted、environment_error、model_error、cancelled |

每个类别至少有一个 L0 fixture，并断言：结构化状态、模型可见文本、Ledger 事件、summary 和 Finish Reason 的对应关系。

### 5.4 不变量与性质测试

1. 每个 assistant tool call 在下一次模型请求前恰有一个对应 tool reply。
2. Tool Observation 的 call ID、name 和参数哈希不可串位。
3. write/process Adapter 不在同一 Workspace 并行执行。
4. Workspace root 在 Repair Run 内不可变化。
5. Workspace Snapshot 绑定固定 base commit，验证前后可复算。
6. Run Ledger 只追加，归约具有确定性和幂等性。
7. 正常完成的 started/finished 配对完整；取消/崩溃有明确 abort 事实。
8. 只有区分性 Verification Result 能产生 strong evidence。
9. verified 必须同时具有 patch、健康环境、区分性行为验证和通过的回归检查。
10. 验证不能改变最终 patch。

### 5.5 CI 与命令建议

快速门禁：

```powershell
python -m pytest tests -q -m "not integration and not experiment"
python -m ruff check corecoder tests
python -m compileall -q corecoder tests
```

L1：

```powershell
python -m pytest tests/replay tests/contracts -q
```

Linux/WSL L2：

```bash
python -m pytest tests/integration -q -m integration
python eval/run_swebench.py -i django__django-11790 --timeout 900
```

要求：

- pytest marker 必须注册，未知 marker 视为错误。
- L0/L1 设全局测试超时；超时报告保留线程/子进程信息。
- flaky 重跑不能把第一次失败隐藏；报告首次失败和重跑结果。
- 覆盖率只作缺口提示，不作为 Depth 指标；核心 Interface 分支必须有显式行为测试。

### 5.6 Artifact 规范

每个测试/实验 Run 至少保存：

- `run-ledger.jsonl`；
- `summary.json`；
- `workspace-snapshots.json`；
- `verification-contract.json`；
- `verification-result.json`；
- 大 stdout/stderr artifact 及 SHA-256；
- `environment.json`：OS、Python、commit、image、模型和参数；
- `pytest.xml`/实验 manifest/失败分类报告。

Artifact 不得包含 API key；路径需要去用户目录前缀或使用 Run 相对路径。

### 5.7 故障注入与易遗漏回归

以下场景容易绕过普通 happy-path 测试，必须在对应阶段加入：

**事件与存储**：

- callback 自身抛异常、Ledger 文件不可写、磁盘空间不足和 artifact 只写入一半；
- 进程硬杀导致尾部事件缺失；读取时必须归类为 incomplete/aborted，不能伪造 completed；
- 重复事件、重复 summary 归约、同一 run ID 重放与恢复，验证幂等策略；
- trace schema 升级必须保留旧格式只读兼容，或先提供可验证的转换工具。

**进程与并发**：

- timeout/cancel 后检查子进程和孙进程是否残留；
- 两个 write 修改同一文件、read 与 write 同批、并行组中一个调用失败；
- ToolRuntime/Ledger callback 失败时，其余调用必须得到 finished/cancelled 或 run_aborted 解释；
- provider 返回首个 stream chunk 后断线，部分文本/tool JSON 不能当作正常 Model Turn 完成。

**数据与安全**：

- 工具输出包含 API key 样式文本、私钥头、二进制、无效 UTF-8 和超长单行；Ledger 必须摘要/脱敏，artifact 访问策略需验证；
- 路径包含空格、中文、CRLF、大小写差异、symlink 和 Windows 反斜杠；
- 受限 Agent 通过全局 registry、别名、大小写、AgentTool 或 Bash 间接调用未授予能力；
- Reviewer 与 Executor 创建同一路径时，cleanup 不得仅凭 untracked delta 删除 Executor 产物。

**Git 与 Workspace**：

- staged/unstaged/untracked、删除、重命名、filemode、submodule 和 symlink patch；
- index 损坏、stash conflict、cleanup 失败、snapshot hash 不匹配；
- 活跃 Repair Run 的 before snapshot 不得依赖修改当前工作树的 `git stash`；如暂时保留旧 Implementation，必须覆盖 stash/pop 失败和未跟踪文件恢复。

**预算与归因**：

- phase budget 在 N-1/N/N+1 的精确边界；compression 前后计数不重置；
- environment error 修复后预算如何继续必须固定并测试；
- Reviewer 请求 Executor revise 的 token 归属必须预先规定，所有角色 usage 之和应等于 Run total；
- Planner/Reviewer Adapter 删除后，Controller 与 Harness 的行为测试应保持不变。

### 5.8 阶段回滚条件

- **P1**：受限 Agent 仍可执行未授予 Tool、现有 Planner/Reviewer/MCP/CLI smoke 回退、MiMo 配置或成本口径意外变化，立即回滚对应上游 hunk。
- **P2**：Tool Observation 无法重建、正常 Run 出现未解释事件、并发死锁、不同 cwd 得到不同 patch、cleanup 删除 Executor 文件或出现 root 外写入，禁止进入 P3。
- **P3**：任一 false-pass fixture 产生 verified、环境错误变成代码失败或 base snapshot 被污染，Controller 只能保留实验 flag，不得默认启用。
- **P4**：Harness 异常/timeout 被判 PASS、验证改变 patch、Verification Contract 可静默修改，回滚完成门禁默认启用。
- **P5**：成功保护或正常长任务出现误杀，或 stop decision 无决定性 Ledger 事件，不启用 no-progress 默认策略。
- **P6**：角色 usage 无法相加到 Run total，或删除某角色后 Controller/Harness 行为变化，不得形成角色消融结论。

## 6. 实施节奏与审查点

建议按以下独立变更提交：

1. P0 恢复与测试收集；
2. P1a Scripted/Fake fixture + 旧行为合约；
3. P1b Agent/Context/LLM 上游补丁；
4. P1c session/grep/UTF-8 低耦合补丁；
5. P2a WorkspaceExecution；
6. P2b ToolRuntime/effect；
7. P2c RunLedger/summary；
8. P3 Controller/FinishReason；
9. P4 Contract/Harness；
10. P5 no-progress；
11. P6 paired experiments；
12. P7 仅在证据触发时启动。

每个审查点回答：

- 这个 Module 通过 deletion test 吗？
- 是否出现第二事实源或 pass-through Seam？
- 是否至少存在两个真实 Adapter，还是只为未来预建？
- 测试是否通过 Module Interface，而不是穿透内部 Implementation？
- feature flag 关闭时，旧 Experiment Arm 是否仍可复现？
- 当前退出门槛是否有自动证据？

## 7. 第一批可执行工作

下一开发任务只执行 P0，不进入产品重构：

1. 保护损坏 index 和工作树；
2. 创建干净实施位置；
3. 生成工作树差异与 NUL 文件清单；
4. 恢复 README 的可读副本但保留损坏版本；
5. 建立可运行的 dev 环境；
6. 固定 pytest 收集和 CI；
7. 交付 recovery manifest、测试报告和 P0 退出门槛状态。

P0 完成后，下一任务先提交 P1a 的失败测试；在测试真实复现旧问题前，不移植任何上游 Implementation。
