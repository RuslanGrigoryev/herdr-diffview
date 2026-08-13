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

from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

ADDED_BG = Style(bgcolor="#123a12")
REMOVED_BG = Style(bgcolor="#3a1212")
HUNK_STYLE = Style(color="cyan", bold=True)
FILE_HEADER_STYLE = Style(color="bright_white", bold=True)
META_STYLE = Style(color="grey50")
GUTTER_STYLE = Style(color="grey42")
GUTTER_ADDED_STYLE = Style(color="green")
GUTTER_REMOVED_STYLE = Style(color="red")

_THEME = Syntax.get_theme("ansi_dark")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _lexer_name_for_filename(filename: str) -> str:
    try:
        return get_lexer_for_filename(filename).aliases[0]
    except (ClassNotFound, IndexError):
        return "text"


def _highlight_line(code: str, lexer_name: str) -> Text:
    if not code or lexer_name == "text":
        return Text(code)
    try:
        lexer = get_lexer_by_name(lexer_name, stripnl=False)
    except ClassNotFound:
        return Text(code)
    text = Text()
    try:
        for token_type, value in lexer.get_tokens(code):
            if value == "":
                continue
            style = _THEME.get_style_for_token(token_type)
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


def render_diff(
    diff_text: str,
    hint_filename: str | None = None,
    wrap: bool = False,
) -> Text:
    """Build a Rich Text with an old/new line-number gutter plus per-line
    syntax + diff coloring.

    hint_filename lets the caller force a lexer (e.g. the selected file's
    name) instead of relying on the `+++ b/...` line inside the diff text,
    which matters for untracked-file pseudo-diffs where every line is `+`.
    wrap controls whether long lines soft-wrap (True) or extend past the
    viewport for horizontal scrolling (False, the default — matches how a
    terminal diff normally reads).
    """
    if not diff_text.strip():
        return Text("(no diff)", style="dim italic")
    if diff_text.startswith("(") and diff_text.rstrip().endswith(")") and "\n" not in diff_text.strip():
        # skip_reason()'s one-line "(binary file skipped ...)" placeholders.
        return Text(diff_text, style="dim italic")

    lexer_name = _lexer_name_for_filename(hint_filename) if hint_filename else "text"
    out = Text()
    lines = diff_text.splitlines()
    width = _gutter_width(diff_text)
    old_no = new_no = 0

    for i, line in enumerate(lines):
        newline = "\n" if i < len(lines) - 1 else ""

        if line.startswith(("diff --git", "index ")):
            out.append(" " * (2 * width + 2))
            out.append(line + newline, style=FILE_HEADER_STYLE)
            continue
        if line.startswith(("--- ", "+++ ")):
            out.append(" " * (2 * width + 2))
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
            out.append(" " * (2 * width + 2))
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
        out.append(" ")

        code_text = _highlight_line(code, lexer_name)
        if bg is not None:
            code_text.stylize(bg)
            out.append(prefix, style=bg)
        else:
            out.append(prefix)
        out.append_text(code_text)
        out.append(newline)

    out.no_wrap = not wrap
    out.overflow = "fold" if wrap else "ignore"
    return out
