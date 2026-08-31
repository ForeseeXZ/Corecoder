# CoreCoder Agent 可靠性问题分析与分阶段改造建议

> 日期：2026-08-30  
> 目标仓库：`D:\Software\Code\Vscode_workspace\corecoder\CoreCoder`  
> 当前分支：`my-baseline`  
> 当前提交：`68143a37136baef9b45b6750e2778e75e55687e1`  
> 文档用途：交给一个新的开发任务继续实现；本文不代表这些改造已经完成。

## 0. 当前状态与准确接续点

- `[verified]` CoreCoder 是一个 Python 3.10+ 编码 Agent，CLI 入口为 `corecoder.cli:main`，SWE-bench 单题入口为 `eval/run_swebench.py`。
- `[verified]` 当前核心执行方式仍是通用 `Agent.chat()` 工具循环；Planner、Executor、Reviewer 都建立在这个循环之上。
- `[verified]` 当前 transcript 只记录流式 token、工具名称和工具参数，没有记录工具返回值、退出码、轮次、状态转换、patch 变化和终止原因。
- `[verified]` 当前仓库的 Git index 已损坏，`git status --short` 返回 `fatal: index file corrupt`。在修复或迁移到干净 checkout 前，不应直接修改产品代码。
- `[verified]` 本轮只读分析没有修改 CoreCoder，也没有运行单元测试、SWE-bench 或重新评分。
- `[proposed]` 下一任务应从“保护现场并恢复可验证 Git 工作区”开始，然后按本文任务 T00→T12 逐项实施和验收。

## 1. 目标、范围和非目标

### 1.1 目标

把 CoreCoder 从“依赖 Prompt 的自由工具循环”逐步改造成“证据驱动、可观测、可停止、可验证的代码修复控制系统”，重点解决：

1. 模型错误和工具/工作区错误无法区分；
2. 模型可以没有验证证据就宣布完成；
3. 困难任务会重复搜索并持续消耗 token；
4. Reviewer 在看到 patch 后自编测试，容易复制 Executor 的错误假设；
5. Planner/Reviewer 增加了复杂度，但现有消融没有显示稳定收益。

### 1.2 本阶段范围

- Agent 运行状态与停止策略；
- Workspace 与工具执行边界；
- Trace 完整性与可回放性；
- 修改前复现、修改后验证与完成门禁；
- Planner/Reviewer 的职责调整；
- 固定任务集上的 paired ablation。

### 1.3 非目标

- 不修改底层 Transformer、Attention、MoE 等神经网络结构；
- 不立即微调或蒸馏模型；
- 不继续增加更多 Agent 角色；
- 不以提高 `max_rounds` 代替收敛控制；
- 不把当前 trace 直接当成高质量训练数据；它缺少 observation 和可靠成功标签；
- 不在同一批改动中重写 MCP、CLI、评测系统和全部工具。

## 2. 证据账本

### 2.1 已验证的代码事实

| 事实 | 证据位置 | 影响 |
|---|---|---|
| `Agent.chat()` 最多运行固定轮数，模型没有 tool call 时直接返回 | `corecoder/agent.py:86-137` | “模型停止说话”被等价成“任务正确完成” |
| 工具调用回调发生在执行前，回调没有接收工具结果 | `corecoder/agent.py:107-128` | transcript 无法知道模型实际观察到了什么 |
| 多个工具调用统一进入并行执行 | `corecoder/agent.py:119,144+` | 只读搜索与有副作用操作没有明确边界 |
| Planner 是一次性只读 Agent，默认最多 20 轮 | `corecoder/plan.py:264+` | 不能通过运行复现来证伪规划假设，也不会随新证据更新计划 |
| Reviewer 在 Executor 完成后生成验证脚本并给出 verdict | `corecoder/review.py:267+` | 验证可能继承实现假设，产生 false pass |
| context compression 主要依据当前上下文占用触发 | `corecoder/context.py`、`corecoder/compress.py:207+` | 无法处理“上下文不高但累计 token 和重复调用很高”的循环 |
| SWE-bench runner 记录 token/tool-start 事件 | `eval/run_swebench.py:213+` | Trace 是行为片段，不是完整因果事件流 |

