"""Content search with regex support."""

import re
from pathlib import Path
from .base import Tool

# skip these dirs to avoid noise
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build"}


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents with regex. "
        "Returns matching lines with file path and line number."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search (default: cwd)",
            },
            "include": {
                "type": "string",
                "description": "Only search files matching this glob (e.g. '*.py')",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, max_files: int = 5000, max_matches: int = 200):
        self.max_files = max_files
        self.max_matches = max_matches

    def execute(self, pattern: str, path: str = ".", include: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Invalid regex: {e}"

        base = Path(path).expanduser().resolve()
        if not base.exists():
            return f"Error: {path} not found"

        if base.is_file():
            files = [base]
            scan_incomplete = False
        else:
            files, scan_incomplete = self._walk(base, include)

        matches = []
        for fp in files:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{fp}:{lineno}: {line.rstrip()}")
                    if len(matches) >= self.max_matches:
                        matches.append(
                            f"... ({self.max_matches} match limit reached)"
                        )
                        return "\n".join(matches)

        result = "\n".join(matches) if matches else "No matches found."
        if scan_incomplete:
            result += (
                f"\n... (file scan incomplete: {self.max_files} file limit reached)"
            )
        return result

    def _walk(self, root: Path, include: str | None) -> tuple[list[Path], bool]:
        """Walk dir tree, skipping junk dirs."""
        results = []
        for item in root.rglob(include or "*"):
            # skip hidden/junk directories
            relative_dirs = item.relative_to(root).parts[:-1]
            if any(part in _SKIP_DIRS for part in relative_dirs):
                continue
            if item.is_file():
                results.append(item)
            if len(results) > self.max_files:
                return results[: self.max_files], True
        return results, False
