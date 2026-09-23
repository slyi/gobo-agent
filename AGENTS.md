# gobo-agent: agent TLDR (build & test loop)

Fastest way to verify a GoboScript change. All commands are
`python tools/gsdev.py <cmd>` from the project root. Default backend is Scratch;
`GSDEV_BACKEND=turbowarp` switches to TurboWarp (both stream logs).

## TLDR

1. **Keep one editor open for the whole session (warm). Do not close between edits.**
2. **Per change: `run --headless --duration 2`** (~1.1–1.3 s to first log) or
   `screenshot --headless` for visuals.
3. **Cold-start only twice:** once to start the session, once at the end to
   validate from scratch. Cold start is 4–7 s each time — 4–6× slower per edit.

## Dependencies (verify with `doctor`)

- **Python 3.10+** — stdlib only; there is no pip/npm/Node/Bun step.
- **`goboscript`** on `PATH` — the compiler (`build`).
- **One editor**: Scratch Desktop (default) or TurboWarp Desktop
  (`GSDEV_BACKEND=turbowarp`). Override locations with `SCRATCH_EXE` /
  `TURBOWARP_EXE`.
- **Windows needs nothing else** — process/port handling is stdlib `ctypes`
  (no PowerShell/WMI/taskkill/netstat). Linux/macOS uses `ps` (+
  `pgrep`/`lsof`/`fuser`).
- **VS Code is optional** — only for the `Ctrl+Shift+B` tasks; the CLI runs
  without it.
- **Display-less Linux** (CI/WSL): install `libnss3 libnspr4 libasound2`, set
  `ELECTRON_DISABLE_SANDBOX=1` when running from a tarball; the tool already adds
  `--ozone-platform=headless` + software GL.
- **macOS is smoke-tested headless for both backends** on a `macos-14` runner in
  `.github/workflows/macos-smoke.yml`: Scratch Desktop installs as
  `Scratch 3.app`, TurboWarp from its dmg. `--headless` works on macOS.

`python tools/gsdev.py doctor` prints one `ok`/`warn`/`FAIL` line per dependency
and exits non-zero if a required one is missing. Install links are in README.md.

`python tools/gsdev.py selftest` checks the per-platform path and headless-flag
logic for win32/darwin/linux — a local stand-in when you cannot run on macOS.

## Measured (headless, this machine)

| Step | Time |
| --- | --- |
| `build` only (incl. Python startup) | ~0.4 s (compiler itself ~15 ms) |
| Cold: run → first `[LOG]` (editor not running) | TurboWarp ~4.5 s, Scratch ~6.7 s |
| Warm: run → first `[LOG]` (editor already open) | TurboWarp ~1.25 s, Scratch ~1.14 s |
| 10 warm iterations | ~12 s (either backend) |
| 10 cold iterations | Scratch ~67 s, TurboWarp ~50 s |

So a change can be verified in **~1.2 s warm**; budget ~1–2 s per iteration.

## The loop

```powershell
# once per session
python tools/gsdev.py doctor

# start a warm session: launches the editor, then leaves it open.
# Ctrl+C once it starts logging; the editor stays open.
python tools/gsdev.py run --headless
# (or prime it with: python tools/gsdev.py screenshot --headless)

# per change (same default port as the session above):
#   behavior / debugging
python tools/gsdev.py run --headless --duration 2
#   visual check
python tools/gsdev.py screenshot --headless --out debug/check.png
#   phone-speed check
python tools/gsdev.py run --headless --duration 3 --cpu 4

# feature complete: validate from scratch (close, then one cold run)
python tools/gsdev.py close
python tools/gsdev.py run --headless --duration 3
```

Use one port consistently for a session: if you start the editor with
`--port 0`, pass `--port 0` on every later command too (it reads the remembered
port from `tools/.gsdev-port`).

## Reading output

- Log lines: `[LOG HH:MM:SS sprite] message`, plus `[WARN …]` / `[ERROR …]`.
- `project stopped` means all threads ended.
- Screenshots land in `debug/*.png` (stage only, gitignored).

## Options that matter

| Flag | Use |
| --- | --- |
| `--headless` | no window (Windows/macOS hidden; Linux Ozone headless + software GL) |
| `--port 0` | auto free port, remembered in `tools/.gsdev-port`; parallel-safe |
| `--cpu N` | emulate an N× slower CPU (e.g. `4` ≈ a phone) |
| `--duration S` | auto-stop after S seconds — **always bound agent runs** |
| `--no-build` | reuse the existing `.sb3` (only when `.gs` didn't change) |
| `--no-reload` | reuse the current page without re-injecting the build |

## Parallel A/B

- One worktree or copy per variant → its own profile and `.sb3`.
- Give each run `--port 0`. Verified on Windows and display-less Linux: two
  headless editors, two CDP connections, independent logs, and `close --port 0`
  kills only its own instance.

## Don'ts

- Don't `close` between iterations — you pay 4–7 s cold every time.
- Don't run unbounded `run` as an agent; use `--duration` or it hangs.
- Don't share a port between parallel runs (second can't bind, or attaches to
  the first editor and races on one VM).
- Keep a `wait` in any `log` loop, or the stream floods.
- On Linux, use `close` (its `killall`/`pkill -x` equivalents can't match the
  >15-char process name).
