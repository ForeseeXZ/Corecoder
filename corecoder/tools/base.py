"""Base class for all tools."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ToolEffect(str, Enum):
    """The kind of workspace effect a tool may produce."""

    READ = "read"
    WRITE = "write"
    PROCESS = "process"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    BAD_ARGUMENTS = "bad_arguments"
    NON_ZERO = "non_zero"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    EXCEPTION = "exception"
    CANCELLED = "cancelled"
    TRUNCATED = "truncated"


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Result returned by a tool adapter before runtime persistence."""

    status: ToolStatus
    content: str
    exit_code: int | None = None
    error: str | None = None


class Tool(ABC):
    """Minimal tool interface. Subclass this to add new capabilities."""

    name: str
    description: str
    parameters: dict  # JSON Schema for the function args
    effect: ToolEffect = ToolEffect.READ

    def bind_workspace(self, root: str | Path) -> None:
        self._workspace_root = Path(root).resolve()

    def resolve_path(self, value: str | Path) -> Path:
        """Resolve paths against a bound workspace and reject escapes."""
        raw = Path(value).expanduser()
        root = getattr(self, "_workspace_root", None)
        candidate = (root / raw).resolve() if root and not raw.is_absolute() else raw.resolve()
        if root is not None:
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise PermissionError(f"path escapes workspace: {value}") from exc
        return candidate

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """Run the tool and return a text result."""
        ...

    def schema(self) -> dict:
        """OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
