"""Render a unified diff with real language syntax highlighting and an
old/new line-number gutter.

Plain `Syntax(text, "diff")` only understands diff punctuation (+/-/@@), not
the language inside each hunk. To get e.g. Python/TypeScript token colors
*and* diff +/- coloring at once, we lex each content line with Pygments'
language-appropriate lexer (guessed from the file extension) and layer a
background style for added/removed lines on top. Uses only Pygments' public
`lex()` API plus Rich's public `Syntax.get_theme()` — no private Syntax
internals, so this stays stable across Rich/Textual versions.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

# Softer than a full ANSI green/red background — closer to how GitHub/lazygit
# tint diff lines, easier to read code through on long diffs.
ADDED_BG = Style(bgcolor="#0f2415")
REMOVED_BG = Style(bgcolor="#2a1216")
# Layered on top of ADDED_BG/REMOVED_BG for just the tokens that actually
# changed within a paired -/+ line (word-level diff, à la Claude Code /
# GitHub's inline diff) — the rest of the line stays at the muted tint above
# so the eye jumps straight to what's different instead of re-reading the
# whole line.
BRIGHT_ADDED_BG = Style(bgcolor="#1f7a3a", bold=True)
BRIGHT_REMOVED_BG = Style(bgcolor="#7a2030", bold=True)
HUNK_STYLE = Style(color="cyan", bold=True)
FILE_HEADER_STYLE = Style(color="bright_white", bold=True)
META_STYLE = Style(color="grey50")
GUTTER_STYLE = Style(color="grey42")
GUTTER_ADDED_STYLE = Style(color="green")
GUTTER_REMOVED_STYLE = Style(color="red")
GUTTER_SEPARATOR_STYLE = Style(color="grey23")
GUTTER_SEPARATOR = "\u2502"  # │

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Curated dark Pygments styles, cycled with the app's 't' key. 'ansi_dark' is
# Rich's own 16-color-safe default (works in any terminal, even one with a
# limited palette); the rest are real Pygments styles that use full 24-bit
# color and generally look closer to editor themes people already know.
DARK_THEMES = [
    "ansi_dark",
    "monokai",
    "dracula",
    "github-dark",
    "one-dark",
    "nord",
    "gruvbox-dark",
    "night-owl",
    "solarized-dark",
    "zenburn",
    "material",
    "inkpot",
    "paraiso-dark",
    "stata-dark",
]

_theme_cache: dict[str, object] = {}


@dataclass
class RenderedDiff:
    """A rendered diff plus metadata Text's __slots__ can't carry directly:
    the 0-indexed line number of each hunk header, for jump-to-hunk
    navigation, and each line's plain text, for search."""

    text: Text
    hunk_lines: list[int] = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        return self.text.plain.split("\n")


def get_theme(name: str):
    """Cached Syntax theme lookup — building a PygmentsSyntaxTheme parses and
    walks the whole style's token table, not free to redo every render."""
    theme = _theme_cache.get(name)
    if theme is None:
        try:
            theme = Syntax.get_theme(name)
        except Exception:
            # Unknown style name (e.g. a Pygments version that dropped it) —
            # fall back to Rich's own always-available ANSI theme rather
            # than crashing the render.
            theme = Syntax.get_theme("ansi_dark")
        _theme_cache[name] = theme
    return theme


def _lexer_name_for_filename(filename: str) -> str:
    try:
        return get_lexer_for_filename(filename).aliases[0]
    except (ClassNotFound, IndexError):
        return "text"


def _highlight_line(code: str, lexer_name: str, theme_name: str) -> Text:
    if not code or lexer_name == "text":
        return Text(code)
    try:
        lexer = get_lexer_by_name(lexer_name, stripnl=False)
    except ClassNotFound:
        return Text(code)
    theme = get_theme(theme_name)
    text = Text()
    try:
        for token_type, value in lexer.get_tokens(code):
            if value == "":
                continue
            style = theme.get_style_for_token(token_type)
            text.append(value, style=style)
    except Exception:
        return Text(code)
    # get_tokens appends a trailing newline token; strip it, caller adds its own.
    plain = text.plain
    if plain.endswith("\n"):
        text = text[: len(plain) - 1]
    return text


_WORD_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")


def _tokenize(line: str) -> list[str]:
    """Split into words/whitespace-runs/single-punctuation-chars — the same
    granularity `git diff --word-diff` and GitHub's inline diff use, so a
    rename like `poolData` -> `pool` highlights as one changed word instead
    of a scatter of changed characters."""
    return _WORD_TOKEN_RE.findall(line)


