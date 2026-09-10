"""CoreCoder - Minimal AI coding agent inspired by Claude Code's architecture."""

__version__ = "0.3.0"

from corecoder.agent import Agent
from corecoder.llm import LLM
from corecoder.config import Config
from corecoder.tools import ALL_TOOLS
from corecoder.ledger import RunLedger
from corecoder.memory import ProjectMemoryStore, create_memory_from_skill
from corecoder.loop_guard import ToolLoopGuard
from corecoder.runtime import ToolRuntime
from corecoder.workspace import WorkspaceExecution

__all__ = [
    "Agent",
    "LLM",
    "Config",
    "ALL_TOOLS",
    "RunLedger",
    "ProjectMemoryStore",
    "ToolLoopGuard",
    "ToolRuntime",
    "WorkspaceExecution",
    "create_memory_from_skill",
    "__version__",
]
