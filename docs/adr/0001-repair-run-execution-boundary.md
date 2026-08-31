# ADR 0001: Repair Run 使用固定 Workspace、effect 调度和单一 Run Ledger

## 状态

已接受（P2）。

## 背景

旧实现依赖进程全局 cwd、共享 Bash cwd 和回调 transcript。它们无法可靠回答一次工具调用在哪里执行、是否超时或被拦截、并发顺序如何，以及中断后哪些调用仍未结束。Windows 本地运行与 Linux 测评还会放大路径、换行、文件权限和 Shell 差异。

## 决定

1. 每个 Repair Run 启动时解析一次 `WorkspaceExecution`，固定绝对 Git 根目录和基线提交。
2. 文件工具绑定该 Workspace 并拒绝越界路径；Bash 使用实例级 cwd，不再依赖进程全局 cwd。
3. 每个工具声明 `read`、`write` 或 `process` effect。连续只读调用可以并发，write/process 与其他调用隔离并按模型调用顺序执行。
4. `ToolRuntime` 直接产生结构化 Tool Observation，不从返回字符串推断退出码、超时或拦截状态。
5. Run Ledger 是 Repair Run 事件的唯一新事实源。旧 transcript 保留只读兼容，但缺失的 Observation 必须标记为 unknown。
6. 大输出写入独立 artifact；Ledger 只保存摘要、长度、哈希和引用。

## 结果

- Planner、Executor、Reviewer 和受限子 Agent 可以共享同一个 Workspace 与 Ledger，同时保持各自显式能力集。
- 不同进程 cwd 不再改变相对路径含义；多个 Repair Run 不再共享可变工具实例。
- Linux 可保留软链接、非 UTF-8 Git 文件名和执行位信息。
- Bash 仍是具有进程能力的工具，不等同于操作系统级沙箱；真正的不可信隔离继续由 SWE-bench Docker 环境承担。
- 新 summary 必须从 Run Ledger 归约；旧 transcript 仅用于兼容查看。
