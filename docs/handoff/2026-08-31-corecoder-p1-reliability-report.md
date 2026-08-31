# CoreCoder P1 可靠性阶段报告

日期：2026-08-31

## 简单结论

P1 的本地开发与 Windows 验证已经完成。这个阶段没有增加新的产品功能，主要是让 Agent 在工具调用、异常、中断、上下文压缩和本地文件处理方面更可靠、更容易测试。

测试全部使用预设回答的 Scripted LLM，不调用真实模型、不消耗 API 额度，也不依赖网络。完整测试从 P0 的 66 项增加到 89 项，连续执行 10 次全部通过。

Ubuntu 尚无法在本机验证：当前 Windows 没有启用 WSL，也没有已安装的 Linux 发行版。仓库 CI 已包含 Ubuntu；需要在审查并提交当前改动后由 GitHub Actions 完成最终跨平台确认。

## 完成内容

### Agent 工具循环

- 建立 `ScriptedLLM`，按固定剧本返回 Model Turn，记录 Agent 发给模型的消息。
- 冻结普通工具调用、最终文本和 Model Turn 上限行为。
- 受限 Agent 只能执行实例明确持有的 Tool，不再回退到全局 Tool 注册表。
- 调用前同时检查 Tool 参数 Schema 和 Python 签名。
- 缺失/多余参数归为 bad arguments；Tool 内部 TypeError 归为 execution error。
- KeyboardInterrupt 发生时，为每个尚未完成的调用补 `[interrupted]` Tool Observation。
- 并行工具即使完成顺序不同，Tool Observation 仍按模型原调用顺序写回。

### Context 压缩

- ContextManager 和 CompressionManager 共用安全切分规则。
- 保留最近消息时不会从一组 Tool Observation 中间切开。
- 多工具 Model Turn 的 Assistant 声明和全部 Tool Observation 始终作为完整组保留。

### LLM 边界

- 只有 BadRequestError 才会触发移除 `stream_options` 的兼容请求。
- 限流、超时和连接错误不再额外放大为第二整轮请求。
- usage 字段为 null 时按 0 统计。
- APIError 没有 status_code 时保留原异常，不再产生 AttributeError。

### Session 与文件搜索

- 损坏 Session JSON 返回缺失结果，不让 CLI 崩溃。
- Session 明确使用 UTF-8，默认 ID 保持不冲突。
- Session ID 阻止 Unix/Windows 路径穿越，并对超长名称生成稳定短名。
- grep 达到文件扫描上限时明确报告 incomplete。
- 搜索根目录的祖先名为 `build` 时，不再误跳过整个搜索范围。
- grep 文件读取明确使用 UTF-8，并对异常字节采用替换策略。

## 测试映射

| 合约 | 结果 | 测试位置 |
|---|---|---|
| A01 普通工具闭环 | 通过 | `tests/test_agent_contract.py` |
| A02 Model Turn 上限 | 通过 | `tests/test_agent_contract.py` |
| A03 中断补齐 Tool Observation | 通过，含单/多工具 | `tests/test_agent_contract.py` |
| A04 实例能力隔离 | 通过 | `tests/test_agent_contract.py` |
| A05/A06 参数与内部 TypeError 分类 | 通过 | `tests/test_agent_contract.py` |
| A07 并行结果顺序 | 通过，Barrier 实际验证并行 | `tests/test_agent_contract.py` |
| A08/A09 两条 Context 路径工具组完整性 | 通过 | `tests/test_context_contract.py` |
| L01/L02 stream_options 与瞬态错误 | 通过 | `tests/test_llm_contract.py` |
| L03 null usage/APIError | 通过 | `tests/test_llm_contract.py` |
| S01/S02/S03 Session | 通过 | `tests/test_session.py`、`tests/test_core.py` |
| G01/G02 grep | 通过 | `tests/test_tools.py` |

## 上游来源与本地适配

比较源为临时只读 clone `he-yufeng/CoreCoder`，检查时 HEAD 为 `a03ef36412e432fc49d972d4007b36ce44ec5d9a`。

- `4e457f1`（v0.4.0）：提供 Agent 能力隔离、参数绑定、中断补齐、Context 安全切分、LLM usage/错误处理、Session 损坏处理等可靠性意图。
- `9268d3d`：提供 grep 达到文件扫描上限时必须明确警告的意图。
- 当前仓库 `public-origin/main` 为 `0cdebf9`，包含默认 Session ID 防碰撞，但早于上面两项上游改动。

本地没有整包复制上游文件。Agent 的 MCP 初始化/关闭、CompressionManager、Planner/Reviewer 和实验路径均保留；Context 安全切分被提取成两条本地压缩路径共享的规则。Tool 参数检查还结合了本地 JSON Schema，覆盖使用 `**kwargs` 的 Tool。

## 验证结果

- `python -m pytest -q`：89 passed。
- Windows 连续完整回归：10/10 通过，每轮 89 项，约 12.3 秒。
- `python -m ruff check corecoder tests`：通过。
- `python -m compileall -q corecoder tests`：通过。
- `git diff --check`：通过。
- 外部模型/API 调用：0。

## GitHub Issues

- 已创建并采用五个默认工作流标签。
- P1 跟踪 Issue：`https://github.com/ForeseeXZ/Corecoder/issues/1`。
- 当前仅剩远端 Windows/Ubuntu CI 验证，Issue 保持打开并转为 `ready-for-human`，等待人工审查提交边界。

## 未完成项

1. 当前改动尚未创建 commit 或 push。
2. `corecoder/mcp_bridge.py` 是 P0 前已有的用户改动，提交时需要与 P0/P1 修改明确分组。
3. GitHub Actions 的 Windows/Ubuntu matrix 尚未被当前改动触发。
4. 上述远端 CI 完成前，P1 状态为“本地完成，跨平台待确认”。
