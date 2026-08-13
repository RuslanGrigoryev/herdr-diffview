from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Footer, ListItem, ListView, Static

from . import git_watch, herdr_client
from .diff_render import render_diff
from .fswatch import DirWatcher
from .herdr_events import AgentStatusSubscriber

STATUS_STYLE = {
    "working": "bold yellow",
    "idle": "dim",
    "blocked": "bold red",
    "done": "bold green",
    "unknown": "dim",
}


class HeaderBar(Static):
    repo_label: reactive[str] = reactive("")
    branch_label: reactive[str] = reactive("")
    status_label: reactive[str] = reactive("")
    status_style: reactive[str] = reactive("dim")
    stat_label: reactive[str] = reactive("")
    wrap_label: reactive[str] = reactive("")
    follow_label: reactive[str] = reactive("")

    def render(self) -> Text:
        t = Text()
        t.append(" herdr-diffview ", style="bold reverse")
        t.append("  ")
        t.append(self.repo_label or "(no repo)", style="bold")
        if self.branch_label:
            t.append("  ")
            t.append(self.branch_label, style="cyan")
        if self.stat_label:
            t.append("  ")
            t.append(self.stat_label, style="magenta")
        if self.status_label:
            t.append("   agent: ", style="dim")
            t.append(self.status_label, style=self.status_style)
        if self.wrap_label:
            t.append("   ", style="dim")
            t.append(self.wrap_label, style="dim")
        if self.follow_label:
            t.append("   ", style="dim")
            t.append(self.follow_label, style="green" if "on" in self.follow_label else "dim")
        return t


class FilePane(ListView):
    pass


class DiffPane(Static):
    def show_diff(
        self, text: str, hint_filename: str | None = None, wrap: bool = False
    ) -> None:
        self.update(render_diff(text, hint_filename, wrap=wrap))


class BannerPane(Static):
    """Shown when watching has ended (dir gone / pane closed)."""