### 2.2 已验证的实验事实

来源：`README_RESEARCH_NOTES.md`、`eval/BASELINE_ANALYSIS.md` 以及抽样的 `eval/runs_mimo/**/summary.json`、`transcript.jsonl`。

- `[verified]` 文档记录的 50 题 baseline：flash 26/50，pro 29/50；换强模型只有有限增益。
- `[verified]` 24 个 baseline 未解决任务中：0 个纯文件定位失败，20/24 属于定位后逻辑错误或修改不完整，4/24 属于不收敛/空 patch。
- `[verified]` 四路共同 9 题：baseline 4/9，Reviewer 5/9，Planner 5/9，Planner+Reviewer 5/9；三个增强臂多解的是同一题，不能证明稳定因果增益。
- `[verified]` `django__django-11790` 的最终说明把“生成了 `maxlength` 属性”当成修复正确的主要证据，表现出验证目标与真实问题语义未对齐。
- `[verified]` `django__django-12304` 定位和机制查找正确，但把属性直接加入 Enum 类体，忽略其可能成为 enum member；属于模型语言语义判断错误。
- `[verified]` `sphinx-doc__sphinx-8548`、`sphinx-doc__sphinx-9461` 存在达到最大工具轮数的运行；困难任务表现为高累计 token 和搜索不收敛。
- `[verified]` compression 消融样本中有任务的上下文峰值没有达到三层压缩阈值，因此该组结果不能直接归因于压缩机制。

### 2.3 仍需确认的事项

- `[unknown]` 当前损坏的 Git index 中是否曾包含未提交的 staged 状态；不能直接删除或重建。
- `[unknown]` Reviewer 路径问题影响了多少历史运行。`run_swebench.py` 在切换目录后仍传递 `repo_dir`，而 Reviewer 再构造 `Path(repo_dir)`，需要用测试稳定复现后才能定论。
- `[unknown]` 不同 run 目录中的重复结果哪些是权威源；目前存在多层 `runs_mimo/runs_mimo/...` 和时间戳副本。
- `[unknown]` 当前模型端温度、服务端随机性和限流对 9/20 题消融的影响程度。
- `[unknown]` 在不改变模型的情况下，完成门禁和无进展控制能提高多少 resolved rate；必须通过 paired run 验证。

## 3. 当前边界图

```text
problem_statement
       │
       ▼
eval/run_swebench.py
       │  准备 checkout / 参数 / callback
       ├──────────── optional Planner ──────┐
       │                                     │ 只读计划文本
       ▼                                     ▼
                    Executor Agent.chat()
                   ┌─────────┴─────────┐
                   │                   │
              Model Adapter        Tool implementations
                   │                   │
              tool calls ◄──────── tool results
                   │
                   └──── optional Reviewer → revise
                                        │
                                        ▼
                               patch / summary / transcript
```

当前最关键的问题是：控制逻辑、工作区、验证和 trace 都围绕 Agent Loop 松散拼接，没有任何一个 Module 持有完整的“任务现在处于什么状态、掌握了什么证据、为什么允许结束”。

## 4. 目标架构

```text
EvaluationRunner
      │
      ▼
WorkspaceExecution ───────────────► TraceLedger
      │                                ▲
      ▼                                │ 每个请求、观察、diff、状态都记账
TaskExecutionController ────────────────┘
      │
      ├── Diagnose: 建立问题复现与候选假设
      ├── Patch:    单写者修改，记录 patch snapshot
      ├── Verify:   运行冻结的验证契约与回归检查
      └── Finish / Recover / Unresolved
             │
             ├── ModelGateway: 模型请求、工具调用解析、角色 usage
             ├── ToolRuntime:  effect-aware 工具执行
             └── VerificationHarness: 确定性执行与结构化结果
```

### 建议的数据边界

以下只是建议的最小概念，不要求第一步就一次性设计完整：

- `RunEvent`：一次不可变运行事件；
- `WorkingState`：假设、证据、反证、已改文件、待验证项；
- `VerificationContract`：修改前应失败、修改后应通过的行为契约；
- `VerificationResult`：patch、before/after、回归、环境健康和置信原因；
- `FinishReason`：verified、unresolved、no_progress、budget_exhausted、environment_error、model_error。

