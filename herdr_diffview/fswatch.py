"""Debounced filesystem watcher: fires a single callback no more than once
per `debounce_seconds`, however many fs events land in that window."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, callback: Callable[[], None], debounce_seconds: float) -> None:
        self._callback = callback
        self._debounce_seconds = debounce_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        self._callback()

    def on_any_event(self, event) -> None:  # noqa: ANN001 - watchdog API
        if ".git" in Path(event.src_path).parts:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()


class DirWatcher:
    def __init__(
        self,
        path: Path,
        on_change: Callable[[], None],
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
