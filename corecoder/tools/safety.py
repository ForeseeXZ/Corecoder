"""Shared safety checks for tool access."""

from __future__ import annotations

import re
from pathlib import Path


_SENSITIVE_NAMES = {
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}

_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_SENSITIVE_DIRS = {"secrets", "credentials"}


def _looks_like_example(path: Path) -> bool:
    lowered = path.name.lower()
    return any(
        marker in lowered
        for marker in ("example", "sample", "template", ".dist")
    )


def sensitive_path_reason(path: str | Path) -> str | None:
    """Return why a path is sensitive, or None when it is safe to access."""
    p = Path(path).expanduser()
    name = p.name.lower()
    parts = {part.lower() for part in p.parts}

    if _looks_like_example(p):
        return None
    if name == ".env" or name.startswith(".env."):
        return "environment file may contain secrets"
    if name in _SENSITIVE_NAMES:
        return "credential file may contain secrets"
    if p.suffix.lower() in _SENSITIVE_SUFFIXES:
        return "private key or certificate file may contain secrets"
    if parts & _SENSITIVE_DIRS:
        return "path is inside a secrets/credentials directory"
    return None


def guard_path(path: str | Path, operation: str) -> str | None:
    """Return a user-facing block message if the path should not be accessed."""
    reason = sensitive_path_reason(path)
    if reason is None:
        return None
    return (
        f"Blocked: refusing to {operation} sensitive file '{path}' "
        f"({reason}). Use an example/template file or redact the secret first."
    )


_SENSITIVE_COMMAND_PATTERNS = [
    (re.compile(r"(?i)(^|[\\/=\s'\"\:])\.env(\.[\w.-]+)?($|[\\/=\s'\"\:;])"),
     "environment file may contain secrets"),
    (re.compile(r"(?i)\bid_(rsa|dsa|ecdsa|ed25519)\b"),
     "SSH private key may contain secrets"),
    (re.compile(r"(?i)\.(pem|key|p12|pfx)\b"),
     "private key or certificate file may contain secrets"),
    (re.compile(r"(?i)(^|[\\/])(secrets|credentials)([\\/.\s]|$)"),
     "secrets/credentials path may contain secrets"),
]


def sensitive_command_reason(command: str) -> str | None:
    """Detect shell commands that appear to read or write secret material."""
    for pattern, reason in _SENSITIVE_COMMAND_PATTERNS:
        if pattern.search(command):
            if any(marker in command.lower() for marker in ("example", "sample", "template")):
                return None
            return reason
    return None
