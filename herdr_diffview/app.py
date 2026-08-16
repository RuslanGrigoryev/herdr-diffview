from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Optional

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Input, ListItem, ListView, Static, Tree
from textual.widgets.tree import TreeNode

from . import config as config_module
from . import git_watch, herdr_client
from .diff_render import DARK_THEMES, RenderedDiff, render_diff
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
    follow_label: reactive[str] = reactive("")
    theme_label: reactive[str] = reactive("")
    diff_mode_label: reactive[str] = reactive("")
    context_label: reactive[str] = reactive("")

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
        if self.follow_label:
            t.append("   ", style="dim")
            t.append(self.follow_label, style="green" if "on" in self.follow_label else "dim")
        if self.theme_label:
            t.append("   ", style="dim")
            t.append(self.theme_label, style="dim")
        if self.diff_mode_label:
            t.append("   ", style="dim")
            style = "bold magenta" if self.diff_mode_label != "vs HEAD" else "dim"
            t.append(self.diff_mode_label, style=style)
        if self.context_label:
            t.append("   ", style="dim")
            t.append(self.context_label, style="dim")
        return t


class FilePane(ListView):
    pass


class FileTreePane(Tree):
    """Directory-tree view over the same file list as FilePane. Leaf nodes
    carry the file's index (into RepoSnapshot.files) as their `data`;
    directory nodes carry None so highlight handling can tell them apart.
    """

    def __init__(
        self,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            "(root)", data=None, name=name, id=id, classes=classes, disabled=disabled
        )
        self.show_root = False
        self.guide_depth = 2

    def rebuild(
        self,
        files: list[git_watch.FileChange],
        followed_index: Optional[int] = None,
    ) -> None:
        collapsed_dirs = self._collapsed_dir_paths()
        self.clear()
        dir_nodes: dict[str, TreeNode] = {}

        def dir_node_for(parts: tuple[str, ...]) -> TreeNode:
            if not parts:
                return self.root
            key = "/".join(parts)
            if key in dir_nodes:
                return dir_nodes[key]
            parent = dir_node_for(parts[:-1])
            # New directories default to expanded; only a dir the user
            # explicitly collapsed before this rebuild stays collapsed.
            node = parent.add(parts[-1], data=None, expand=key not in collapsed_dirs)
            dir_nodes[key] = node
            return node

        style_map = {
            "M": "yellow",
            "A": "green",
            "D": "red",
            "??": "green",
            "R": "cyan",
        }
        for i, f in enumerate(files):
            parts = f.path.split("/")
            parent = dir_node_for(tuple(parts[:-1]))
            style = style_map.get(f.status, "white")
            if i == followed_index:
                label = Text("> ", style=Style(bold=True, bgcolor="dark_green"))
                label.append(f"{f.marker} {parts[-1]}", style=f"bold {style}")
            else:
                label = Text(f"  {f.marker} {parts[-1]}", style=style)
            parent.add_leaf(label, data=i)

    def _collapsed_dir_paths(self) -> set[str]:
        """Best-effort: directory nodes have no stable identity across a
        clear()+rebuild, so remembering by depth-first path string is the
        simplest way to keep a dir the user collapsed from popping back open
        on every refresh. New directories aren't in this set, so they
        default to expanded (dir_node_for's `not in` check)."""
        collapsed: set[str] = set()

        def walk(node: TreeNode, path: tuple[str, ...]) -> None:
            for child in node.children:
                if child.allow_expand:
                    child_path = path + (str(child.label),)
                    if not child.is_expanded:
                        collapsed.add("/".join(child_path))
                    walk(child, child_path)

        walk(self.root, ())
        return collapsed

    def select_index(self, index: Optional[int]) -> None:
        """Move the tree cursor to the leaf node carrying this file index (or
        clear the cursor entirely if index/node not found)."""
        node = None if index is None else self._node_for_index(index)
        self.move_cursor(node)

    def _node_for_index(self, index: int) -> Optional[TreeNode]:
        for node in self._nodes_by_index():
            if node.data == index:
                return node
        return None

    def _nodes_by_index(self):
        def walk(node: TreeNode):
            for child in node.children:
                yield child
                yield from walk(child)

        return walk(self.root)


