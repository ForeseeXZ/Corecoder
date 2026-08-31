# CoreCoder P2 完成报告

## 结论

P2 的开发和跨平台验收已经完成。一次 Repair Run 现在从固定 Workspace 出发，经 ToolRuntime 得到结构化 Tool Observation，并写入可重建的 Run Ledger。Windows 本地全量测试通过；GitHub Actions 已在 Ubuntu、macOS、Windows 的 Python 3.10–3.13 全部通过。

## 完成内容

### P2a：WorkspaceExecution

- 固定绝对 Git 根目录和基线提交，不受进程 cwd 改变影响。
- Workspace Snapshot 同时记录 tracked patch 与 untracked 文件内容身份。
- Reviewer 只能清理自己创建的 scratch；预先存在的用户目录会被拒绝并保留。
- 文件工具绑定 Workspace，拒绝写到根目录以外。
- SWE-bench runner、Planner、Executor、Reviewer 使用同一个 Workspace，不再修改进程全局 cwd 或共享 Bash cwd。
- Git 文件名按文件系统编码处理；Linux 保留非 UTF-8 文件名、软链接目标和执行位变化。

### P2b：ToolRuntime

- 状态包括 success、bad arguments、non-zero、timeout、blocked、exception、cancelled 和 truncated。
- 参数校验失败不会进入工具实现；损坏 JSON 不再静默变成空参数。
- 每个工具声明 read/write/process effect；只读组可并发，写入和进程调用串行。
- Tool Observation 保留调用 ID、参数哈希、状态、退出码、完成顺序、输出哈希和模型可见文本。
- 超长输出写 artifact，Ledger 保存长度、哈希与引用。
- 子 Agent 只能继承父 Agent 已授予的能力，不能从全局 registry 找回 Bash 或写入能力。

### P2c：Run Ledger

- JSONL 只追加记录 run、Model Turn、tool started/finished、Workspace Snapshot、compression 和 abort/finish 事实。
- summary 完全由 Ledger 归约，重复读取结果一致。
- 正常调用 started/finished 一一配对；受控取消写 cancelled 与 run_aborted。
- 旧 transcript 继续可读，但明确标记 `observation_unknown=true`。
- SWE-bench run 额外生成 `run-ledger.jsonl`、`ledger-summary.json`、`workspace-snapshots.json` 和 `artifacts/`。

## 测试方案与结果

| 范围 | 覆盖行为 | 本地结果 |
|---|---|---|
| Workspace | 不同 cwd、tracked/untracked、中文空格路径、越界写入、scratch 所有权 | 通过 |
| Bash | blocked、non-zero、timeout、UTF-8、连续 `cd a && cd b` | 通过 |
| 调度 | read Barrier 真并发，write/process 串行，模型调用顺序与完成顺序可重建 | 通过 |
| Tool JSON | 分片重建、损坏 JSON、重复/缺失 ID、脱敏片段与哈希 | 通过 |
| Ledger | started/finished 配对、取消归因、artifact 哈希、确定性 summary、旧 transcript | 通过 |
| 能力边界 | 顶层和子 Agent 均无法调用未授予工具 | 通过 |
| Linux 专属 | 执行位、非 UTF-8 文件名、外部软链接不跟随 | Ubuntu 3.10–3.13 全部通过 |
| P0/P1 回归 | Agent、Context、LLM、Session、工具与 LiteLLM 合约 | 通过 |

本地门禁：`114 passed, 3 skipped`。3 个 skip 均为只能在 POSIX/Linux 验证的测试。Ruff、compileall、`pip check` 和 `git diff --check` 均通过。

远端门禁：[GitHub Actions CI 33402174020](https://github.com/ForeseeXZ/Corecoder/actions/runs/33402174020) 全部通过，包括 12 个操作系统/Python 组合、OpenAI 1.x 兼容通道和 package build。

## Linux 服务器验收步骤

1. 全新 clone，不复用 Windows 虚拟环境。
2. 使用 Python 3.10–3.13 创建虚拟环境并安装 `.[dev]`。
3. 运行 pytest、Ruff、compileall 与 `pip check`。
4. 确认 Linux 专属 3 项测试实际执行而不是 skip。
5. 使用 ScriptedLLM 跑一次离线 Repair Run，核对 Ledger、summary、snapshot 和 artifact 哈希。
6. 安装 `.[eval]` 后检查 Docker 与目标 SWE-bench 镜像，再运行一题 L2 smoke。

第 6 步需要 Linux Docker 和 SWE-bench 镜像，当前 Windows 本机没有该环境，因此不伪造通过结论；它是进入真实批量测评前的服务器门禁。
