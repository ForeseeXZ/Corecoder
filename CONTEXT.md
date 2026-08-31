# CoreCoder 代码修复运行上下文

本文统一 CoreCoder 在代码修复、验证与实验中的领域语言，避免实现和文档对“结束”“通过”“记录”等概念产生不同解释。

## Language

**Repair Run**:
针对一个问题描述、一个固定基线提交和一组运行参数进行的一次完整代码修复尝试。
_Avoid_: task run, agent run, execution

**Model Turn**:
Repair Run 中模型接收当前消息并产生文本或工具调用的一次交互。
_Avoid_: round（仅用于配置上限时保留）

**Tool Observation**:
一次工具执行后实际返回给模型的结果及其状态、耗时、截断和错误信息。
_Avoid_: tool output（只表达文本，不表达完整结果）

**Workspace Snapshot**:
Repair Run 在某一时刻相对于固定基线的文件状态和 patch 标识。
_Avoid_: current diff, working tree state

**Run Ledger**:
按发生顺序保存 Repair Run 请求、Tool Observation、Workspace Snapshot、验证和终止事实的不可变事件记录。
_Avoid_: transcript（现有 transcript 只是非完整行为片段）

**Verification Contract**:
在查看最终 patch 前冻结的、用于区分基线行为与目标行为的验证约定。
_Avoid_: reviewer script, test guess

**Verification Result**:
同一 Verification Contract 在基线和修改后 Workspace Snapshot 上执行得到的结构化结论。
_Avoid_: PASS/FAIL text

**Finish Reason**:
Repair Run 停止时对 verified、unresolved、no progress、budget、environment 或 model failure 的唯一结构化归因。
_Avoid_: final answer, done, success

**Experiment Arm**:
在固定任务清单和控制变量下启用同一组增量的实验配置。
_Avoid_: mode, variant

## Relationships

- 一个 **Repair Run** 包含一个或多个 **Model Turn**。
- 一个 **Model Turn** 可以产生零个或多个 **Tool Observation**。
- 一个 **Repair Run** 产生零个或多个 **Workspace Snapshot**，每个 Snapshot 均绑定同一固定基线。
- 一个 **Run Ledger** 记录且只记录一个 **Repair Run** 的事实。
- 一个 **Verification Contract** 可以在两个 **Workspace Snapshot** 上执行，并产生一个 **Verification Result**。
- 一个 **Verification Result** 与运行健康状态共同决定且只决定一个 **Finish Reason**。
- 一个 **Experiment Arm** 包含多个独立 **Repair Run**，不得共享可变 Workspace。

## Example dialogue

> **Dev:** “模型没有继续调用工具，这个 Repair Run 可以算成功吗？”
> **Domain expert:** “不能。那只表示当前 Model Turn 结束；只有 Verification Result 满足冻结的 Verification Contract，Repair Run 才能得到 `verified` Finish Reason。”

## Flagged ambiguities

- “完成”过去同时表示模型停止输出和修复已验证；现在前者称 **Model Turn 结束**，后者只能由 **Finish Reason** 表达。
- “transcript”过去被当成完整 trace；现有文件缺少 Tool Observation，只保留旧格式名称，新事实源统一称 **Run Ledger**。
- “测试失败”过去混合代码断言、依赖缺失和 runner 异常；这些必须在 **Verification Result** 中分开。
- SWE-bench 官方隐藏测试只用于离线评分，不属于 production **Verification Contract**，不得被 Repair Run 读取。
