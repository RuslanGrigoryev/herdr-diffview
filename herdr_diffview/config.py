"""Small persisted-preferences file so herdr-diffview remembers your last
theme/follow/view-mode/panel-size choices across launches, instead of
resetting to the same defaults every time.

Deliberately not a config *language* (no TOML/YAML dependency) — a flat
JSON dict is enough for a handful of scalar preferences, and every value is
validated/clamped on load so a hand-edited or stale file can never crash
the app; it just falls back to that field's default.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_THEME_INDEX = 0
DEFAULT_FOLLOW = True
DEFAULT_VIEW_MODE = "list"
DEFAULT_FILE_PANEL_HEIGHT = 30  # percent
MIN_FILE_PANEL_HEIGHT = 15
MAX_FILE_PANEL_HEIGHT = 70


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "herdr-diffview" / "config.json"


@dataclass
class Config:
    theme_index: int = DEFAULT_THEME_INDEX
    follow: bool = DEFAULT_FOLLOW
    view_mode: str = DEFAULT_VIEW_MODE
    file_panel_height: int = DEFAULT_FILE_PANEL_HEIGHT

    @classmethod
    def load(cls, num_themes: int) -> "Config":
        path = config_path()
        try:
            raw: dict[str, Any] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()

        theme_index = raw.get("theme_index", DEFAULT_THEME_INDEX)
        if not isinstance(theme_index, int) or not (0 <= theme_index < max(num_themes, 1)):
            theme_index = DEFAULT_THEME_INDEX

        follow = raw.get("follow", DEFAULT_FOLLOW)
        if not isinstance(follow, bool):
            follow = DEFAULT_FOLLOW

        view_mode = raw.get("view_mode", DEFAULT_VIEW_MODE)
        if view_mode not in ("list", "tree"):
            view_mode = DEFAULT_VIEW_MODE

        height = raw.get("file_panel_height", DEFAULT_FILE_PANEL_HEIGHT)
        if not isinstance(height, int):
            height = DEFAULT_FILE_PANEL_HEIGHT
        height = max(MIN_FILE_PANEL_HEIGHT, min(MAX_FILE_PANEL_HEIGHT, height))

        return cls(
            theme_index=theme_index,
            follow=follow,
            view_mode=view_mode,
            file_panel_height=height,
        )

    def save(self) -> None:
        path = config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        except OSError:
            # Best-effort: a read-only home dir or similar shouldn't crash
            # the app, it just means preferences won't persist this run.
            pass