## 5. 实施原则：每个任务都形成闭环

每个任务必须同时满足：

1. 有一个窄而完整的端到端行为，而不只是创建抽象类；
2. 先用单元测试或固定 fixture 证明旧行为的问题；
3. 实施后通过自动化验收；
4. 保留 baseline 默认路径或明确写出迁移策略；
5. 生成可检查的 artifact；
6. 不满足退出门槛时，不进入下一任务。

验收分四级：

- **L0 单元级**：不调用远程模型；使用 fake LLM/fake tool。
- **L1 录制回放级**：使用脱敏、固定 trace fixture。
- **L2 单题 smoke**：1 个简单成功任务 + 1 个代表性失败任务。
- **L3 实验级**：固定 20 题 paired ablation，模型和参数一致。

## 6. 分阶段任务清单

### T00（HITL）：保护现场并恢复可验证 Git 工作区

**目的**：在不丢失潜在 staged/unstaged 内容的前提下，获得可工作的 Git 状态。

**步骤**

1. 记录当前 commit、branch、remotes 和整个工作目录的备份位置；
2. 单独保留损坏的 `.git/index`，不要直接覆盖；
3. 优先在新的干净 clone/worktree 中实施本文改造；
4. 对现有目录只做只读差异恢复，确认是否存在未提交源码；
5. 确认 `git status --short`、`git diff`、`git diff --cached` 都可运行。

**验收**

- [ ] Git 三个检查命令均成功；
- [ ] 当前源码与原目录做过文件级对照；
- [ ] 潜在用户改动有独立备份；
- [ ] 后续开发位置被明确记录。

**退出门槛**：Git 状态不再报 index 损坏。未满足时禁止进入 T01。

---

### T01（AFK）：冻结现状的 Agent Loop 合约测试

**目的**：先把关键旧行为变成可复现测试，避免后续重构无法判断回归。

**改动范围**：`tests/`、必要的 fake LLM/fake tool fixture；暂不改产品行为。

**步骤**

1. Fake LLM 依次返回：工具调用、普通文本、损坏工具参数、无限重复工具调用；
2. Fake tool 返回成功、异常、非零退出、超长输出；
3. 固定并测试当前消息顺序、并行结果顺序和 max-rounds 行为；
4. 建立两个最小 failure fixture：过早完成、重复调用直到上限。

**自动验收（L0）**

- [ ] 测试不访问网络；
- [ ] 能稳定复现“无 tool call 即完成”；
- [ ] 能稳定复现“重复调用直到 max rounds”；
- [ ] 能稳定复现工具异常目前如何进入 messages。

**artifact**：测试报告和 fixture 说明。

**Blocked by**：T00。

---

### T02（AFK）：TraceLedger 最小闭环——记录完整工具观察

**目的**：从一次 Agent tool call 到最终 transcript，完整记录 request 和 result。

**改动范围**：`corecoder/agent.py`、trace 事件定义、`eval/run_swebench.py`、tests。

**步骤**

1. 为每个 run、agent、round、tool call 生成稳定 ID；
2. 分别写入 `tool_started` 和 `tool_finished`；
3. `tool_finished` 至少包含状态、耗时、结果长度、是否截断和错误类别；
4. 超长 stdout 保存到独立 artifact，Ledger 只放摘要和引用；
5. 保留旧 transcript 的读取兼容，或明确版本号并提供一次性转换脚本。

**自动验收（L0/L1）**

- [ ] 每个 `tool_started` 恰好对应一个 `tool_finished`；
- [ ] 成功、异常、超时、非零退出均有明确状态；
- [ ] 可以从 Ledger 重建模型实际收到的 tool observation；
- [ ] transcript 不再依赖 token 碎片推断工具是否成功。

**人工验收**：打开一个 JSONL，能在 1 分钟内回答“第 6 轮模型调用了什么、看到了什么、是否截断”。

**Blocked by**：T01。

---

### T03（AFK）：TraceLedger 状态、diff 与终止原因闭环

**目的**：让 trace 能解释一次运行为何推进、为何停止。

**步骤**

