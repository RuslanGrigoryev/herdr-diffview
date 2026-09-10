"""Debounced filesystem watcher: fires a single callback no more than once
per `debounce_seconds`, however many fs events land in that window. The
callback receives the set of paths that changed during the debounce window,
so callers can e.g. auto-select whichever file the agent just touched.

Also watches for "the current commit moved" (a bare `git commit`, `git
checkout`, `git pull` fast-forwarding a local branch, etc.), which touches
only git-internal metadata files — none of them tracked working-tree files.
Blanket-ignoring all git-internal writes (needed to avoid reacting to git's
own noisy internals: loose objects, lockfiles, packing) would otherwise mean
the app never refreshes after a plain commit/checkout that has no
working-tree file changes alongside it — exactly the "diff still shows the
old content after I committed" symptom this exists to fix.

In a `git worktree` checkout, the metadata that changes on commit/checkout
does NOT live under `<worktree>/.git` at all — `.git` there is a one-line
pointer file, and the real HEAD/index/refs live under the main checkout's
`.git/worktrees/<name>/` (HEAD, index) and `.git/` (refs/, shared by every
worktree of the repo). `DirWatcher` therefore watches the working tree PLUS
whichever extra git directories `git_watch.git_dirs()` reports, so a commit
made in a worktree is caught even though it never touches a single path
under the worktree's own directory tree.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Names that indicate history actually moved (HEAD changed, a branch ref was
# updated, the index was rewritten by a commit) when found directly inside a
# git metadata directory — everything else in there (objects/, logs/,
# *.lock, config, hooks/, etc.) is still ignored as internal git noise the
# watcher shouldn't react to.
_GIT_REF_MARKERS = ("HEAD", "index")


def _is_git_ref_change(relative_parts: tuple[str, ...]) -> bool:
    """True for HEAD, index, or anything under refs/, given a path already
    made relative to a git metadata directory's root."""
    if not relative_parts:
        return False
    if relative_parts[0] == "refs":
        return True
    if relative_parts[0] in _GIT_REF_MARKERS:
        return True
    return False


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(
        self,
        callback: Callable[[set[Path]], None],
        debounce_seconds: float,
        git_dirs: list[Path],
    ) -> None:
        self._callback = callback
        self._debounce_seconds = debounce_seconds
        # Resolved once up front; event paths are compared against these
        # rather than re-resolving symlinks per event.
        self._git_dirs = git_dirs
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._pending: set[Path] = set()

    def _passes_filter(self, path: Path) -> bool:
        for git_dir in self._git_dirs:
            try:
                rel = path.relative_to(git_dir)
            except ValueError:
                continue
            return _is_git_ref_change(rel.parts)
        # Not under any known git metadata directory — an ordinary
        # working-tree file, always worth reacting to.
        return True

    def on_any_event(self, event) -> None:  # noqa: ANN001 - watchdog API
        if getattr(event, "is_directory", False):
            return
        # Many editors/tools write atomically: write a temp file, then rename
        # it over the real target. That fires a *moved* event whose real
        # filename is dest_path, not src_path — record both so the final
        # name is always in the changed set.
        paths = [Path(event.src_path)]
        dest = getattr(event, "dest_path", "")
        if dest:
            paths.append(Path(dest))
        paths = [p for p in paths if self._passes_filter(p)]
        if not paths:
            return
        with self._lock:
            self._pending.update(paths)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            changed = self._pending
            self._pending = set()
            self._timer = None
        self._callback(changed)


class DirWatcher:
    def __init__(
        self,
        path: Path,
        on_change: Callable[[set[Path]], None],
        debounce_seconds: float = 0.15,
        extra_git_dirs: list[Path] | None = None,
    ) -> None:
        """Watches `path` (the working tree) recursively, plus each of
        `extra_git_dirs` (git metadata directories outside the working tree
        — see module docstring re: worktrees). Every watched root is
        recursive so nested directories are covered too, but the ref-change
        filter only lets HEAD/index/refs/* through for anything that lands
        under one of the git dirs; ordinary working-tree files pass
        unfiltered.
        """
        self._observer = Observer()
        git_dirs = [d.resolve() for d in (extra_git_dirs or [])]
        handler = _DebouncedHandler(on_change, debounce_seconds, git_dirs)
        root = path.resolve()
        self._observer.schedule(handler, str(path), recursive=True)
        watched_roots = {root}
        for git_dir in git_dirs:
            # Skip a git dir that's already inside (or equal to) the
            # working-tree root, or a dir we already added a schedule for
            # (e.g. --absolute-git-dir and --git-common-dir happen to be the
            # same path in a plain non-worktree checkout) — watchdog doesn't
            # need, and shouldn't get, two overlapping recursive schedules
            # on the same tree.
            if git_dir in watched_roots or root == git_dir or root in git_dir.parents:
                continue
            self._observer.schedule(handler, str(git_dir), recursive=True)
            watched_roots.add(git_dir)

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=2)