class HerdrDiffApp(App):
    CSS = """
    Screen { layout: vertical; }
    HeaderBar { height: 1; background: $panel; dock: top; }
    BannerPane { height: 3; background: $error 20%; color: $error; padding: 1; dock: top; }
    #body { height: 1fr; layout: vertical; }
    FilePane {
        height: 30%;
        min-height: 5;
        max-height: 15;
        border-bottom: solid $accent;
    }
    DiffPane { height: 1fr; padding: 0 1; overflow-y: auto; overflow-x: auto; }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("a", "toggle_cumulative", "All-files diff"),
        Binding("w", "toggle_wrap", "Wrap"),
        Binding("f", "toggle_follow", "Follow latest"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, target_path: Path, pane_id: Optional[str]) -> None:
        super().__init__()
        self._target_path = target_path
        self._pane_id = pane_id
        self._snapshot: Optional[git_watch.RepoSnapshot] = None
        self._selected_index = 0
        self._cumulative = False
        self._wrap = False
        self._follow = True
        self._programmatic_select = False
        self._watcher: Optional[DirWatcher] = None
        self._subscriber: Optional[AgentStatusSubscriber] = None
        self._ended = False

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        yield BannerPane(id="banner")
        with Vertical(id="body"):
            yield FilePane(id="files")
            yield DiffPane(id="diff")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#banner", BannerPane).display = False
        self._update_follow_label()
        self._reload()
        self._watcher = DirWatcher(self._target_path, self._on_fs_change)
        self._watcher.start()
        if self._pane_id and herdr_client.in_herdr_pane():
            self._subscriber = AgentStatusSubscriber(
                self._pane_id, self._on_agent_status, self._on_subscriber_error
            )
            self._subscriber.start()
        self.set_interval(2.0, self._poll_pane_health)

    def on_unmount(self) -> None:
        if self._watcher:
            self._watcher.stop()
        if self._subscriber:
            self._subscriber.stop()

    # -- data refresh -----------------------------------------------------

    def _on_fs_change(self, changed_paths: set[Path]) -> None:
        self.call_from_thread(self._reload, changed_paths)

    def _reload(self, changed_paths: Optional[set[Path]] = None) -> None:
        if self._ended:
            return
        try:
            snap = git_watch.snapshot(self._target_path)
        except (git_watch.NotAGitRepo, FileNotFoundError):
            self._end_watching("Watched directory is gone or no longer a git repo.")
            return
        self._snapshot = snap
        header = self.query_one("#header", HeaderBar)
        header.repo_label = snap.root.name
        header.branch_label = snap.branch
        header.stat_label = git_watch.diffstat_from_text(
            git_watch.cumulative_diff(snap.root)
        ).render()
        self._render_file_list(changed_paths)
        self._render_diff()

    def _render_file_list(self, changed_paths: Optional[set[Path]] = None) -> None:
        file_list = self.query_one("#files", FilePane)
        file_list.clear()
        assert self._snapshot is not None
        if not self._snapshot.files:
            file_list.append(ListItem(Static("(clean)", classes="dim")))
            return
        for f in self._snapshot.files:
            style = {
                "M": "yellow",
                "A": "green",
                "D": "red",
                "??": "green",
                "R": "cyan",
            }.get(f.status, "white")
            label = Text(f"{f.marker} {f.path}", style=style)
            file_list.append(ListItem(Static(label)))

        if self._follow and changed_paths:
            match = self._match_changed_file(changed_paths)
            if match is None:
                # Some editors bounce writes through swap/hidden files that
                # never appear in git status, so the direct path match can
                # come up empty even though something real changed. Fall
                # back to whichever tracked file was modified most recently.
                match = self._newest_file_index()
            if match is not None:
                self._selected_index = match

        if self._selected_index >= len(self._snapshot.files):
            self._selected_index = max(0, len(self._snapshot.files) - 1)
        self._programmatic_select = True
        file_list.index = self._selected_index

    def _match_changed_file(self, changed_paths: set[Path]) -> Optional[int]:
        """Map fs-watcher paths to an index in the current file list.

        Resolves both sides (watcher root vs. git-reported root can differ on
        symlinked paths, e.g. macOS's /tmp -> /private/tmp) before comparing.
        """
        assert self._snapshot is not None
        root = self._snapshot.root.resolve()
        rel_changed = set()
        for p in changed_paths:
            try:
                rel_changed.add(str(p.resolve().relative_to(root)))
            except (ValueError, OSError):
                continue
        if not rel_changed:
            return None
        for i, f in enumerate(self._snapshot.files):
            if f.path in rel_changed:
                return i
        return None

    def _render_diff(self) -> None:
        diff_pane = self.query_one("#diff", DiffPane)
        header = self.query_one("#header", HeaderBar)
        if self._snapshot is None:
            return
        if self._cumulative:
            text = git_watch.cumulative_diff(self._snapshot.root)
            diff_pane.show_diff(text, hint_filename=None, wrap=self._wrap)
        elif self._snapshot.files:
            f = self._snapshot.files[self._selected_index]
            text = git_watch.diff_for_file(self._snapshot.root, f)
            diff_pane.show_diff(text, hint_filename=f.path, wrap=self._wrap)
        else:
            diff_pane.show_diff("", hint_filename=None, wrap=self._wrap)
        header.wrap_label = "wrap: on" if self._wrap else "wrap: off"

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Fires for both keyboard nav, mouse clicks, and our own programmatic
        follow-mode selection in the file list."""
        if event.list_view.id != "files" or not self._snapshot or not self._snapshot.files:
            return
        index = event.list_view.index
        if index is None:
            return
        self._selected_index = index
        if self._programmatic_select:
            # This selection came from follow-mode auto-jumping, not the user
            # browsing manually — leave follow mode engaged.
            self._programmatic_select = False
        elif self._follow:
            # A real user click/keypress while following: hand control back
            # to the user until they re-enable follow with 'f'.
            self._follow = False
            self._update_follow_label()
        if not self._cumulative:
            self._render_diff()

    # -- agent status -------------------------------------------------------

    def _on_agent_status(self, status: str) -> None:
        self.call_from_thread(self._apply_status, status)

    def _apply_status(self, status: str) -> None:
        header = self.query_one("#header", HeaderBar)
        header.status_label = status
        header.status_style = STATUS_STYLE.get(status, "dim")

    def _on_subscriber_error(self, exc: Exception) -> None:
        # Non-fatal: fall back to periodic polling via the CLI.
        pass

    def _poll_pane_health(self) -> None:
        if not self._pane_id or self._ended:
            return
        try:
            pane = herdr_client.get_pane(self._pane_id)
        except herdr_client.HerdrError:
            self._end_watching("Watched Herdr pane closed.")
            return
        if pane.agent_status:
            self._apply_status(pane.agent_status)

    def _end_watching(self, message: str) -> None:
        self._ended = True
        banner = self.query_one("#banner", BannerPane)
        banner.update(f"⚠ watching ended — {message}  (press q to quit)")
        banner.display = True
        if self._watcher:
            self._watcher.stop()
            self._watcher = None

    # -- actions ------------------------------------------------------------

    def action_cursor_down(self) -> None:
        if not self._snapshot or not self._snapshot.files:
            return
        new_index = min(self._selected_index + 1, len(self._snapshot.files) - 1)
        # Setting .index fires ListView.Highlighted, which re-renders the diff.
        self.query_one("#files", FilePane).index = new_index

    def action_cursor_up(self) -> None:
        if not self._snapshot or not self._snapshot.files:
            return
        new_index = max(self._selected_index - 1, 0)
        self.query_one("#files", FilePane).index = new_index

    def action_toggle_cumulative(self) -> None:
        self._cumulative = not self._cumulative
        self._render_diff()

    def action_toggle_wrap(self) -> None:
        self._wrap = not self._wrap
        self._render_diff()

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        self._update_follow_label()
        if self._follow and self._snapshot and self._snapshot.files:
            # Jump to the most recently modified file on disk right away,
            # rather than waiting for the next fs event.
            index = self._newest_file_index()
            if index is not None:
                self._programmatic_select = True
                self.query_one("#files", FilePane).index = index

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _newest_file_index(self) -> Optional[int]:
        if not self._snapshot or not self._snapshot.files:
            return None
        root = self._snapshot.root
        newest_index, newest_mtime = None, -1.0
        for i, f in enumerate(self._snapshot.files):
            mtime = self._mtime(root / f.path)
            if mtime > newest_mtime:
                newest_index, newest_mtime = i, mtime
        return newest_index

    def _update_follow_label(self) -> None:
        header = self.query_one("#header", HeaderBar)
        header.follow_label = "follow: on" if self._follow else "follow: off"

    def action_refresh_now(self) -> None:
        self._reload()


def _resolve_target(args: argparse.Namespace) -> tuple[Path, Optional[str]]:
    if args.path:
        return Path(args.path).expanduser().resolve(), None

    if not herdr_client.in_herdr_pane():
        print(
            "herdr-diffview: not running inside a Herdr pane (HERDR_ENV is unset).\n"
            "Run this inside a Herdr-managed pane, or pass --path <dir> to watch "
            "a directory directly.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.pane:
        pane = herdr_client.get_pane(args.pane)
    else:
        pane = herdr_client.find_agent_pane()
        if pane is None:
            print(
                "herdr-diffview: no agent pane detected in this Herdr workspace.\n"
                "Start an agent (claude/codex/...) in a sibling pane, or pass "
                "--pane <id> / --path <dir> explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)

    cwd = pane.effective_cwd
    if not cwd:
        print(
            f"herdr-diffview: pane {pane.pane_id} has no resolvable cwd.",
            file=sys.stderr,
        )
        sys.exit(1)
    return Path(cwd).expanduser().resolve(), pane.pane_id


def main() -> None:
    parser = argparse.ArgumentParser(prog="herdr-diffview")
    parser.add_argument(
        "--pane", help="Watch this Herdr pane's cwd instead of auto-detecting."
    )
    parser.add_argument(
        "--path", help="Watch this directory directly, skipping Herdr lookup."
    )
    args = parser.parse_args()

    target_path, pane_id = _resolve_target(args)

    try:
        git_watch.repo_root(target_path)
    except git_watch.NotAGitRepo:
        print(f"herdr-diffview: {target_path} is not a git repository.", file=sys.stderr)
        sys.exit(1)

    app = HerdrDiffApp(target_path, pane_id)
    app.run()


if __name__ == "__main__":
    main()
