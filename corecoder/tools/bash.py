"""Shell command execution with safety checks.

Claude Code's BashTool is 1,143 lines. This is the distilled version:
- Output capture with truncation (head+tail preserved)
- Timeout support
- Dangerous command detection
- Working directory tracking (cd awareness)
"""

import os
import locale
import re
import subprocess
from pathlib import Path

from .base import Tool, ToolEffect, ToolExecutionResult, ToolStatus

# Backward-compatible module attribute used by older runners. New Agent paths
# bind a workspace to each BashTool instance instead of mutating this value.
_cwd: str | None = None

# patterns that could wreck the filesystem or leak secrets
_DANGEROUS_PATTERNS = [
    (r"\brm\s+(-\w*)?-r\w*\s+(/|~|\$HOME)", "recursive delete on home/root"),
    (r"\brm\s+(-\w*)?-rf\s", "force recursive delete"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*of=/dev/", "raw disk write"),
    (r">\s*/dev/sd[a-z]", "overwrite block device"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "chmod 777 on root"),
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?bash", "pipe curl to bash"),
    (r"\bwget\b.*\|\s*(sudo\s+)?bash", "pipe wget to bash"),
]


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a shell command. Returns stdout, stderr, and exit code. "
        "Use this for running tests, installing packages, git operations, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120)",
            },
        },
        "required": ["command"],
    }

    effect = ToolEffect.PROCESS

    def __init__(self, cwd: str | Path | None = None):
        self._workspace_cwd = Path(cwd).resolve() if cwd is not None else None

    def bind_workspace(self, root: str | Path) -> None:
        self._workspace_cwd = Path(root).resolve()

    def execute(self, command: str, timeout: int = 120) -> str:
        result = self.execute_structured(command=command, timeout=timeout)
        out = result.content
        if len(out) > 15_000:
            out = (
                out[:6000]
                + f"\n\n... truncated ({len(out)} chars total) ...\n\n"
                + out[-3000:]
            )
        return out

    def execute_structured(
        self, command: str, timeout: int | float = 120
    ) -> ToolExecutionResult:
        # safety check
        warning = _check_dangerous(command)
        if warning:
            content = (
                f"⚠ Blocked: {warning}\nCommand: {command}\n"
                "If intentional, modify the command to be more specific."
            )
            return ToolExecutionResult(
                status=ToolStatus.BLOCKED,
                content=content,
                error=warning,
            )

        # use tracked working directory
        cwd = str(self._workspace_cwd) if self._workspace_cwd else (_cwd or os.getcwd())

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                timeout=timeout,
                cwd=cwd,
            )

            # track cd commands so next command runs in the right place
            if proc.returncode == 0:
                self._update_cwd(command, cwd)
            out = _decode_output(proc.stdout)
            if proc.stderr:
                out += f"\n[stderr]\n{_decode_output(proc.stderr)}"
            if proc.returncode != 0:
                out += f"\n[exit code: {proc.returncode}]"
            status = (
                ToolStatus.SUCCESS
                if proc.returncode == 0
                else ToolStatus.NON_ZERO
            )
            return ToolExecutionResult(
                status=status,
                content=out.strip() or "(no output)",
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            message = f"Error: timed out after {timeout}s"
            return ToolExecutionResult(
                status=ToolStatus.TIMEOUT,
                content=message,
                error=message,
            )
        except Exception as e:
            message = f"Error running command: {e}"
            return ToolExecutionResult(
                status=ToolStatus.EXCEPTION,
                content=message,
                error=f"{type(e).__name__}: {e}",
            )

    def _update_cwd(self, command: str, current_cwd: str) -> None:
        """Track successful cd operations without process-global state."""
        active = current_cwd
        for part in command.split("&&"):
            part = part.strip()
            if not part.startswith("cd "):
                continue
            target = part[3:].strip().strip("'\"")
            if not target:
                continue
            new_dir = os.path.normpath(os.path.join(active, os.path.expanduser(target)))
            if os.path.isdir(new_dir):
                active = new_dir
                self._workspace_cwd = Path(new_dir).resolve()


def _check_dangerous(cmd: str) -> str | None:
    """Return a warning string if the command looks destructive, else None."""
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, cmd):
            return reason
    return None


def _decode_output(data: bytes | str | None) -> str:
    """Decode command output without trusting the platform default."""
    if not data:
        return ""
    if isinstance(data, str):
        return data

    encodings = ["utf-8", locale.getpreferredencoding(False), "gbk"]
    seen = set()
    for encoding in encodings:
        if not encoding or encoding in seen:
            continue
        seen.add(encoding)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass

    return data.decode("utf-8", errors="replace")


def _update_cwd(command: str, current_cwd: str):
    """Legacy process-global cwd tracking retained for external callers."""
    global _cwd
    # simple heuristic: look for cd at the end of a && chain or standalone
    parts = command.split("&&")
    for part in parts:
        part = part.strip()
        if part.startswith("cd "):
            target = part[3:].strip().strip("'\"")
            if target:
                new_dir = os.path.normpath(os.path.join(current_cwd, os.path.expanduser(target)))
                if os.path.isdir(new_dir):
                    _cwd = new_dir
