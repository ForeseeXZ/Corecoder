# CoreCoder P0 恢复与绿基线报告

日期：2026-08-30

## 结论

P0 的本地恢复与快速门禁已经完成。Git 对象库健康，损坏范围确认是全零 `.git/index` 和 11 个全零 tracked 文件；索引与文件均在独立备份后恢复。当前 tracked 用户改动 `corecoder/mcp_bridge.py` 保持不变，3,943 个 untracked 路径未删除、未覆盖。

本地测试、lint 和编译门禁全部通过。远端 Windows/Ubuntu CI 已配置但尚未触发，因此 P0 的“两个平台至少各成功一次”仍是唯一未闭环的退出条件。

## 现场保护与恢复证据

- Branch：`my-baseline`
- HEAD：`68143a37136baef9b45b6750e2778e75e55687e1`
- 损坏 index：57,955 bytes，SHA-256 `DD09C7A0F031246F883908B68AF077496B1AC625956D864F9D830B2533574A6D`，内容全零。
- 重建方法：在隔离的 `GIT_INDEX_FILE` 中执行 `git read-tree HEAD`，先验证 `status/diff/diff --cached/fsck`，再替换正式 index。
- `git fsck --full --no-reflogs`：退出码 0；只有 dangling tree/commit，没有缺失或损坏对象。
- 全零 tracked 文件：11 个，恢复前版本全部保存在备份目录，恢复后逐项与 HEAD blob 哈希一致。
- `README.md`：15,607 bytes，NUL 数量 0，worktree blob 与 HEAD 均为 `6cb08d0172765a7b2a038492ed87a96a84b7350d`。
- 真实用户改动：`corecoder/mcp_bridge.py`，恢复流程没有覆盖；备份 SHA-256 为 `22286AC9B5FFBBB6234170ED9A047D64BD4DC1698958BE2392F9DE5E0369C4B9`。
- staged diff：空。

备份和完整清单位于 `.tmp/p0-recovery-20260830/`：

- `index.corrupt`：损坏索引原件；
- `index.rebuilt`：隔离重建并验证过的索引；
- `worktree-backup/`：恢复前的 tracked 文件与本轮规划文档；
- `RECOVERY_MANIFEST.md`：人类可读哈希清单；
- `recovery-manifest.json`：机器可读恢复与验收状态。

## 绿基线改动

1. `pyproject.toml`
   - `dev` 依赖增加 Ruff；
   - pytest 默认 `testpaths = ["tests"]`，注册 integration/experiment marker；
   - Ruff P0 基线限定为 `E9/F63/F7/F82`，先阻断语法、名称和高风险运行错误，避免 P0 混入 69 个历史风格重写。
2. `tests/test_core.py`
   - 配置测试不再读取开发者 `.env`；
   - session 测试全部使用 pytest 隔离临时目录，不再访问用户主目录。
3. `tests/test_tools.py`
   - 移除 `tempfile.mktemp()` 和共享 `Temp/sub/dir`；使用 `tmp_path`，修复并行运行时可复现的 Windows 清理竞态。
4. `.github/workflows/ci.yml`
   - 保留现有 Ubuntu/macOS/Windows 与 Python 3.10–3.13 matrix；
   - 默认运行 `pytest -q`，由项目配置约束收集范围；
   - 增加 Ruff，继续运行 compileall。Windows/Ubuntu 3.10 与 3.13 均包含在 matrix 中。

## 本地验收结果

环境：仓库内 `.venv`，Python 3.11.7，pytest 9.1.1，Ruff 0.16.5，pip 23.2.1；通过 `python -m pip install -e ".[dev]"` 建立。

| 门禁 | 结果 |
|---|---|
| `git status --short` | 通过 |
| `git diff` | 通过 |
| `git diff --cached --exit-code` | 通过，staged diff 为空 |
| `git diff --check` | 通过 |
| `python -m pytest -q` | 66 passed；默认只收集 `tests/` |
| `python -m pytest tests -q` | 66 passed |
| 连续默认 pytest | 2/2 通过；另有最终回归 1 次通过 |
| `python -m ruff check corecoder tests` | 通过 |
| `python -m compileall -q corecoder tests` | 通过 |

测试期间未调用外部 API。依赖安装需要访问 PyPI；完成安装后，测试本身为离线运行。

## P0 退出门槛

| 条件 | 状态 |
|---|---|
| Git 命令可正常读取索引 | 完成 |
| Branch/HEAD 保持一致 | 完成 |
| 差异可映射到备份或明确改动 | 完成 |
| README 无 NUL，恢复前版本独立备份 | 完成 |
| pytest 收集与运行 | 完成 |
| Ruff/compileall | 完成 |
| Windows 与 Ubuntu CI 各成功一次 | 待触发 |

因此当前状态是“P0 本地完成，远端 CI 待确认”。没有创建 commit，也没有 push。触发远端 CI 属于下一项外部状态变更，应在确认提交边界后执行。

## 恢复实施说明

计划原建议在新 clone/恢复工作树中实施并把原目录作为只读证据源。实际执行采用了“原位、先备份、逐项校验”的恢复方式：损坏 index、11 个清零文件和关键文档均先复制到独立恢复目录，再修改当前工作树。这样恢复了用户当前工作位置，但不等同于保留整个原目录的只读快照；完整的受影响文件级证据仍可从恢复目录复核。

## 下一步

1. 审查当前 P0 diff 与被保护的 `mcp_bridge.py` 用户改动，确定提交边界。
2. 创建提交并推送/开 PR，等待 Windows 与 Ubuntu matrix 通过，随后把 P0 标记为完全完成。
3. 仅在 P0 CI 闭环后进入 P1a，先提交失败的 Agent 合约测试，不提前移植上游 Implementation。
