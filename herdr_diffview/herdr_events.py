"""Minimal client for Herdr's local socket API — used only to subscribe to
`pane.agent_status_changed` events for the pane we're watching, so the status
badge updates the instant Herdr's own detection changes instead of us polling.

Protocol: newline-delimited JSON over a Unix domain socket.
https://herdr.dev/docs/socket-api/
"""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional


def default_socket_path() -> Path:
    session = os.environ.get("HERDR_SESSION")
    config_dir = Path(os.environ.get("HERDR_CONFIG_DIR", Path.home() / ".config" / "herdr"))
    if session:
        return config_dir / "sessions" / session / "herdr.sock"
    return config_dir / "herdr.sock"


def resolve_socket_path() -> Optional[Path]:
    explicit = os.environ.get("HERDR_SOCKET_PATH")
    if explicit:
        return Path(explicit)
    path = default_socket_path()
    return path if path.exists() else None


class AgentStatusSubscriber:
    """Runs a background thread that watches one pane's agent_status.

    Best-effort: if the socket isn't reachable or the protocol shape drifts,
    it calls `on_error` once and the caller should fall back to polling via
    the CLI (`herdr pane get`) instead of treating this as fatal.
    """

    def __init__(
        self,
        pane_id: str,
        on_status: Callable[[str], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self._pane_id = pane_id
        self._on_status = on_status
        self._on_error = on_error
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> bool:
        path = resolve_socket_path()
        if path is None:
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(str(path))
            sock.settimeout(None)
        except OSError as exc:
            if self._on_error:
                self._on_error(exc)
            return False
        self._sock = sock
        request = {
            "id": "sub_agent_status",
            "method": "events.subscribe",
            "params": {
                "subscriptions": [
                    {
                        "type": "pane.agent_status_changed",
                        "pane_id": self._pane_id,
                    }
                ]
            },
        }
        try:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        except OSError as exc:
            if self._on_error:
                self._on_error(exc)
            return False
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return True

    def _read_loop(self) -> None:
        assert self._sock is not None
        buf = b""
        try:
            while not self._stop.is_set():
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    self._handle_line(line)
        except OSError as exc:
            if not self._stop.is_set() and self._on_error:
                self._on_error(exc)

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            return
        event = msg.get("event") or msg.get("result")
        if not isinstance(event, dict):
            return
        if event.get("pane_id") != self._pane_id:
            return
        status = event.get("agent_status") or event.get("status")
        if status:
            self._on_status(status)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
