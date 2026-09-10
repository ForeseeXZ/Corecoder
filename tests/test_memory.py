import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from corecoder.agent import Agent
from corecoder.memory import ProjectMemoryStore, create_memory_from_skill, redact_memory_skill


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "memory-project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "CoreCoder Tests")
    (repo / "README.md").write_text("memory\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


class FakeLLM:
    model = "fake-memory-model"

    def __init__(self, content: str = "", error: Exception | None = None):
        self.content = content
        self.error = error
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append(messages)
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.content)


def test_memory_skill_is_summarized_and_persisted(tmp_path):
    repo = _repo(tmp_path)
    llm = FakeLLM(json.dumps({
        "title": "Run tests before finishing",
        "summary": "Always run the focused pytest suite.",
        "details": "Use .venv/Scripts/python.exe on Windows.",
        "use_when": "Before reporting a code change as complete.",
    }))

    result = create_memory_from_skill(
        llm,
        "Remember the Windows test command.",
        project_root=repo,
        memory_date="2026-09-03",
        home=tmp_path / "memory-home",
    )

    assert result.used_model_summary is True
    assert result.path.is_file()
    content = result.path.read_text(encoding="utf-8")
    assert "2026-09-03" in content
    assert "Run tests before finishing" in content
    assert len(llm.requests) == 1


def test_empty_model_response_falls_back_without_losing_the_skill(tmp_path):
    repo = _repo(tmp_path)
    result = create_memory_from_skill(
        FakeLLM(""),
        "Use the official Docker harness for final SWE-bench scoring.",
        project_root=repo,
        home=tmp_path / "memory-home",
    )

    assert result.used_model_summary is False
    assert result.fallback_reason == "ValueError"
    assert "official Docker harness" in result.path.read_text(encoding="utf-8")


def test_memory_skill_redacts_common_credentials(tmp_path):
    cleaned = redact_memory_skill(
        "api_key=sk-1234567890 token: abcdefghijkl Bearer secret-token-123 password=hunter2"
    )
    assert "sk-1234567890" not in cleaned
    assert "abcdefghijkl" not in cleaned
    assert "secret-token-123" not in cleaned
    assert "hunter2" not in cleaned
    assert cleaned.count("<redacted>") == 4


def test_startup_memory_is_bounded_and_injected_into_agent(tmp_path):
    repo = _repo(tmp_path)
    store = ProjectMemoryStore.for_project(repo, home=tmp_path / "memory-home")
    store.directory.mkdir(parents=True)
    store.memory_path.write_text(
        "# Memory\n" + "\n".join(f"line-{i}" for i in range(400)),
        encoding="utf-8",
    )
    loaded = store.load_startup(max_lines=200, max_bytes=25_000)

    agent = Agent(llm=FakeLLM(), tools=[], project_memory=loaded)

    assert len(loaded.splitlines()) == 200
    assert "line-198" in agent._full_messages()[0]["content"]
    assert "line-399" not in agent._full_messages()[0]["content"]


def test_demo_seed_covers_april_through_september_and_is_not_startup_memory(tmp_path):
    repo = _repo(tmp_path)
    store = ProjectMemoryStore.for_project(repo, home=tmp_path / "memory-home")

    assert store.seed_demo() == 6
    first = store.demo_path.read_text(encoding="utf-8")
    assert store.seed_demo() == 6
    assert store.demo_path.read_text(encoding="utf-8") == first
    for month in range(4, 10):
        assert f"2026-{month:02d}-" in first
    assert "Demonstration data only" in first
    assert store.load_startup() == ""


def test_worktrees_share_the_same_project_memory_directory(tmp_path):
    repo = _repo(tmp_path)
    linked = tmp_path / "linked-worktree"
    _git(repo, "worktree", "add", "--detach", str(linked), "HEAD")
    try:
        home = tmp_path / "memory-home"
        main_store = ProjectMemoryStore.for_project(repo, home=home)
        linked_store = ProjectMemoryStore.for_project(linked, home=home)
        assert main_store.project_id == linked_store.project_id
        assert main_store.memory_path == linked_store.memory_path
    finally:
        _git(repo, "worktree", "remove", "--force", str(linked))
