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
            new_no += 1
        elif line.startswith("-"):
            prefix, code, bg = "-", line[1:], REMOVED_BG
            old_str, new_str = str(old_no), ""
            gutter_style = GUTTER_REMOVED_STYLE
            old_no += 1
        else:
            prefix, code, bg = " ", line[1:] if line else "", None
            old_str, new_str = str(old_no), str(new_no)
            gutter_style = GUTTER_STYLE
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
        out.append_text(code_text)
        out.append(newline)

    out.no_wrap = not wrap
    out.overflow = "fold" if wrap else "ignore"
    return RenderedDiff(text=out, hunk_lines=hunk_lines)