1. 记录模型请求开始/结束、round、phase、usage 和 finish reason；
2. 每轮计算 workspace diff hash，变化时记录 `patch_changed`；
3. 记录 context compression 前后规模和触发层级；
4. 记录明确 `FinishReason`；
5. `summary.json` 的核心统计由 Ledger 归约生成，并校验一致性。

**自动验收（L0/L1）**

- [ ] max-round、普通结束、模型错误和工具错误可区分；
- [ ] Ledger 与 summary 的轮数、tool 数、usage 一致；
- [ ] 相同 fixture 重放得到相同归约结果；
- [ ] schema 校验能够拒绝缺失关键字段的事件。

**Blocked by**：T02。

---

### T04（AFK）：WorkspaceExecution 绝对路径闭环

**目的**：消除 `os.chdir`、相对 repo_dir 和工具私有 cwd 之间的隐藏耦合。

**改动范围**：SWE-bench runner、Agent/工具初始化、Reviewer Git 操作、子 Agent。

**步骤**

1. 启动时将 checkout root `resolve()` 为绝对路径；
2. Workspace 作为显式依赖传入工具、Reviewer 和子 Agent；
3. Git diff、untracked、cleanup 全部相对同一个 root；
4. 添加“runner 已 chdir + 仍传入相对路径”的回归测试；
5. Reviewer 只读取 Executor 完成时生成的固定 patch snapshot。

**自动验收（L0）**

- [ ] 从仓库外、仓库内、不同 cwd 启动得到相同 diff；
- [ ] Reviewer 能看见 fixture 中的已知修改；
- [ ] Reviewer 临时文件不会进入最终 patch；
- [ ] cleanup 不删除 Executor 创建的文件。

**Blocked by**：T01；建议在 T03 后实施，以便 trace 记录修复效果。

---

### T05（AFK）：Tool effect 与安全并行闭环

**目的**：只并行真正独立的只读调用，保证写入和进程操作顺序可解释。

**步骤**

1. 为工具声明 `read`、`write`、`process` effect；
2. 同一批纯 read 工具允许并行；
3. edit/write/bash/process 默认串行；
4. 发生写操作后刷新 diff hash 和 Working State；
5. Ledger 记录调度顺序和并行组。

**自动验收（L0）**

- [ ] 两个 read fixture 确实并行；
- [ ] read+write、两个 write 不并行；
- [ ] 多个工具结果返回顺序与 tool call ID 对齐；
- [ ] 子 Agent 不能绕过写权限。

**Blocked by**：T02、T04。

---

### T06（AFK）：最小完成门禁闭环

**目的**：把“模型停止调用工具”和“任务已验证完成”分离。

**步骤**

1. 模型输出普通文本时，不再自动映射成 verified；
2. Controller 检查 patch 是否存在、是否运行过验证、环境是否健康；
3. 未满足条件时进入 `needs_verification` 或 `unresolved`；
4. 最终结果必须带结构化 FinishReason；
5. 保留配置开关，支持与旧 baseline paired 对照。

**自动验收（L0/L1）**

- [ ] “我已经修复”但无 patch → 不得 verified；
- [ ] 有 patch 但未验证 → 不得 verified；
- [ ] 验证命令因环境失败 → environment_error，而不是 success/fail；
- [ ] 符合最小证据链的 fixture → verified。

**人工验收（L2）**：在 `django__django-11790` 类 fixture 上，错误的 post-hoc 检查不能产生强通过。

**Blocked by**：T03、T04。

---

### T07（AFK）：WorkingState 与 Evidence Ledger 闭环

**目的**：把关键问题状态从易丢失的聊天历史中分离出来。

**最小字段**

- 当前阶段；
- 候选假设、置信度；
- evidence/counterevidence 引用；
- 已检查位置；
- 已改文件；
- reproduction 状态；
- pending verification；
- environment health。

**步骤**

1. WorkingState 由确定性 Controller 更新，不依赖自由文本 summary 作为唯一事实源；
2. context compression 后仍保留完整结构化状态；
3. Prompt 只渲染当前需要的状态视图；
4. trace 保存状态版本及变更原因。

**自动验收（L0/L1）**

- [ ] compression 前后关键证据 ID 不丢失；
- [ ] 重复读取同一路径可被统计；
- [ ] 假设被反证后不能继续作为 active hypothesis；
- [ ] 可从 Ledger 重建每个阶段的 WorkingState。

