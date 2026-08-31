"""Integration contracts for a stable Repair Run workspace."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from corecoder.workspace import WorkspaceError, WorkspaceExecution
from corecoder.review import run_review_loop
from corecoder import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.runtime import ToolStatus
from corecoder.tools import WriteFileTool
from corecoder.tools.bash import BashTool
from tests.fakes import ScriptedLLM


def _git(repo, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _committed_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "CoreCoder Tests")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    return repo


def test_workspace_resolves_the_same_root_and_baseline_from_a_nested_path(tmp_path):
    repo = _committed_repo(tmp_path)
    nested = repo / "src" / "package"
    nested.mkdir(parents=True)

    workspace = WorkspaceExecution.resolve(nested)

    assert workspace.root == repo.resolve()
    assert workspace.baseline_commit == _git(repo, "rev-parse", "HEAD")


def test_workspace_snapshot_captures_tracked_and_untracked_changes(tmp_path):
    repo = _committed_repo(tmp_path)
    workspace = WorkspaceExecution.resolve(repo)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "notes.txt").write_text("new artifact\n", encoding="utf-8")

    snapshot = workspace.snapshot()

    assert "README.md" in snapshot.tracked_patch
    assert "+changed" in snapshot.tracked_patch
    assert [item.path for item in snapshot.untracked_files] == ["notes.txt"]
    assert snapshot.untracked_files[0].size == len((repo / "notes.txt").read_bytes())
    assert len(snapshot.untracked_files[0].sha256) == 64
    assert len(snapshot.snapshot_hash) == 64


def test_workspace_snapshot_does_not_depend_on_the_process_cwd(tmp_path, monkeypatch):
    repo = _committed_repo(tmp_path)
    workspace = WorkspaceExecution.resolve(repo / "README.md")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    expected = workspace.snapshot()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.chdir(outside)

    assert workspace.snapshot() == expected


def test_workspace_resolution_classifies_a_non_git_directory(tmp_path):
    with pytest.raises(WorkspaceError, match="not a git repository"):
        WorkspaceExecution.resolve(tmp_path)


def test_workspace_rejects_an_unknown_baseline_commit(tmp_path):
    repo = _committed_repo(tmp_path)

    with pytest.raises(WorkspaceError):
        WorkspaceExecution.resolve(repo, baseline_commit="missing-commit")


def test_workspace_refuses_to_claim_a_preexisting_scratch_directory(tmp_path):
    repo = _committed_repo(tmp_path)
    scratch = repo / ".cc_verify"
    scratch.mkdir()
    user_file = scratch / "user-data.txt"
    user_file.write_text("preserve me", encoding="utf-8")
    workspace = WorkspaceExecution.resolve(repo)

    with pytest.raises(WorkspaceError, match="already exists"):
        with workspace.scratch(".cc_verify"):
            pass

    assert user_file.read_text(encoding="utf-8") == "preserve me"


def test_workspace_cleans_only_the_scratch_directory_it_created(tmp_path):
    repo = _committed_repo(tmp_path)
    executor_file = repo / "executor-output.txt"
    executor_file.write_text("keep", encoding="utf-8")
    workspace = WorkspaceExecution.resolve(repo)

    with workspace.scratch(".cc_verify") as scratch:
        (scratch / "verify.sh").write_text("exit 0\n", encoding="utf-8")
        assert scratch.is_dir()

    assert not (repo / ".cc_verify").exists()
    assert executor_file.read_text(encoding="utf-8") == "keep"


def test_review_loop_refuses_to_delete_preexisting_user_scratch(tmp_path):
    repo = _committed_repo(tmp_path)
    scratch = repo / ".cc_verify"
    scratch.mkdir()
    user_file = scratch / "user-data.txt"
    user_file.write_text("preserve", encoding="utf-8")
    agent = SimpleNamespace(context=SimpleNamespace(max_tokens=1000))
    llm = SimpleNamespace(total_prompt_tokens=0, total_completion_tokens=0)

    with pytest.raises(WorkspaceError, match="already exists"):
        run_review_loop(
            agent=agent,
            llm=llm,
            repo_dir=repo,
            repo="example/project",
            problem_statement="test",
            max_rounds=0,
            log=lambda message: None,
        )

    assert user_file.read_text(encoding="utf-8") == "preserve"


def test_agent_file_tools_stay_bound_to_workspace_when_process_cwd_changes(
    tmp_path, monkeypatch
):
    repo = _committed_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = WorkspaceExecution.resolve(repo)
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={"file_path": "中文 记录.txt", "content": "ok\n"},
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    monkeypatch.chdir(outside)
    agent = Agent(llm=llm, tools=[WriteFileTool()], workspace=workspace)

    agent.chat("write inside the Repair Run workspace")

    assert (repo / "中文 记录.txt").read_text(encoding="utf-8") == "ok\n"
    assert not (outside / "中文 记录.txt").exists()


def test_agent_classifies_a_file_write_outside_the_workspace_as_blocked(tmp_path):
    repo = _committed_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    workspace = WorkspaceExecution.resolve(repo)
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="write-outside",
                        name="write_file",
                        arguments={"file_path": str(outside), "content": "no"},
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = Agent(llm=llm, tools=[WriteFileTool()], workspace=workspace)

    agent.chat("try to write outside")

    assert agent.last_tool_observations[0].status is ToolStatus.BLOCKED
    assert not outside.exists()


def test_bash_keeps_a_workspace_local_cwd_across_chained_cd_commands(tmp_path):
    repo = _committed_repo(tmp_path)
    deepest = repo / "a" / "b"
    deepest.mkdir(parents=True)
    bash = BashTool(repo)

    first = bash.execute(command="cd a && cd b")
    second = bash.execute(
        command=f'"{sys.executable}" -c "import os;print(os.getcwd())"'
    )

    assert "exit code" not in first
    assert str(deepest.resolve()).lower() in second.lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")
def test_linux_snapshot_captures_an_executable_bit_change(tmp_path):
    repo = _committed_repo(tmp_path)
    script = repo / "verify.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    _git(repo, "add", "verify.sh")
    _git(repo, "commit", "--quiet", "-m", "script")
    workspace = WorkspaceExecution.resolve(repo)

    script.chmod(0o755)
    snapshot = workspace.snapshot()

    assert "old mode 100644" in snapshot.tracked_patch
    assert "new mode 100755" in snapshot.tracked_patch


@pytest.mark.skipif(sys.platform != "linux", reason="Linux byte filenames")
def test_linux_snapshot_preserves_a_non_utf8_git_filename(tmp_path):
    repo = _committed_repo(tmp_path)
    raw_name = b"non-utf8-\xff.txt"
    raw_path = os.fsencode(repo) + b"/" + raw_name
    descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.write(descriptor, b"content\n")
    finally:
        os.close(descriptor)

    snapshot = WorkspaceExecution.resolve(repo).snapshot()

    assert os.fsencode(snapshot.untracked_files[0].path) == raw_name


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink behavior")
def test_linux_snapshot_hashes_an_untracked_symlink_without_following_it(tmp_path):
    repo = _committed_repo(tmp_path)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be read", encoding="utf-8")
    link = repo / "external-link"
    link.symlink_to(outside)

    snapshot = WorkspaceExecution.resolve(repo).snapshot()

    state = snapshot.untracked_files[0]
    assert state.path == "external-link"
    assert state.size == len(os.fsencode(os.readlink(link)))
