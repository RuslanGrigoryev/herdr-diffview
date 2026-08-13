"""Debounced filesystem watcher: fires a single callback no more than once
per `debounce_seconds`, however many fs events land in that window. The
callback receives the set of paths that changed during the debounce window,
so callers can e.g. auto-select whichever file the agent just touched."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(
        self,
        callback: Callable[[set[Path]], None],
        debounce_seconds: float,
    ) -> None:
        self._callback = callback
        self._debounce_seconds = debounce_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._pending: set[Path] = set()

    def _fire(self) -> None:
        with self._lock:
            changed = self._pending
            self._pending = set()
            self._timer = None
        self._callback(changed)

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
        paths = [p for p in paths if ".git" not in p.parts]
        if not paths:
            return
        with self._lock:
            self._pending.update(paths)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()


class DirWatcher:
    def __init__(
        self,
        path: Path,
        on_change: Callable[[set[Path]], None],
        debounce_seconds: float = 0.15,
    ) -> None:
        self._observer = Observer()
        handler = _DebouncedHandler(on_change, debounce_seconds)
        self._observer.schedule(handler, str(path), recursive=True)

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=2)