def _is_pairable_modification(removed: str, added: str) -> bool:
    """Heuristic: only pair a -/+ line as 'the same line, modified' (worth a
    word diff) when they're similar enough that highlighting the difference
    is informative rather than noise — two unrelated lines that happen to
    sit next to each other would just get a useless all-bright overlay.
    """
    if not removed or not added:
        return False
    ratio = difflib.SequenceMatcher(None, removed, added, autojunk=False).ratio()
    return ratio >= 0.35


def _word_diff_spans(
    removed: str, added: str
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Character-offset spans (into `removed` and `added` respectively) of
    the tokens that differ between the two lines, via SequenceMatcher over
    word-granularity tokens. Returns (removed_spans, added_spans)."""
    removed_tokens = _tokenize(removed)
    added_tokens = _tokenize(added)
    matcher = difflib.SequenceMatcher(None, removed_tokens, added_tokens, autojunk=False)

    def _token_offsets(tokens: list[str]) -> list[int]:
        offsets = [0]
        for t in tokens:
            offsets.append(offsets[-1] + len(t))
        return offsets

    removed_offsets = _token_offsets(removed_tokens)
    added_offsets = _token_offsets(added_tokens)
    removed_spans: list[tuple[int, int]] = []
    added_spans: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            removed_spans.append((removed_offsets[i1], removed_offsets[i2]))
        if j2 > j1:
            added_spans.append((added_offsets[j1], added_offsets[j2]))
    return removed_spans, added_spans


def _pair_modified_lines(lines: list[str]) -> dict[int, list[tuple[int, int]]]:
    """Scan raw diff lines for consecutive runs of '-' lines immediately
    followed by '+' lines (git's standard shape for 'these lines became
    those lines'), pair them up index-wise, and word-diff each pair.

    Returns {line_index: [(start, end), ...]} of character spans (into that
    line's content *after* stripping the leading +/-) that changed, for
    every line that got paired. A run of N removed + M added lines pairs
    min(N, M) of them; any excess lines are left as plain additions/
    deletions (no entry), since there's nothing sensible to diff them
    against.
    """
    spans: dict[int, list[tuple[int, int]]] = {}
    i = 0
    n = len(lines)
    while i < n:
        if not lines[i].startswith("-") or lines[i].startswith("--- "):
            i += 1
            continue
        removed_start = i
        while i < n and lines[i].startswith("-") and not lines[i].startswith("--- "):
            i += 1
        removed_end = i
        if i >= n or not lines[i].startswith("+") or lines[i].startswith("+++ "):
            continue
        added_start = i
        while i < n and lines[i].startswith("+") and not lines[i].startswith("+++ "):
            i += 1
        added_end = i

        pair_count = min(removed_end - removed_start, added_end - added_start)
        for k in range(pair_count):
            r_idx = removed_start + k
            a_idx = added_start + k
            removed_code = lines[r_idx][1:]
            added_code = lines[a_idx][1:]
            if not _is_pairable_modification(removed_code, added_code):
                continue
            r_spans, a_spans = _word_diff_spans(removed_code, added_code)
            if r_spans:
                spans[r_idx] = r_spans
            if a_spans:
                spans[a_idx] = a_spans
    return spans


def _gutter_width(diff_text: str) -> int:
    """Widest line number that will appear, so both columns align."""
    widest = 3
    for m in _HUNK_HEADER_RE.finditer(diff_text):
        widest = max(widest, len(m.group(1)), len(m.group(2)))
    # Hunks can run for hundreds of lines past their header; pad a bit more
    # generously than the header numbers alone to reduce the odds of a
    # ragged gutter on long hunks.
    return widest + 2


def _gutter_prefix_width(width: int) -> int:
    """Total column width consumed by 'old new│ ' so non-code lines (hunk
    headers, file headers) can pad themselves to the same indent."""
    return 2 * width + 1 + 2  # two numbers + separator + two spaces


def render_diff(
    diff_text: str,
    hint_filename: str | None = None,
    wrap: bool = True,
    theme: str = "ansi_dark",
) -> RenderedDiff:
    """Build a Rich Text with an old/new line-number gutter plus per-line
    syntax + diff coloring, plus hunk-position metadata for navigation.

    hint_filename lets the caller force a lexer (e.g. the selected file's
    name) instead of relying on the `+++ b/...` line inside the diff text,
    which matters for untracked-file pseudo-diffs where every line is `+`.
    wrap controls whether long lines soft-wrap onto the next row (True, the
    default — the app always renders this way so nothing needs horizontal
    scrolling) or extend past the viewport (False, for callers/tests that
    want the old un-wrapped behavior).

    RenderedDiff.hunk_lines is a list of 0-indexed line numbers (into
    RenderedDiff.lines) where each hunk header starts, for 'jump to next/
    previous hunk' navigation. Line-based, not visual-row-based — with
    word-wrap on, a wrapped hunk can land a little further down than this on
    screen, but never before it, so repeated jumps still converge correctly.
    """
    if not diff_text.strip():
        return RenderedDiff(text=Text("(no diff)", style="dim italic"))
    if diff_text.startswith("(") and diff_text.rstrip().endswith(")") and "\n" not in diff_text.strip():
        # skip_reason()'s one-line "(binary file skipped ...)" placeholders.
        return RenderedDiff(text=Text(diff_text, style="dim italic"))

    lexer_name = _lexer_name_for_filename(hint_filename) if hint_filename else "text"
    out = Text()
    hunk_lines: list[int] = []
    lines = diff_text.splitlines()
    width = _gutter_width(diff_text)
    old_no = new_no = 0
    word_spans = _pair_modified_lines(lines)

    def _current_line_no() -> int:
        return out.plain.count("\n")

    for i, line in enumerate(lines):
        newline = "\n" if i < len(lines) - 1 else ""

        prefix_width = _gutter_prefix_width(width)

        if line.startswith(("diff --git", "index ")):
            out.append(" " * prefix_width)
            out.append(line + newline, style=FILE_HEADER_STYLE)
            continue
        if line.startswith(("--- ", "+++ ")):
            out.append(" " * prefix_width)
            if line.startswith("+++ ") and lexer_name == "text":
                candidate = line[4:].strip()
                if candidate.startswith("b/"):
                    candidate = candidate[2:]
                if candidate and candidate != "/dev/null":
                    lexer_name = _lexer_name_for_filename(candidate)
            out.append(line + newline, style=META_STYLE)
            continue
        hunk_match = _HUNK_HEADER_RE.match(line)
        if hunk_match:
            old_no, new_no = int(hunk_match.group(1)), int(hunk_match.group(2))
            # Blank line before each new hunk (not the very first line of
            # the diff) so separate hunks don't visually run together.
            if out.plain:
                out.append("\n")
            hunk_lines.append(_current_line_no())
            out.append(" " * prefix_width)
            out.append(line + newline, style=HUNK_STYLE)
            continue

        if line.startswith("+"):
            prefix, code, bg = "+", line[1:], ADDED_BG
            old_str, new_str = "", str(new_no)
            gutter_style = GUTTER_ADDED_STYLE
            bright_bg = BRIGHT_ADDED_BG
            new_no += 1
        elif line.startswith("-"):
            prefix, code, bg = "-", line[1:], REMOVED_BG
            old_str, new_str = str(old_no), ""
            gutter_style = GUTTER_REMOVED_STYLE
            bright_bg = BRIGHT_REMOVED_BG
            old_no += 1
        else:
            prefix, code, bg = " ", line[1:] if line else "", None
            old_str, new_str = str(old_no), str(new_no)
            gutter_style = GUTTER_STYLE
            bright_bg = None
            old_no += 1
            new_no += 1

        out.append(old_str.rjust(width), style=gutter_style)
        out.append(" ")
        out.append(new_str.rjust(width), style=gutter_style)
        out.append(GUTTER_SEPARATOR, style=GUTTER_SEPARATOR_STYLE)
        out.append(" ")

        code_text = _highlight_line(code, lexer_name, theme)
        if bg is not None:
            code_text.stylize(bg)
            out.append(prefix, style=bg)
        else:
            out.append(prefix)
        # Word-level highlight spans go on top of the whole-line tint above
        # (applied after it, so Style.combine's later-wins semantics let the
        # brighter overlay show through) rather than being swallowed by it.
        for start, end in word_spans.get(i, ()):
            code_text.stylize(bright_bg, start, end)
        out.append_text(code_text)
        out.append(newline)

    out.no_wrap = not wrap
    out.overflow = "fold" if wrap else "ignore"
    return RenderedDiff(text=out, hunk_lines=hunk_lines)
