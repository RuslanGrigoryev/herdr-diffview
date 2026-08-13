"""Thin wrapper around the `herdr` CLI.

We deliberately go through the CLI (documented as a stable, versioned
surface — https://herdr.dev/docs/cli-reference/) instead of speaking the raw
socket protocol ourselves. It is slower per-call but far more robust across
Herdr versions, and every call we need (`pane list`, `pane get`, event
subscription is the one exception — see `herdr_events.py`) has a `--json`
or JSON-by-default mode.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional


class HerdrError(RuntimeError):
    pass


class HerdrNotAvailable(HerdrError):
    """herdr binary missing, or we're not running inside a Herdr pane."""


@dataclass
class PaneInfo:
    pane_id: str
    workspace_id: str
    tab_id: str
    cwd: Optional[str]
    foreground_cwd: Optional[str]
    agent_status: Optional[str]
    focused: bool
    revision: int
    raw: dict

    @property
    def effective_cwd(self) -> Optional[str]:
        return self.foreground_cwd or self.cwd

    @property
    def has_agent(self) -> bool:
        return self.agent_status is not None


def herdr_binary() -> str:
    path = shutil.which("herdr")
    if not path:
        raise HerdrNotAvailable(
            "The 'herdr' binary was not found on PATH. Install it from "
            "https://herdr.dev, or pass --path to skip Herdr auto-detection."
        )
    return path


def in_herdr_pane() -> bool:
    return os.environ.get("HERDR_ENV") == "1"


def _run_json(args: list[str]) -> dict:
    binary = herdr_binary()
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise HerdrError(f"herdr {' '.join(args)} timed out") from exc
    if proc.returncode != 0:
        raise HerdrError(
            f"herdr {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    text = proc.stdout.strip()
    if not text:
        raise HerdrError(f"herdr {' '.join(args)} produced no output")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HerdrError(
            f"herdr {' '.join(args)} did not return JSON: {text[:200]!r}"
        ) from exc


def _pane_from_dict(d: dict) -> PaneInfo:
    return PaneInfo(
        pane_id=d.get("pane_id", ""),
        workspace_id=d.get("workspace_id", ""),
        tab_id=d.get("tab_id", ""),
        cwd=d.get("cwd"),
        foreground_cwd=d.get("foreground_cwd"),
        agent_status=d.get("agent_status"),
        focused=bool(d.get("focused", False)),
        revision=int(d.get("revision", 0)),
        raw=d,
    )


def _extract_panes(payload: dict) -> list[dict]:
    """`herdr pane list` wraps results differently across CLI versions —
    handle the common shapes defensively rather than pinning to one."""
    if isinstance(payload, list):
        return payload
    for key in ("panes", "result", "items"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            inner = _extract_panes(val)
            if inner:
                return inner
    return []


def list_panes(workspace_id: Optional[str] = None) -> list[PaneInfo]:
    args = ["pane", "list"]
    if workspace_id:
        args += ["--workspace", workspace_id]
    payload = _run_json(args)
    return [_pane_from_dict(d) for d in _extract_panes(payload)]


def get_pane(pane_id: str) -> PaneInfo:
    payload = _run_json(["pane", "get", pane_id])
    for key in ("pane", "result"):
        if key in payload and isinstance(payload[key], dict):
            return _pane_from_dict(payload[key])
    return _pane_from_dict(payload)


def current_workspace_id() -> Optional[str]:
    return os.environ.get("HERDR_WORKSPACE_ID")


def current_pane_id() -> Optional[str]:
    return os.environ.get("HERDR_PANE_ID")


def find_agent_pane(workspace_id: Optional[str] = None) -> Optional[PaneInfo]:
    """Best-effort pick of "the agent pane to watch" in the current workspace.

    Strategy: list panes in the workspace, keep ones that (a) aren't us and
    (b) have a detected agent_status, then prefer the focused one, falling
    back to the highest `revision` (Herdr's own recency counter) as a proxy
    for "most recently active".
    """
    ws = workspace_id or current_workspace_id()
    me = current_pane_id()
    panes = list_panes(ws)
    candidates = [p for p in panes if p.has_agent and p.pane_id != me]
    if not candidates:
        return None
    focused = [p for p in candidates if p.focused]
    if focused:
        return focused[0]
    return max(candidates, key=lambda p: p.revision)