**Blocked by**：T03、T06。

---

### T08（AFK）：无进展检测与自适应预算闭环

**目的**：解决低上下文占用但高累计 token 的搜索循环。

**建议进展信号**

- 新文件/符号证据；
- 新的可证伪假设；
- patch hash 变化；
- failing test 数量减少；
- environment error 被解决；
- 重复 tool signature 比例。

**步骤**

1. 定义可配置阶段预算，而不是只有全局 50 轮；
2. 连续 N 轮无新证据时先总结并要求换假设；
3. 再次无进展时进入 unresolved/no_progress；
4. 不因简单增加 context compression 而重置无进展计数；
5. 输出 stop decision 的证据。

**自动验收（L0/L1）**

- [ ] 无限重复 read fixture 在远低于 50 轮时停止；
- [ ] 正常取得新证据的长任务不会被误杀；
- [ ] environment_error 不被计为代码假设失败；
- [ ] 每次预算调整可由 trace 解释。

**实验验收（L2）**：对 `sphinx-doc__sphinx-8548` 或录制 fixture，工具调用和累计 token 显著下降，同时保留已有 patch 产出能力。

**Blocked by**：T07。

---

### T09（HITL→AFK）：修改前 VerificationContract 闭环

**目的**：验证目标必须在看到实现结果前形成，减少自我确认偏差。

**HITL 决策**

- 哪些任务允许无法本地复现；
- weak oracle 是否允许最终 verified；
- 验证契约由同模型、不同模型还是确定性模板产生。

**建议默认政策**

1. 优先生成最小 reproducer；
2. 保存 before 结果，要求目标行为修改前失败；
3. 冻结 contract 后才进入 Patch；
4. 如果无法复现，明确标记 `oracle_strength=weak`；
5. Reviewer 不得悄悄修改冻结契约，只能提出修订请求并记录原因。

**自动验收（L0/L1）**

- [ ] 修改前已经通过的脚本不能作为区分性证据；
- [ ] contract 能区分 before/after；
- [ ] 修改 contract 会产生版本事件；
- [ ] false-pass fixture 被拒绝。

**Blocked by**：T06、T07。

---

### T10（AFK）：确定性 VerificationHarness 闭环

**目的**：LLM 提出验证意图，但成功条件由确定性 harness 汇总。

**建议结果字段**

- `patch_present`；
- `reproduction_before`；
- `behavior_after`；
- `regression_tests`；
- `environment_health`；
- `oracle_strength`；
- `confidence_reason`。

**步骤**

1. 在干净 base 和 patched workspace 上运行同一 contract；
2. 区分 test failure、timeout、dependency failure 和 runner failure；
3. 保留 stdout/stderr artifact，Ledger 保存引用；
4. Controller 是唯一能把 VerificationResult 转成 FinishReason 的组件；
5. Reviewer 临时文件始终隔离于最终 patch。

**自动验收（L0/L1）**

- [ ] before fail + after pass 才能给出强行为证据；
- [ ] before/after 都 pass 被标为非区分性检查；
- [ ] 环境损坏不会被解释成代码失败；
- [ ] harness 自身异常不会默认为 PASS；
- [ ] 清理后 patch 与验证前 snapshot 一致。

**Blocked by**：T04、T09。

---

### T11（AFK）：缩减 Reviewer/Planner 职责并进行组件消融

**目的**：只有在新控制和验证基础上，重新判断 Planner/Reviewer 是否值得保留。

**建议职责**

- Planner：只维护候选假设、影响范围和未决问题，不宣告正确；
- Executor：唯一写者；
- Reviewer：寻找反例、检查漏改范围，不自行决定最终 PASS；
- Controller：依据 VerificationResult 决定阶段转换。

**实验臂**

1. 新 Controller + 无 Planner/Reviewer；
2. + Reviewer 反例搜索；
3. + 动态 Planner（只在多假设或多文件时触发）；
4. 旧 baseline 对照。

**验收（L2/L3）**

- [ ] 每个角色的 token/tool usage 独立统计；
- [ ] 每个角色的新增决策能追溯到证据；
- [ ] 若组件没有稳定收益，应允许删除而不破坏 Controller；
- [ ] 不以单次多解 1 题宣称有效，需要逐题归因或重复运行。