class DiffContent(Static):
    """The actual diff text. height:auto so it grows to fit content — it must
    live inside a scrolling container (DiffPane) to be viewable past one
    screen, since Static itself never clips or scrolls on its own."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rendered: Optional[RenderedDiff] = None

    def show_diff(
        self,
        text: str,
        hint_filename: str | None = None,
        theme: str = "ansi_dark",
    ) -> None:
        self.rendered = render_diff(text, hint_filename, wrap=True, theme=theme)
        self.update(self.rendered.text)


class DiffPane(VerticalScroll):
    """Scrollable viewport around DiffContent. Mouse wheel and PageUp/Down/
    Home/End work here out of the box (VerticalScroll's own bindings); j/k/
    arrow keys are still routed to the file list regardless of focus, so
    this container intentionally doesn't grab those."""

    def compose(self) -> ComposeResult:
        yield DiffContent(id="diff-content")

    def show_diff(self, *args, **kwargs) -> None:
        self.query_one("#diff-content", DiffContent).show_diff(*args, **kwargs)
        self.scroll_home(animate=False)

    @property
    def rendered(self) -> Optional[RenderedDiff]:
        return self.query_one("#diff-content", DiffContent).rendered

    def scroll_to_line(self, line: int, animate: bool = True) -> None:
        """Scroll so the given 0-indexed logical line is visible near the
        top. Approximate under word-wrap (a logical line can span more than
        one visual row), close enough for jump-to-hunk/search since it's
        always at-or-before the true position, never past it."""
        self.scroll_to(y=max(0, line - 1), animate=animate)


class BannerPane(Static):
    """Shown when watching has ended (dir gone / pane closed)."""


class SearchBar(Input):
    """Docked search input for the diff pane, shown on '/' and hidden again
    on Escape or Enter. A plain substring search over the diff's own text,
    not a full editor-grade search — good enough for jumping to a symbol or
    line in a diff that's grown too long to scan by eye."""


_UNSET = object()


class HerdrDiffApp(App):
    _UNSET = _UNSET
    CSS = """
    Screen { layout: vertical; }
    HeaderBar { height: 1; background: $panel; dock: top; }
    BannerPane { height: 3; background: $error 20%; color: $error; padding: 1; dock: top; }
    #body { height: 1fr; layout: vertical; }
    FilePane, FileTreePane {
        min-height: 5;
        border-bottom: solid $accent;
    }
    DiffPane { height: 1fr; overflow-y: auto; overflow-x: hidden; scrollbar-gutter: stable; }
    DiffContent { width: 100%; padding: 0 1; }
    SearchBar { dock: bottom; display: none; }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("a", "toggle_cumulative", "All-files diff"),
        Binding("b", "toggle_base_branch_diff", "Diff vs base branch"),
        Binding("f", "toggle_follow", "Follow latest"),
        Binding("t", "cycle_theme", "Theme"),
        Binding("v", "toggle_view_mode", "List/Tree"),
        Binding("pageup", "diff_page_up", "Diff PgUp", show=False),
        Binding("pagedown", "diff_page_down", "Diff PgDn", show=False),
        Binding("home", "diff_scroll_home", "Diff Home", show=False),
        Binding("end", "diff_scroll_end", "Diff End", show=False),
        Binding("n", "next_hunk", "Next hunk"),
        Binding("N", "prev_hunk", "Prev hunk", show=False),
        Binding("plus", "grow_file_panel", "Grow files panel", show=False),
        Binding("minus", "shrink_file_panel", "Shrink files panel", show=False),
        Binding("right_square_bracket", "more_context", "More context"),
        Binding("left_square_bracket", "less_context", "Less context", show=False),
        Binding("w", "toggle_full_function", "Whole function"),
        Binding("slash", "open_search", "Search"),
        Binding("escape", "close_search", "Close search", show=False),
        Binding("r", "refresh_now", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, target_path: Path, pane_id: Optional[str]) -> None:
        super().__init__()
        self._target_path = target_path
        self._pane_id = pane_id
        self._snapshot: Optional[git_watch.RepoSnapshot] = None
        self._selected_index = 0
        # Tracks what's currently rendered in the file list/tree (see
        # _render_file_list) so a reload that doesn't actually change the
        # displayed set/markers can skip the clear()+rebuild that would
        # otherwise flicker every file row on every debounced fs event.
        self._file_list_signature: tuple = ()
        self._last_pushed_diff_token: Optional[str] = None
        self._cumulative = False
        self._base_branch_diff = False
        self._base_branch: Optional[str] = None
        self._branch_files: list[git_watch.FileChange] = []
        # The index we ourselves last set programmatically (follow-mode
        # auto-jump, reload re-sync, 'f' re-enable). A Highlighted/
        # NodeHighlighted event is only treated as a real user click when its
        # index differs from this — deliberately NOT consumed/reset on
        # match, since a single assignment can produce zero, one, or two
        # matching events across ListView/Tree (clear() posts its own,
        # separate from the following index= assignment) and Textual
        # handlers read live widget state rather than a per-event snapshot,
        # so every one of them would carry the same final index anyway.
        self._expected_index: Optional[int] = self._UNSET
        self._watcher: Optional[DirWatcher] = None
        self._subscriber: Optional[AgentStatusSubscriber] = None
        self._ended = False
        self._search_matches: list[int] = []
        self._search_index = -1
        self._search_query = ""
        self._config = config_module.Config.load(len(DARK_THEMES))
        self._theme_index = self._config.theme_index
        self._follow = self._config.follow
        self._view_mode = self._config.view_mode  # "list" or "tree"
        self._file_panel_height = self._config.file_panel_height
        self._context_lines = self._config.context_lines
        self._full_function = self._config.full_function

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        yield BannerPane(id="banner")
        with Vertical(id="body"):
            yield FilePane(id="files")
            yield FileTreePane(id="file-tree")
            yield DiffPane(id="diff")
        yield SearchBar(id="search", placeholder="search diff… (Enter: next, Esc: close)")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#banner", BannerPane).display = False
        self.query_one("#file-tree", FileTreePane).display = False
        self._apply_file_panel_height()
        self._update_follow_label()
        self._update_theme_label()
        self._update_diff_mode_label()
        self._update_context_label()
        self._update_view_mode_visibility()
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
        if self._base_branch_diff and self._base_branch:
            self._branch_files = git_watch.branch_file_list(snap.root, self._base_branch)
        header = self.query_one("#header", HeaderBar)
        header.repo_label = snap.root.name
        header.branch_label = snap.branch
        stat = git_watch.diffstat_from_text(git_watch.cumulative_diff(snap.root))
        header.stat_label = stat.render()
        self._push_diff_metadata(stat)
        self._render_file_list(changed_paths)
        self._render_diff()

    def _push_diff_metadata(self, stat: git_watch.DiffStat) -> None:
        """Best-effort: surface the current diffstat as a $diff token on the
        watched Herdr pane, so herdr's own sidebar can show e.g. '+340 -12'
        without this pane needing to be open/focused. Runs off the UI thread
        since it shells out to the herdr CLI; failures are swallowed — this
        is a nice-to-have, never something that should interrupt watching.
        """
        if not self._pane_id or not herdr_client.in_herdr_pane():
            return
        value = stat.render() or "clean"
        if value == self._last_pushed_diff_token:
            return
        self._last_pushed_diff_token = value
        pane_id = self._pane_id

        def push() -> None:
            try:
                herdr_client.report_metadata(
                    pane_id,
                    source="herdr-diffview",
                    token={"diff": value},
                    ttl_ms=None,
                )
            except herdr_client.HerdrError:
                pass

        threading.Thread(target=push, daemon=True).start()

    def _active_files(self) -> list[git_watch.FileChange]:
        if self._base_branch_diff:
            return self._branch_files
        return self._snapshot.files if self._snapshot else []

    def _render_file_list(self, changed_paths: Optional[set[Path]] = None) -> None:
        file_list = self.query_one("#files", FilePane)
        tree = self.query_one("#file-tree", FileTreePane)
        files = self._active_files()

        if not files:
            if self._file_list_signature != ():
                file_list.clear()
                file_list.append(ListItem(Static("(clean)", classes="dim")))
                tree.rebuild([])
                self._file_list_signature = ()
            return

        # Follow-mode auto-jump only makes sense against the live working
        # tree (that's what fs events describe); base-branch mode keeps
        # whatever selection it already had instead of trying to match.
        if self._follow and changed_paths and not self._base_branch_diff:
            match = self._match_changed_file(changed_paths)
            if match is None:
                # Some editors bounce writes through swap/hidden files that
                # never appear in git status, so the direct path match can
                # come up empty even though something real changed. Fall
                # back to whichever tracked file was modified most recently.
                match = self._newest_file_index()
            if match is not None:
                self._selected_index = match

        if self._selected_index >= len(files):
            self._selected_index = max(0, len(files) - 1)

        # Follow mode always keeps the selection synced to the latest change,
        # so the currently-selected row *is* the auto-followed one whenever
        # it's on — mark it distinctly from a plain manual selection.
        followed_index = self._selected_index if self._follow else None

        # Rebuilding the list/tree on every reload (which fires on every
        # debounced fs event — i.e. potentially every ~150ms while the agent
        # is actively writing) causes a visible flash/flicker even when the
        # actual file set and markers haven't changed, since ListView.clear()
        # tears down and recreates every row. Skip the rebuild entirely when
        # nothing about what should be *displayed* actually changed; only the
        # selected diff content below needs to refresh every time.
        signature = tuple((f.path, f.status) for f in files) + (followed_index,)
        if signature != self._file_list_signature:
            file_list.clear()
            for i, f in enumerate(files):
                style = {
                    "M": "yellow",
                    "A": "green",
                    "D": "red",
                    "??": "green",
                    "R": "cyan",
                }.get(f.status, "white")
                if i == followed_index:
                    # Bold text alone can be indistinguishable on the row
                    # that's ALSO the list's cursor highlight (ListView's own
                    # CSS already recolors that row); an explicit background
                    # tint on the marker chars themselves stays visible
                    # regardless.
                    label = Text("> ", style=Style(bold=True, bgcolor="dark_green"))
                    label.append(f"{f.marker} {f.path}", style=f"bold {style}")
                else:
                    label = Text(f"  {f.marker} {f.path}", style=style)
                file_list.append(ListItem(Static(label)))
            tree.rebuild(files, followed_index=followed_index)
            self._file_list_signature = signature

        self._expected_index = self._selected_index
        file_list.index = self._selected_index
        tree.select_index(self._selected_index)

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
        for i, f in enumerate(self._active_files()):
            if f.path in rel_changed:
                return i
        return None

    def _render_diff(self) -> None:
        diff_pane = self.query_one("#diff", DiffPane)
        if self._snapshot is None:
            return
        theme = DARK_THEMES[self._theme_index]
        files = self._active_files()
        ctx = self._context_lines
        full_fn = self._full_function
        if self._base_branch_diff and self._base_branch:
            if self._cumulative:
                text = git_watch.branch_diff(
                    self._snapshot.root, self._base_branch, ctx, full_fn
                )
                diff_pane.show_diff(text, hint_filename=None, theme=theme)
            elif files:
                f = files[self._selected_index]
                text = git_watch.branch_diff_for_file(
                    self._snapshot.root, self._base_branch, f, ctx, full_fn
                )
                diff_pane.show_diff(text, hint_filename=f.path, theme=theme)
            else:
                diff_pane.show_diff("", hint_filename=None, theme=theme)
        elif self._cumulative:
            text = git_watch.cumulative_diff(self._snapshot.root, ctx, full_fn)
            diff_pane.show_diff(text, hint_filename=None, theme=theme)
        elif files:
            f = files[self._selected_index]
            text = git_watch.diff_for_file(self._snapshot.root, f, ctx, full_fn)
            diff_pane.show_diff(text, hint_filename=f.path, theme=theme)
        else:
            diff_pane.show_diff("", hint_filename=None, theme=theme)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Fires for both keyboard nav, mouse clicks, and our own programmatic
        follow-mode selection in the file list."""
        if event.list_view.id != "files" or not self._active_files():
            return
        index = event.list_view.index
        if index is None:
            return
        self._select_from_view(index)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Fires for keyboard nav, mouse clicks, and our own programmatic
        select_index() calls in the directory tree. Directory nodes carry
        data=None and aren't selectable files."""
        if event.node.tree.id != "file-tree":
            return
        index = event.node.data
        if index is None or not self._active_files():
            return
        self._select_from_view(index)

    def _select_from_view(self, index: int) -> None:
        self._selected_index = index
        if index == self._expected_index:
            # Matches what we ourselves last set — not a user click. (A
            # genuine click that happens to land back on the already-
            # programmatically-selected file is indistinguishable from this
            # and is harmless to treat the same way: nothing changes either
            # way since that file is already showing.)
            pass
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
        if self._view_mode == "tree":
            self.query_one("#file-tree", FileTreePane).action_cursor_down()
            return
        files = self._active_files()
        if not files:
            return
        new_index = min(self._selected_index + 1, len(files) - 1)
        # Setting .index fires ListView.Highlighted, which re-renders the diff.
        self.query_one("#files", FilePane).index = new_index

    def action_cursor_up(self) -> None:
        if self._view_mode == "tree":
            self.query_one("#file-tree", FileTreePane).action_cursor_up()
            return
        if not self._active_files():
            return
        new_index = max(self._selected_index - 1, 0)
        self.query_one("#files", FilePane).index = new_index

    def action_toggle_cumulative(self) -> None:
        self._cumulative = not self._cumulative
        self._render_diff()

    def action_toggle_base_branch_diff(self) -> None:
        if self._base_branch_diff:
            self._base_branch_diff = False
            self._branch_files = []
            self._update_diff_mode_label()
            self._resync_selection_and_render()
            return
        if not self._snapshot:
            return
        base = git_watch.detect_base_branch(self._snapshot.root)
        if base is None:
            self._flash_banner(
                "No default branch found to diff against "
                "(tried origin/HEAD, main, master)."
            )
            return
        self._base_branch = base
        self._base_branch_diff = True
        self._branch_files = git_watch.branch_file_list(self._snapshot.root, base)
        self._update_diff_mode_label()
        self._resync_selection_and_render()

    def _resync_selection_and_render(self) -> None:
        """Switching between working-tree and branch file lists changes what
        _active_files() returns entirely, so re-clamp the selection and
        rebuild both file views before re-rendering the diff — otherwise the
        old index could point at an unrelated file in the new list."""
        files = self._active_files()
        self._selected_index = min(self._selected_index, max(0, len(files) - 1))
        self._render_file_list()
        self._render_diff()

    def _update_diff_mode_label(self) -> None:
        header = self.query_one("#header", HeaderBar)
        if self._base_branch_diff and self._base_branch:
            header.diff_mode_label = f"vs {self._base_branch}"
        else:
            header.diff_mode_label = "vs HEAD"

    def _flash_banner(self, message: str, seconds: float = 3.0) -> None:
        """Transient banner for a non-fatal notice, reusing BannerPane's
        already-styled slot without permanently ending the watch session."""
        banner = self.query_one("#banner", BannerPane)
        banner.update(f"⚠ {message}")
        banner.display = True
        self.set_timer(seconds, self._hide_banner)

    def _hide_banner(self) -> None:
        if self._ended:
            return
        self.query_one("#banner", BannerPane).display = False

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        self._update_follow_label()
        self._save_config()
        if self._follow and not self._base_branch_diff and self._active_files():
            # Jump to the most recently modified file on disk right away,
            # rather than waiting for the next fs event. (Only meaningful
            # against the live working tree, same as the fs-event path.)
            index = self._newest_file_index()
            if index is not None:
                self._selected_index = index
        # Route through _render_file_list either way: it both re-syncs the
        # index and (re)draws the > auto-followed marker — which needs to
        # disappear immediately when follow turns off, not just stop moving.
        self._render_file_list()
        if not self._cumulative:
            self._render_diff()

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _newest_file_index(self) -> Optional[int]:
        files = self._active_files()
        if not self._snapshot or not files:
            return None
        root = self._snapshot.root
        newest_index, newest_mtime = None, -1.0
        for i, f in enumerate(files):
            mtime = self._mtime(root / f.path)
            if mtime > newest_mtime:
                newest_index, newest_mtime = i, mtime
        return newest_index

    def _update_follow_label(self) -> None:
        header = self.query_one("#header", HeaderBar)
        header.follow_label = "follow: on" if self._follow else "follow: off"

    def action_cycle_theme(self) -> None:
        self._theme_index = (self._theme_index + 1) % len(DARK_THEMES)
        self._update_theme_label()
        self._render_diff()
        self._save_config()

    def _update_theme_label(self) -> None:
        header = self.query_one("#header", HeaderBar)
        header.theme_label = f"theme: {DARK_THEMES[self._theme_index]}"

    def action_toggle_view_mode(self) -> None:
        self._view_mode = "tree" if self._view_mode == "list" else "list"
        self._update_view_mode_visibility()
        self._save_config()

    def action_grow_file_panel(self) -> None:
        self._resize_file_panel(+5)

    def action_shrink_file_panel(self) -> None:
        self._resize_file_panel(-5)

    def _resize_file_panel(self, delta: int) -> None:
        self._file_panel_height = max(
            config_module.MIN_FILE_PANEL_HEIGHT,
            min(config_module.MAX_FILE_PANEL_HEIGHT, self._file_panel_height + delta),
        )
        self._apply_file_panel_height()
        self._save_config()

    def _apply_file_panel_height(self) -> None:
        height = f"{self._file_panel_height}%"
        self.query_one("#files", FilePane).styles.height = height
        self.query_one("#file-tree", FileTreePane).styles.height = height

    def _save_config(self) -> None:
        self._config.theme_index = self._theme_index
        self._config.follow = self._follow
        self._config.view_mode = self._view_mode
        self._config.file_panel_height = self._file_panel_height
        self._config.context_lines = self._context_lines
        self._config.full_function = self._full_function
        self._config.save()

    def action_more_context(self) -> None:
        self._full_function = False
        self._context_lines = min(
            git_watch.MAX_CONTEXT_LINES, self._context_lines + git_watch.CONTEXT_STEP
        )
        self._update_context_label()
        self._render_diff()
        self._save_config()

    def action_less_context(self) -> None:
        self._full_function = False
        self._context_lines = max(
            git_watch.MIN_CONTEXT_LINES, self._context_lines - git_watch.CONTEXT_STEP
        )
        self._update_context_label()
        self._render_diff()
        self._save_config()

    def action_toggle_full_function(self) -> None:
        self._full_function = not self._full_function
        self._update_context_label()
        self._render_diff()
        self._save_config()

    def _update_context_label(self) -> None:
        header = self.query_one("#header", HeaderBar)
        if self._full_function:
            header.context_label = "context: whole function"
        elif self._context_lines != git_watch.DEFAULT_CONTEXT_LINES:
            header.context_label = f"context: {self._context_lines}"
        else:
            header.context_label = ""

    def _update_view_mode_visibility(self) -> None:
        file_list = self.query_one("#files", FilePane)
        tree = self.query_one("#file-tree", FileTreePane)
        file_list.display = self._view_mode == "list"
        tree.display = self._view_mode == "tree"
        if self._view_mode == "tree":
            tree.focus()
        else:
            file_list.focus()

    def action_diff_page_up(self) -> None:
        self.query_one("#diff", DiffPane).scroll_page_up()

    def action_diff_page_down(self) -> None:
        self.query_one("#diff", DiffPane).scroll_page_down()

    def action_diff_scroll_home(self) -> None:
        self.query_one("#diff", DiffPane).scroll_home()

    def action_diff_scroll_end(self) -> None:
        self.query_one("#diff", DiffPane).scroll_end()

    def action_next_hunk(self) -> None:
        self._jump_hunk(1)

    def action_prev_hunk(self) -> None:
        self._jump_hunk(-1)

    def _jump_hunk(self, direction: int) -> None:
        diff_pane = self.query_one("#diff", DiffPane)
        rendered = diff_pane.rendered
        if not rendered or not rendered.hunk_lines:
            return
        current_line = int(diff_pane.scroll_y) + 1
        hunks = rendered.hunk_lines
        if direction > 0:
            target = next((h for h in hunks if h > current_line), hunks[0])
        else:
            target = next((h for h in reversed(hunks) if h < current_line), hunks[-1])
        diff_pane.scroll_to_line(target)

    def action_open_search(self) -> None:
        search = self.query_one("#search", SearchBar)
        search.display = True
        search.value = self._search_query
        search.focus()

    def action_close_search(self) -> None:
        search = self.query_one("#search", SearchBar)
        if search.display:
            search.display = False
            self.query_one("#diff", DiffPane).focus()
            return
        # Escape with the search bar already closed: no-op (lets Escape fall
        # through harmlessly rather than binding-erroring elsewhere).

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search":
            return
        query = event.value.strip()
        if not query:
            return
        if query != self._search_query:
            self._search_query = query
            self._recompute_search_matches()
            self._search_index = -1
        self._jump_search(1)

    def _recompute_search_matches(self) -> None:
        diff_pane = self.query_one("#diff", DiffPane)
        rendered = diff_pane.rendered
        self._search_matches = []
        if not rendered or not self._search_query:
            return
        needle = self._search_query.lower()
        self._search_matches = [
            i for i, line in enumerate(rendered.lines) if needle in line.lower()
        ]

    def _jump_search(self, direction: int) -> None:
        if not self._search_matches:
            self._flash_banner(f"No matches for '{self._search_query}'.")
            return
        self._search_index = (self._search_index + direction) % len(self._search_matches)
        line = self._search_matches[self._search_index]
        self.query_one("#diff", DiffPane).scroll_to_line(line)

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
