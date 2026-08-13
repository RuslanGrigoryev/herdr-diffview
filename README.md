# herdr-diffview

A live diff viewer for [Herdr](https://herdr.dev) — run it in a pane next to a
coding agent (Claude Code, Codex, ...) and watch the working tree change in
real time: a file tree with dirty markers on the left, a syntax-highlighted
unified diff on the right, and the agent's live Herdr status (`working` /
`idle` / `blocked`) in the header.

It does **not** guess anything about agent state — it asks the running Herdr
server for it, over the same CLI/socket API Herdr itself uses.

## Requirements

- [Herdr](https://herdr.dev) installed and running (you're inside a Herdr pane).
- Python 3.10+
- `git` on PATH
- The target directory is a git repository (tracked or not, doesn't matter —
  new/untracked files show up too).

## Install

```bash
git clone https://github.com/RuslanGrigoryev/herdr-diffview.git
cd herdr-diffview
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Use

1. Start your agent in a Herdr pane as usual:

   ```bash
   cd ~/projects/app
   claude
   ```

2. Split a pane next to it (`ctrl+b` then `v`, or `herdr pane split --current
   --direction right --no-focus` from a script) and run:

   ```bash
   herdr-diffview
   ```

   No arguments needed. It reads `HERDR_ENV` / `HERDR_WORKSPACE_ID` /
   `HERDR_PANE_ID` from its own environment (Herdr injects these into every
   pane it manages), asks Herdr for the other panes in the same workspace via
   `herdr pane list --json`, and picks the most recently active pane that has
   an `agent_status` — i.e. the one running your agent. If you're watching a
   plain terminal (no detected agent), pass `--pane <id>` or `--path <dir>`
   explicitly.

### Options

```
herdr-diffview                 # auto-detect the agent pane in this workspace
herdr-diffview --pane w1:p2    # watch a specific Herdr pane's cwd
herdr-diffview --path ~/repo   # watch an explicit directory, skip Herdr lookup
```

### Keybindings

| Key       | Action                                         |
|-----------|-------------------------------------------------|
| `j` / `k` | move file selection down / up (also mouse-clickable) |
| `v`       | switch the file panel between a flat list and a directory tree |
| `space` / `enter` | (tree view) expand/collapse a directory        |
| `a`       | toggle all-files diff vs single file (applies to whichever base — working tree, or the branch base when `b` is on) |
| `b`       | toggle diffing against the repo's default branch instead of the working tree (auto-detected: origin/HEAD, then `main`, then `master`) — the file list shows every file touched since diverging plus uncommitted changes; select one to see its highlighted diff, or press `a` for the whole-branch view (like a PR preview) |
| `f`       | toggle "follow latest changed file" (on by default; clicking/navigating turns it off until pressed again). The auto-followed file is marked with `>` in the file list/tree so it's visually distinct from a manual selection. |
| `t`       | cycle syntax theme (monokai, dracula, github-dark, one-dark, nord, gruvbox-dark, night-owl, solarized-dark, zenburn, material, inkpot, paraiso-dark, stata-dark, plus the ANSI-safe default) |
| `n` / `N` | jump to next / previous hunk in the current diff |
| `/`       | open a search box; type and press Enter to jump to the next matching line, Enter again for the next match, Esc to close |
| `+` / `-` | grow / shrink the file list/tree panel (15–70%)  |
| `PageUp`/`PageDown`/`Home`/`End`, mouse wheel | scroll the diff pane |
| `r`       | force refresh                                   |
| `q`       | quit                                            |

Your theme, follow-on-by-default, list/tree view, and panel-size choices
persist across launches in `~/.config/herdr-diffview/config.json`
(`$XDG_CONFIG_HOME/herdr-diffview/config.json` if set). Delete that file (or
just ignore it) to go back to the defaults.

The header also shows a running `<N> files +<adds> -<dels>` diffstat for the
full working tree, independent of which file is selected. The same figure is
pushed onto the watched Herdr pane as a `$diff` metadata token (best-effort,
silently skipped if unsupported), so herdr's own sidebar can render it even
when this pane isn't open or focused.

The diff pane shows an old/new line-number gutter (like GitHub's side-by-side
numbers, condensed into unified-diff form) alongside the syntax highlighting.
Binary files and anything over ~1.5MB are skipped with a one-line summary
instead of being rendered — no megabytes of binary noise or giant lockfiles
flooding the pane.

## How it works

- **Change detection**: a `watchdog` (inotify) observer on the target
  directory triggers a debounced (~150ms) re-read of `git status --porcelain`
  and `git diff` (+ `git diff --cached`, untracked files are diffed against
  `/dev/null`).
- **Agent status**: if launched inside Herdr, it subscribes to
  `pane.agent_status_changed` over Herdr's local socket for the target pane,
  so the header badge (`working` / `idle` / `blocked` / `done`) updates the
  instant Herdr's own detection changes — no polling.
- **UI**: [Textual](https://textual.textualize.io/), diff highlighting via
  `rich`'s built-in `Syntax`/diff lexer.

If the watched pane closes or its directory disappears, the UI shows a clear
"watching ended" banner instead of crashing.

## Not (yet) covered

- Multiple simultaneous agent panes / a session switcher (deliberately out of
  scope for v1 — see the project issues if you want this).
- Windows (Herdr itself is beta there; this tool is untested).
