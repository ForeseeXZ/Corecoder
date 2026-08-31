"""Stable workspace identity for one Repair Run."""

from __future__ import annotations

import subprocess
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    """Raised when a Repair Run workspace cannot be resolved."""


@dataclass(frozen=True, slots=True)
class UntrackedFileState:
    """Content identity for one untracked workspace file."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """A reproducible view of changes relative to one baseline commit."""

    baseline_commit: str
    tracked_patch: str
    untracked_files: tuple[UntrackedFileState, ...]
    snapshot_hash: str

    @property
    def tracked_patch_sha256(self) -> str:
        return hashlib.sha256(self.tracked_patch.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class WorkspaceScratch:
    """A temporary directory owned exclusively by one workspace operation."""

    path: Path
    _created: bool = False

    def __enter__(self) -> Path:
        try:
            self.path.mkdir()
        except FileExistsError as exc:
            raise WorkspaceError(
                f"Scratch path already exists: {self.path.name}"
            ) from exc
        self._created = True
        return self.path

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._created:
            shutil.rmtree(self.path)
            self._created = False


@dataclass(frozen=True, slots=True)
class WorkspaceExecution:
    """An absolute Git root and immutable baseline for one Repair Run."""

    root: Path
    baseline_commit: str

    @classmethod
    def resolve(
        cls,
        start_path: str | Path,
        baseline_commit: str | None = None,
    ) -> "WorkspaceExecution":
        start = Path(start_path).expanduser().resolve()
        if start.is_file():
            start = start.parent

        root_text = cls._git(start, "rev-parse", "--show-toplevel")
        root = Path(root_text).resolve()
        requested_baseline = baseline_commit or "HEAD"
        baseline = cls._git(
            root,
            "rev-parse",
            "--verify",
            f"{requested_baseline}^{{commit}}",
        )
        return cls(root=root, baseline_commit=baseline)

    def snapshot(self) -> WorkspaceSnapshot:
        tracked_patch = self._git_raw_text(
            self.root,
            "diff",
            "--binary",
            "--no-ext-diff",
            self.baseline_commit,
            "--",
        )
        raw_untracked = self._git_bytes(
            self.root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        raw_paths = [path for path in raw_untracked.split(b"\0") if path]
        untracked = tuple(
            self._untracked_state(os.fsdecode(path))
            for path in sorted(raw_paths)
        )

        digest = hashlib.sha256()
        digest.update(self.baseline_commit.encode("ascii"))
        digest.update(b"\0")
        digest.update(tracked_patch.encode("utf-8", errors="surrogateescape"))
        for item in untracked:
            digest.update(b"\0")
            digest.update(os.fsencode(item.path))
            digest.update(b"\0")
            digest.update(str(item.size).encode("ascii"))
            digest.update(b"\0")
            digest.update(item.sha256.encode("ascii"))

        return WorkspaceSnapshot(
            baseline_commit=self.baseline_commit,
            tracked_patch=tracked_patch,
            untracked_files=untracked,
            snapshot_hash=digest.hexdigest(),
        )

    def scratch(self, relative_path: str | Path) -> WorkspaceScratch:
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
        ):
            raise WorkspaceError(f"Invalid scratch path: {relative_path}")
        path = self.root / relative
        try:
            path.parent.resolve().relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(
                f"Scratch path escapes workspace: {relative_path}"
            ) from exc
        return WorkspaceScratch(path=path)

    def _untracked_state(self, relative_path: str) -> UntrackedFileState:
        candidate = self.root / relative_path
        if candidate.is_symlink():
            data = os.fsencode(os.readlink(candidate))
        else:
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self.root)
            except ValueError as exc:
                raise WorkspaceError(
                    f"Untracked path escapes workspace: {relative_path}"
                ) from exc
            data = resolved.read_bytes()
        return UntrackedFileState(
            path=relative_path.replace("\\", "/"),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        completed = WorkspaceExecution._git_process(cwd, *args)
        return completed.stdout.decode("utf-8", errors="surrogateescape").strip()

    @staticmethod
    def _git_raw_text(cwd: Path, *args: str) -> str:
        completed = WorkspaceExecution._git_process(cwd, *args)
        return completed.stdout.decode("utf-8", errors="surrogateescape")

    @staticmethod
    def _git_bytes(cwd: Path, *args: str) -> bytes:
        return WorkspaceExecution._git_process(cwd, *args).stdout

    @staticmethod
    def _git_process(cwd: Path, *args: str) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = os.fsdecode(completed.stderr.strip() or completed.stdout.strip())
            raise WorkspaceError(detail or "Git workspace resolution failed")
        return completed
