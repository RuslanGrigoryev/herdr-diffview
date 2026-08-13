"""git-status/diff snapshotting for the watched directory."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class NotAGitRepo(RuntimeError):
    pass


# Files at or above this size, or detected as binary, get a summary line
# instead of a rendered diff — a multi-MB lockfile or a binary asset dumped
# into the pane is noise, not something worth syntax-highlighting line by
# line.
MAX_DIFF_BYTES = 1_500_000


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


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


@dataclass
class DiffStat:
    files_changed: int
    additions: int
    deletions: int

    def render(self) -> str:
        if self.files_changed == 0:
            return ""
        return (
            f"{self.files_changed} file{'s' if self.files_changed != 1 else ''} "
            f"+{self.additions} -{self.deletions}"
        )


def diffstat_from_text(diff_text: str) -> DiffStat:
    """Count +/- lines and touched files directly from unified diff text.

    We compute this from the diff we already generated (works uniformly for
    tracked changes and untracked-file pseudo-diffs) instead of shelling out
    to `git diff --shortstat`, which does not cover untracked files.
    """
    additions = 0
    deletions = 0
    files = set()
    current_file: Optional[str] = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            if name.startswith("b/"):
                name = name[2:]
            if name != "/dev/null":
                current_file = name
                files.add(current_file)
            continue
        if line.startswith("+++") or line.startswith("---") or line.startswith("diff --git") or line.startswith("index "):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return DiffStat(files_changed=len(files), additions=additions, deletions=deletions)


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


def skip_reason(root: Path, change: FileChange) -> Optional[str]:
    """Return a human-readable reason to skip rendering this file's diff
    (binary content, or too large), or None if it should render normally.

    Deleted files have nothing on disk to sample — always let those through
    to git's own diff machinery, which handles them fine either way.
    """
    if change.status == "D":
        return None
    path = root / change.path
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > MAX_DIFF_BYTES:
        return f"binary or large file skipped ({_human_size(size)})"
    try:
        with open(path, "rb") as fh:
            sample = fh.read(8192)
    except OSError:
        return None
    if _looks_binary(sample):
        return f"binary file skipped ({_human_size(size)})"
    return None


def diff_for_file(root: Path, change: FileChange) -> str:
    """Unified diff text for one file, handling untracked files specially
    (git diff shows nothing for them by default)."""
    reason = skip_reason(root, change)
    if reason is not None:
        return f"({reason})"
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


# Common default-branch names, checked in this order against local and
# origin refs. Covers the two conventions in the wild without needing any
# config from the user.
_DEFAULT_BRANCH_CANDIDATES = ("main", "master")


def _ref_exists(root: Path, ref: str) -> bool:
    try:
        _run(["rev-parse", "--verify", "--quiet", ref], cwd=root)
        return True
    except NotAGitRepo:
        return False


def detect_base_branch(root: Path) -> Optional[str]:
    """Best-effort default-branch detection, no config required.

    Tries, in order: origin's reported HEAD (authoritative when available),
    then common local branch names, then the same names under origin/. Skips
    whichever branch is currently checked out — diffing a branch against
    itself is never useful. Returns None if nothing plausible is found (e.g.
    a fresh repo with only one commit and no remote).
    """
    current = current_branch(root)

    try:
        origin_head = _run(
            ["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], cwd=root
        ).strip()
    except NotAGitRepo:
        origin_head = ""
    if origin_head:
        short = origin_head.removeprefix("origin/")
        if short != current and _ref_exists(root, origin_head):
            return origin_head

    for name in _DEFAULT_BRANCH_CANDIDATES:
        if name != current and _ref_exists(root, name):
            return name
    for name in _DEFAULT_BRANCH_CANDIDATES:
        ref = f"origin/{name}"
        if name != current and _ref_exists(root, ref):
            return ref
    return None


def branch_diff(root: Path, base_ref: str) -> str:
    """Whole-branch diff: working tree + all commits on HEAD since it
    diverged from base_ref, i.e. what a PR against base_ref would show.
    Uses git's own `base...HEAD` merge-base syntax, then folds in
    uncommitted changes and untracked files the same way cumulative_diff
    does, so this is a strict superset of 'commits so far' + 'not yet
    committed'.
    """
    try:
        merge_base_diff = _run(["diff", f"{base_ref}...HEAD"], cwd=root)
    except NotAGitRepo as exc:
        return f"(could not diff against {base_ref}: {exc})"
    try:
        working_diff = _run(["diff", "HEAD"], cwd=root)
    except NotAGitRepo:
        working_diff = ""
    parts = [p for p in (merge_base_diff, working_diff) if p.strip()]
    snap = snapshot(root)
    for f in snap.files:
        if f.status == "??":
            parts.append(diff_for_file(root, f))
    combined = "\n".join(p for p in parts if p.strip())
    if len(combined) > MAX_DIFF_BYTES:
        combined = (
            combined[:MAX_DIFF_BYTES]
            + f"\n\n... (truncated, branch diff exceeds "
            f"{_human_size(MAX_DIFF_BYTES)} — view files individually instead)"
        )
    return combined


def cumulative_diff(root: Path) -> str:
    # Tracked binary files are already summarized by git itself ("Binary
    # files a/x and b/x differ") inside `git diff HEAD`, so only untracked
    # files need our own skip_reason() guard here.
    try:
        head_diff = _run(["diff", "HEAD"], cwd=root)
    except NotAGitRepo:
        head_diff = ""
    parts = [head_diff] if head_diff.strip() else []
    snap = snapshot(root)
    for f in snap.files:
        if f.status == "??":
            parts.append(diff_for_file(root, f))
    combined = "\n".join(p for p in parts if p.strip())
    if len(combined) > MAX_DIFF_BYTES:
        # Safety net for the one case skip_reason() can't cover per-file:
        # a single huge *tracked* text file dumped whole inside `git diff
        # HEAD`'s combined output (binary tracked files are already
        # summarized by git itself before this point).
        combined = (
            combined[:MAX_DIFF_BYTES]
            + f"\n\n... (truncated, cumulative diff exceeds "
            f"{_human_size(MAX_DIFF_BYTES)} — view files individually instead)"
        )
    return combined