**Blocked by**：T08、T10。

---

### T12（AFK）：ModelGateway 错误语义与角色会话闭环

**目的**：把 provider、工具参数解析、usage 和角色会话从业务控制中隔离。

**步骤**

1. 损坏 JSON 参数返回显式 `ToolCallParseError`，保留原始片段；
2. OpenAI/LiteLLM 的流式 tool-call 归一化到同一结果；
3. 每个角色独立 usage ledger；
4. 共享底层 client 时仍保证并发计数正确；
5. rate limit、timeout、provider error 与任务失败分开。

**自动验收（L0）**

- [ ] 分片 JSON、损坏 JSON、重复 call ID 有固定行为；
- [ ] 429 不再被统计成模型完成或代码验证失败；
- [ ] Planner/Executor/Reviewer usage 可相加并等于 run total；
- [ ] 切换 Adapter 不改变 Controller 测试。

**Blocked by**：T03；可在 T08-T11 之外并行开发，但合并前需通过完整回归。

## 7. 最终实验验收方案

### 7.1 任务集

至少包括：

- 简单成功保护：从现有 resolved 任务选 3～5 个；
- 自信错误：`django__django-11790`、`django__django-12304`；
- 多文件不完整：从 baseline 分析的 partial 类选 2～3 个；
- 不收敛：`sphinx-doc__sphinx-8548`、`sphinx-doc__sphinx-9461`；
- 固定随机 20 题：使用现有消融交集或重新生成一次不可变 manifest。

### 7.2 控制变量

- 相同 Executor 模型、参数、题目顺序和超时；
- 固定代码 commit 与 Docker 镜像；
- 独立输出目录，禁止覆盖历史结果；
- 记录 rate limit、环境错误和重试；
- 若成本允许，至少重复 3 次；否则必须把单次结果标成弱证据。

### 7.3 指标

正确性：

- resolved rate；
- false verified 数量；
- before/after 区分性验证覆盖率；
- partial multi-file 数量。

收敛与成本：

- 每题累计 prompt/completion token；
- tool calls、重复 tool signature 比例；
- 达到 max-round/no-progress 数量；
- 首次 patch 和首次有效验证的轮次；
- easy solved 任务的额外开销。

可观测性：

- tool started/finished 配对完整率应为 100%；
- summary 与 Ledger 一致率应为 100%；
- 每个 FinishReason 都有可定位证据；
- 环境失败不得混入代码失败。

### 7.4 建议的发布门槛

- 不降低固定成功保护任务的通过率；
- false verified fixture 全部被门禁拦截；
- 重复读取 fixture 在阶段预算内终止；
- 困难任务的 token/tool 调用下降能由 no-progress 事件解释；
- 如果 resolved rate 没有提升，也应诚实报告可靠性、成本或归因能力是否改善。

## 8. 推荐实施顺序

```text
T00 Git 安全恢复
  └─ T01 旧行为合约测试
       ├─ T02 工具 observation trace
       │    └─ T03 状态/diff/finish trace
       │         ├─ T06 最小完成门禁
       │         │    └─ T07 WorkingState
       │         │         └─ T08 无进展控制
       │         │              └─ T09 修改前验证契约
       │         │                   └─ T10 确定性验证
       │         │                        └─ T11 角色消融
       │         └─ T12 ModelGateway（可并行）
       └─ T04 Workspace
            └─ T05 工具 effect
            └─ T10 确定性验证
```

建议里程碑：

- **M1 可诊断**：T00-T03；能可靠回答模型看到了什么、为什么停止；
- **M2 可执行**：T04-T05；路径、diff、并发和工具副作用可靠；
- **M3 可控**：T06-T08；不能无证据完成，重复搜索能主动停止；
- **M4 可验证**：T09-T10；修改前后具有独立验证契约；
- **M5 可评估**：T11-T12 + 固定 20 题；决定哪些角色值得保留。

## 9. 风险、回滚与解释边界

