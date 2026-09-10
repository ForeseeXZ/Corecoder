"""Project-scoped, durable memory generated from manually supplied skills.

The store deliberately separates real memory (``MEMORY.md``) from seeded demo
content (``DEMO_MEMORY.md``).  Real memory is loaded into new CLI sessions;
demo content exists only to make the feature easy to inspect and present.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


MEMORY_HOME = Path.home() / ".corecoder" / "projects"
MAX_STARTUP_LINES = 200
MAX_STARTUP_BYTES = 25_000

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_LIKE_RE = re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)


MEMORY_SYSTEM_PROMPT = """You maintain concise project memory for a coding agent.
Convert the user's manually supplied memory skill into a JSON object with exactly
these string fields: title, summary, details, use_when. Preserve concrete commands,
decisions, constraints, and verified numbers. Do not invent facts. Do not include
credentials. Keep each field concise and return JSON only, without a code fence."""


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    path: Path
    project_id: str
    entry_id: str
    used_model_summary: bool
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MemorySummary:
    title: str
    summary: str
    details: str
    use_when: str


def _run_git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _safe_name(value: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", value).strip(".-_")
    return cleaned[:60] or "project"


def _project_identity(start_path: str | Path) -> tuple[str, Path]:
    start = Path(start_path).expanduser().resolve()
    if start.is_file():
        start = start.parent

    top_text = _run_git(start, "rev-parse", "--show-toplevel")
    top = Path(top_text).resolve() if top_text else start
    common_text = _run_git(top, "rev-parse", "--git-common-dir")
    if common_text:
        common = Path(common_text)
        if not common.is_absolute():
            common = (top / common).resolve()
        else:
            common = common.resolve()
        identity_source = common
        project_name = common.parent.name if common.name == ".git" else top.name
    else:
        identity_source = top
        project_name = top.name

    digest = hashlib.sha256(str(identity_source).casefold().encode("utf-8")).hexdigest()[:12]
    return f"{_safe_name(project_name)}-{digest}", top


def redact_memory_skill(text: str) -> str:
    """Remove common credential shapes before sending or persisting a skill."""
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
    redacted = _BEARER_RE.sub("Bearer <redacted>", redacted)
    return _KEY_LIKE_RE.sub("<redacted>", redacted)


class ProjectMemoryStore:
    """A memory directory shared by worktrees from the same Git repository."""

    def __init__(self, project_id: str, project_root: Path, home: Path = MEMORY_HOME):
        self.project_id = project_id
        self.project_root = project_root
        self.directory = Path(home).expanduser().resolve() / project_id / "memory"
        self.memory_path = self.directory / "MEMORY.md"
        self.demo_path = self.directory / "DEMO_MEMORY.md"

    @classmethod
    def for_project(
        cls,
        start_path: str | Path,
        *,
        home: str | Path | None = None,
    ) -> "ProjectMemoryStore":
        project_id, root = _project_identity(start_path)
        return cls(project_id, root, Path(home) if home is not None else MEMORY_HOME)

    def load_startup(
        self,
        *,
        max_lines: int = MAX_STARTUP_LINES,
        max_bytes: int = MAX_STARTUP_BYTES,
    ) -> str:
        """Load a bounded prefix for injection into a new Agent session."""
        if not self.memory_path.exists():
            return ""
        try:
            lines = self.memory_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return ""

        kept: list[str] = []
        size = 0
        for line in lines[:max_lines]:
            encoded_size = len((line + "\n").encode("utf-8"))
            if size + encoded_size > max_bytes:
                break
            kept.append(line)
            size += encoded_size
        return "\n".join(kept).strip()

    def append(self, summary: MemorySummary, *, memory_date: str | None = None) -> str:
        stamp = _validate_date(memory_date)
        body = _render_entry(summary, stamp)
        entry_id = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        marker = f"<!-- memory-id: {entry_id} -->"

        existing = self._read_or_header(self.memory_path, demo=False)
        if marker not in existing:
            self._atomic_write(self.memory_path, existing.rstrip() + "\n\n" + marker + "\n" + body + "\n")
        return entry_id

    def seed_demo(self) -> int:
        """Write deterministic April-September 2026 examples, idempotently."""
        sections = []
        for item in DEMO_MEMORIES:
            summary = MemorySummary(
                title=item["title"],
                summary=item["summary"],
                details=item["details"],
                use_when=item["use_when"],
            )
            sections.append(_render_entry(summary, item["date"]))
        content = self._demo_header() + "\n\n" + "\n\n".join(sections) + "\n"
        self._atomic_write(self.demo_path, content)
        return len(sections)

    def show(self, *, include_demo: bool = False) -> str:
        path = self.demo_path if include_demo else self.memory_path
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""

    def _read_or_header(self, path: Path, *, demo: bool) -> str:
        try:
            current = path.read_text(encoding="utf-8")
            if current.strip():
                return current
        except (OSError, UnicodeError):
            pass
        return self._demo_header() if demo else self._memory_header()

    def _atomic_write(self, path: Path, content: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8", newline="\n")
        temp.replace(path)

    def _memory_header(self) -> str:
        return (
            "# CoreCoder Project Memory\n\n"
            f"> Project: `{self.project_id}`. Generated from manually supplied memory skills. "
            "Loaded into new CoreCoder sessions as bounded project context."
        )

    def _demo_header(self) -> str:
        return (
            "# CoreCoder Memory Demo\n\n"
            "> Demonstration data only; not an audit log. These examples are never loaded "
            "into the Agent's startup context."
        )


def create_memory_from_skill(
    llm: Any,
    memory_skill: str,
    *,
    project_root: str | Path = ".",
    memory_date: str | None = None,
    home: str | Path | None = None,
) -> MemoryWriteResult:
    """Summarize a manual memory skill with the model and persist it."""
    cleaned = redact_memory_skill(memory_skill).strip()
    if not cleaned:
        raise ValueError("memory skill is empty")

    store = ProjectMemoryStore.for_project(project_root, home=home)
    summary: MemorySummary
    used_model = False
    fallback_reason = None
    try:
        response = llm.chat(
            [
                {"role": "system", "content": MEMORY_SYSTEM_PROMPT},
                {"role": "user", "content": cleaned},
            ]
        )
        content = (getattr(response, "content", "") or "").strip()
        summary = _parse_model_summary(content)
        used_model = True
    except Exception as exc:  # the memory command must remain usable offline
        fallback_reason = type(exc).__name__
        summary = _fallback_summary(cleaned)

    entry_id = store.append(summary, memory_date=memory_date)
    return MemoryWriteResult(
        path=store.memory_path,
        project_id=store.project_id,
        entry_id=entry_id,
        used_model_summary=used_model,
        fallback_reason=fallback_reason,
    )


def _validate_date(value: str | None) -> str:
    if value is None:
        return date.today().isoformat()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("memory date must use YYYY-MM-DD") from exc


def _parse_model_summary(content: str) -> MemorySummary:
    if not content:
        raise ValueError("empty model memory summary")
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model memory summary is not JSON")
    payload = json.loads(content[start : end + 1])
    required = ("title", "summary", "details", "use_when")
    if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) for key in required):
        raise ValueError("model memory summary has an invalid shape")
    values = {key: _clean_field(payload[key], 1200 if key == "details" else 500) for key in required}
    if not values["title"] or not values["summary"]:
        raise ValueError("model memory summary is incomplete")
    return MemorySummary(**values)


def _clean_field(value: str, max_chars: int) -> str:
    value = redact_memory_skill(value).replace("\x00", "").strip()
    value = re.sub(r"^#{1,6}\s*", "", value)
    return value[:max_chars].rstrip()


def _fallback_summary(skill: str) -> MemorySummary:
    compact = " ".join(skill.split())
    title = compact.split("。", 1)[0].split(".", 1)[0][:60] or "Manual project memory"
    return MemorySummary(
        title=title,
        summary=compact[:500],
        details="Model summarization was unavailable; the sanitized manual memory skill was preserved.",
        use_when="Use when working on the same project or repeating the described workflow.",
    )


def _render_entry(summary: MemorySummary, stamp: str) -> str:
    return (
        f"## {stamp} — {summary.title}\n\n"
        f"- **结论**：{summary.summary}\n"
        f"- **细节**：{summary.details}\n"
        f"- **适用场景**：{summary.use_when}"
    )


DEMO_MEMORIES = (
    {
        "date": "2026-04-18",
        "title": "统一 OpenAI-compatible 模型入口",
        "summary": "CoreCoder 通过 model、base_url 和 API key 切换兼容供应商。",
        "details": "模型适配保持在 LLM 边界，Agent Loop 与工具实现不感知具体供应商。",
        "use_when": "接入或切换新的 OpenAI-compatible 模型服务时。",
    },
    {
        "date": "2026-05-12",
        "title": "HumanEval 使用独立判题目录",
        "summary": "Agent 只看到函数签名和 Docstring，官方测试在独立 judge 目录注入。",
        "details": "每题生成 solution.py，并记录 pass@1、耗时、Token、成本与工具调用。",
        "use_when": "解释函数级代码生成评测和隐藏测试隔离时。",
    },
    {
        "date": "2026-06-10",
        "title": "双 Benchmark 基线完成",
        "summary": "HumanEval 为 161/164；SWE-bench Verified Mini 的 flash/pro 基线为 26/50 和 29/50。",
        "details": "HumanEval 衡量函数生成；SWE-bench 衡量真实仓库定位、修改和隐藏测试通过情况。",
        "use_when": "比较模型基础编码能力与仓库级软件工程能力时。",
    },
    {
        "date": "2026-07-16",
        "title": "编排增量必须通过消融验证",
        "summary": "Planner、Reviewer、Compression 和 MCP 默认关闭，分别作为 Experiment Arm 开启。",
        "details": "小任务中额外编排可能只增加固定成本，因此不把角色数量当作效果保证。",
        "use_when": "设计 Agent A/B 实验或解释多 Agent 的适用边界时。",
    },
    {
        "date": "2026-08-31",
        "title": "Repair Run 可靠性边界完成",
        "summary": "WorkspaceExecution、ToolRuntime 与 Run Ledger 组成可重建的执行闭环。",
        "details": "固定 Git baseline，记录结构化 Tool Observation、Workspace Snapshot 和 append-only 事件。",
        "use_when": "排查工具异常、并发顺序或评测结果无法归因时。",
    },
    {
        "date": "2026-09-03",
        "title": "Qwen3.5-4B Provider 兼容性记录",
        "summary": "Qwen 默认 thinking 响应需要显式处理 reasoning content 与最终可见文本。",
        "details": "评测必须固定 thinking、max_tokens、temperature 和模型版本，不能把空文本直接当作无推理。",
        "use_when": "通过兼容 API 运行 Qwen3.5-4B 工具调用评测时。",
    },
)


__all__ = [
    "DEMO_MEMORIES",
    "MEMORY_HOME",
    "MemorySummary",
    "MemoryWriteResult",
    "ProjectMemoryStore",
    "create_memory_from_skill",
    "redact_memory_skill",
]
