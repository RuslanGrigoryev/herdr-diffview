"""git-status/diff snapshotting for the watched directory."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class NotAGitRepo(RuntimeError):
    pass


@dataclass
class FileChange:
    path: str
    status: str  # "M", "A", "D", "R", "??", etc. (porcelain code, trimmed)

    @property
    def marker(self) -> str:
        return {
            "M": "M",
            "A": "A",
            "D": "D",
            "R": "R",
            "C": "C",
            "??": "?",
            "U": "U",
        }.get(self.status, self.status[:1] or "?")


@dataclass
class RepoSnapshot:
    root: Path
    branch: str
    files: list[FileChange] = field(default_factory=list)

    def find(self, path: str) -> Optional[FileChange]:
        for f in self.files:
            if f.path == path:
                return f
        return None


def _run(args: list[str], cwd: Path, timeout: float = 5.0) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise NotAGitRepo(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def repo_root(path: Path) -> Path:
    out = _run(["rev-parse", "--show-toplevel"], cwd=path)
    return Path(out.strip())


def current_branch(root: Path) -> str:
    try:
        out = _run(["symbolic-ref", "--short", "-q", "HEAD"], cwd=root)
        name = out.strip()
        if name:
            return name
    except NotAGitRepo:
        pass
    try:
        out = _run(["rev-parse", "--short", "HEAD"], cwd=root)
        return f"detached@{out.strip()}"
    except NotAGitRepo:
        return "(no commits yet)"


def snapshot(path: Path) -> RepoSnapshot:
    root = repo_root(path)
    branch = current_branch(root)
    status_out = _run(["status", "--porcelain=v1", "--untracked-files=all"], cwd=root)
    files: list[FileChange] = []
    for line in status_out.splitlines():
        if not line:
            continue
        code = line[:2].strip()
        fname = line[3:]
        if " -> " in fname:  # renames: "old -> new"
            fname = fname.split(" -> ", 1)[1]
        files.append(FileChange(path=fname, status=code or "?"))
    files.sort(key=lambda f: f.path)
    return RepoSnapshot(root=root, branch=branch, files=files)


def diff_for_file(root: Path, change: FileChange) -> str:
    """Unified diff text for one file, handling untracked files specially
    (git diff shows nothing for them by default)."""
    if change.status == "??":
        try:
            content = (root / change.path).read_text(errors="replace")
        except OSError as exc:
            return f"(could not read {change.path}: {exc})"
        lines = content.splitlines()
        body = "\n".join(f"+{l}" for l in lines)
        header = (
            f"--- /dev/null\n+++ b/{change.path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n"
        )
        return header + body + ("\n" if body else "")
    try:
        out = _run(["diff", "HEAD", "--", change.path], cwd=root)
        if out.strip():
            return out
        # Fall back to diffing the index (covers newly-added-but-uncommitted
        # files where `diff HEAD` already works, and staged-only changes).
        out = _run(["diff", "--cached", "--", change.path], cwd=root)
        if out.strip():
            return out
        out = _run(["diff", "--", change.path], cwd=root)
        return out
    except NotAGitRepo as exc:
        return f"(diff failed for {change.path}: {exc})"


def cumulative_diff(root: Path) -> str:
    try:
        head_diff = _run(["diff", "HEAD"], cwd=root)
    except NotAGitRepo:
        head_diff = ""
    parts = [head_diff] if head_diff.strip() else []
    snap = snapshot(root)
    for f in snap.files:
        if f.status == "??":
            parts.append(diff_for_file(root, f))
    return "\n".join(p for p in parts if p.strip())