1. **Git 风险**：当前 index 损坏。任何“重建 index”动作都必须先保护用户现场。
2. **行为漂移**：状态机可能降低表面完成率，因为过去的无证据完成会变成 unresolved；这属于预期语义修正，应同时观察 resolved 和 false verified。
3. **验证过拟合**：不要针对 SWE-bench gold patch 或隐藏 test_patch 设计 production 验证；否则评测泄漏。
4. **成本转移**：Reviewer 减少不代表总成本必然下降，VerificationHarness 可能增加测试时间，必须分别记录 LLM 成本和执行成本。
5. **随机性**：小样本 +1 题不能支撑架构结论；单次 paired run 只能作为初步信号。
6. **压缩误判**：当前证据不支持“压缩提升了正确率”；它只能在触发并记录后评估。
7. **模型能力上限**：Enum、类型语义等错误可能仍需要更强模型；控制系统的目标是发现错误、减少虚假成功和无效消耗，不保证替代模型能力。

每个任务的回滚原则：保持 feature flag 或小提交；自动验收未通过时只回滚当前任务，不跨阶段叠加修补。

## 10. 关键文件索引

| 路径 | 作用 |
|---|---|
| `corecoder/agent.py` | 通用 Agent loop、工具调度、停止行为 |
| `corecoder/context.py` | baseline 上下文裁剪 |
| `corecoder/compress.py` | 可选多层上下文压缩与统计 |
| `corecoder/plan.py` | Planner、结构化计划和降级 |
| `corecoder/review.py` | Reviewer、验证脚本、revise/cleanup |
| `corecoder/tools/agent.py` | Search SubAgent 与上下文隔离 |
| `corecoder/tools/bash.py` | shell 执行及 cwd 行为 |
| `corecoder/llm.py` | OpenAI/LiteLLM 适配、tool call 解析、usage |
| `eval/run_swebench.py` | 单题 checkout、Agent 执行、trace/summary/patch |
| `eval/run_swebench_batch.py` | 批量消融与续跑 |
| `eval/BASELINE_ANALYSIS.md` | 24 个 baseline 失败的归因 |
| `README_RESEARCH_NOTES.md` | 实验设计、消融结论和复现入口 |
| `eval/runs_mimo/**/transcript.jsonl` | 原始模型 token/tool-start 记录 |
| `eval/runs_mimo/**/summary.json` | 单题 usage、final text 和组件 metadata |

## 11. 建议命令与验证顺序

以下命令是建议，不代表本轮已经成功执行：

```powershell
# 先确认 Git；当前已知此命令会因 index 损坏失败
git status --short

# Git 恢复后，记录基线
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD

# 安装开发依赖（是否联网由接手任务决定）
python -m pip install -e ".[dev]"

# 无网络单元测试优先
python -m pytest -q

# 单题 smoke；需要对应依赖、API 和评测环境
python eval/run_swebench.py -i django__django-11790 --timeout 900
```

不要在 Git index 未恢复前执行批量格式化、全仓覆盖式修改或删除 `.git/index`。

## 12. 新任务启动清单

按以下顺序读取：

1. 本文；
2. `README_RESEARCH_NOTES.md`；
3. `eval/BASELINE_ANALYSIS.md`；
4. `corecoder/agent.py`；
5. `eval/run_swebench.py`；
6. `corecoder/review.py`、`corecoder/plan.py`；
7. 抽样 `django__django-11790`、`sphinx-doc__sphinx-8548` trace。

第一条安全检查：

```powershell
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --short
```

如果第三条仍报 index 损坏，停止产品修改，先执行 T00。恢复后从 T01 开始；不要直接从状态机或 Reviewer 重构开始。

## 13. 可直接交给新任务的提示词

> 阅读 `2026-08-30-corecoder-agent-reliability-analysis.md`，并在 CoreCoder 仓库中按照 T00→T12 的依赖顺序工作。每次只实施一个任务：先补能复现旧问题的测试，再修改实现，再运行该任务列出的自动化验收，最后记录证据和剩余风险。不要一次性重写 Agent；不要在 Git index 损坏时修改产品代码；不要把模型不调用工具当成验证成功；不要使用 SWE-bench gold patch 或隐藏测试构造 production oracle。每完成一个任务先汇报改动、测试、artifact 和退出门槛，再决定是否进入下一项。

