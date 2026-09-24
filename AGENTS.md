# gobo-agent: agent TLDR (build & test loop)

Fastest way to verify a GoboScript change. The runtime is a **headless local
scratch-vm web host** driven by `tools/gsdev.py` — there is no Scratch/TurboWarp
desktop app to install. Everything runs through `tools/gsdev.py`.

- **Agents always run the host `--headless`.**
- **VS Code tasks run the host non-headless** (visible window, real GPU) for humans.

## TLDR

1. **Keep the host open for the whole session (warm). Do not `close` between
   edits.** Cold start is a **once-per-session** cost: start it once, then both
   live value edits *and* logic rebuilds reuse the same warm host, so you never
   pay cold start again during a vibe-coding session.
2. **Per change (agent):** `python tools/gsdev.py run --headless --duration 2`
   (~1 s to running warm) or `screenshot --headless` for visuals.
3. **VS Code tasks (human):** Run/Screenshot/Stop/Close use the host without
   `--headless`; Build and Check dependencies use `gsdev.py`.
4. **Cold-start only twice:** once to start the session, once at the end to
   validate from scratch.

## Dependencies (verify with `doctor`)

- **Python 3.10+** — stdlib only; no pip step.
- **`goboscript`** on `PATH` — the compiler (`build`).
- **Node/npm + Chrome or Edge** — for the scratch-vm host. One-time:
  `npm install --prefix tools/scratchhost` (the `@scratch/*` packages are
  AGPL-3.0-only and gitignored). `GSDEV_BROWSER` overrides the browser;
  `GSDEV_SOFTWARE=1` forces software GL (SwiftShader) for every command.
- No Scratch Desktop or TurboWarp Desktop; no Electron.

`python tools/gsdev.py doctor` prints one `ok`/`warn`/`FAIL` line per dependency.

`python tools/gsdev.py selftest` checks the per-platform browser path/flag logic.

`python tools/gsdev.py tasks --run` checks the `.vscode` tasks resolve and runs the
non-launching ones.

## Measured (headless, this machine)

| Step | Time |
| --- | --- |
| `build` only (incl. Python startup) | ~0.4 s (compiler itself ~15 ms) |
| Cold: host launch → running | ~3–5 s (browser + 5.8 MB VM parse) |
| Warm: `run` → running | ~1 s |
| Warm: `run` → first `[LOG]` | ~1–2 s |

## The loop

```powershell
# once per session: start the host and leave it running
python tools/gsdev.py run --headless --leave-running

# per change (same host, no cold start):
python tools/gsdev.py run --headless --duration 2   # behavior + logs
python tools/gsdev.py screenshot --headless --out debug/check.png
python tools/gsdev.py run --headless --duration 3 --cpu 4   # phone-speed
```

`--headless` is for agents. Omitting it (as the VS Code tasks do) opens a visible
browser window using the real GPU.

## Live tuning (fast iteration)

Poke values into an already-running project and read the effect on the next frame
— no rebuild, no reload. This is data injection (variables/lists), not code.

```powershell
python tools/gsdev.py run --headless --duration 1 --leave-running
python tools/gsdev.py get QuadFiller.res
python tools/gsdev.py set QuadFiller.res 4
python tools/gsdev.py set_batch main.dotx=0 main.doty=0   # several at once, atomically
python tools/gsdev.py watch QuadFiller.drawcount --duration 2
```

`set_batch` applies all assignments in **one evaluate**, so the VM cannot step between
them — use it instead of several `set`s whenever values must change together (there
is a race with separate `set`s). Lists and `list[index]` work too.

Each command pays ~0.5 s of Python startup, so batch a sweep over one attach:

```powershell
@"
set QuadFiller.res 2
sleep 400
watch 600 QuadFiller.drawcount QuadFiller.rendertime
set QuadFiller.res 8
sleep 400
watch 600 QuadFiller.drawcount QuadFiller.rendertime
"@ | python tools/gsdev.py session
```

Selectors are `sprite.variable`, `sprite.list[index]` (1-based), or a bare name
for a Stage (global) variable. `set` replaces a whole variable/list, or a single
element with an index. `watch` prints `[WATCH +Nms] name=value` samples plus a
summary line. stdio is UTF-8, so non-ASCII (CJK) selectors/values round-trip
through `set`/lists and piped `session`/`test` files (JSON escapes them as
`\uXXXX`, which decodes to the original text).

Policy: use live edits for **small value/list changes**; **big logic changes
require a full rebuild + reload** (structural script edits are not injected).
Both reuse the same warm host — only the session's first start is cold.

## Performance runs

The **real GPU is used by default** so perf is representative; `--software` forces
SwiftShader on GPU-less headless boxes, and `--cpu N` emulates an Nx slower CPU
(e.g. `--cpu 4` ~ a phone; measured fps decays 30 → 13 with rendertime ~180 ms).

## Input injection & tests

Drive the running project like a test. Injection is VM-level (`postIOData`), so it
is deterministic and works headless. Coordinates are Scratch stage coordinates
(0,0 centre, +x right, +y up).

```powershell
python tools/gsdev.py mouse 0 0 --down   # press and hold
python tools/gsdev.py mouse 0 0 --up
python tools/gsdev.py click 0 0
python tools/gsdev.py key space          # press and release
python tools/gsdev.py key space --down   # hold
```

Batch a test over one connection with `session`; `expect` prints
`[ASSERT ok|FAIL]` and the command exits non-zero if any expectation fails, so a
session file doubles as a unit test:

```powershell
@"
expect QuadFiller.res == 2
click 0 0
key space press
set QuadFiller.res 4
sleep 200
expect QuadFiller.res == 4
"@ | python tools/gsdev.py session
```

`expect` operators: `==`, `!=`, `>`, `<`, `>=`, `<=` (numeric when both sides
parse as numbers, otherwise string). Needs the project running (`run --leave-running`).
`set_batch a=1 b=2` writes several variables in **one** evaluate, so the VM cannot step
between them (use it instead of two `set`s when a pair must change together).

Colour checks: `pixel X Y` prints the stage colour at Scratch coords
(`{"hex":"#000000","rgba":[...]}`); in `session`, `expectpixel X Y #rrggbb` asserts
it. Accuracy is **exact (0 px error)**. Known pen offset: `scratch-render`'s
`PenSkin` adds **+0.5 px** to pen widths **1 and 3** (Scratch 2.0 pixel-alignment;
`PenSkin.js drawLine`), so a pen 1-px dot needs `hide` + a `-1` y compensation and
a *zero-length* stroke (`pen_down; pen_up`), else it draws a 2-px line. A 1x1
costume instead needs `+0.5` (it is centred on a pixel corner).

Click hats: an `onclick` on both the stage and the sprite confirms the sprite hat
fires on the button, the stage hat outside it (the stage is the fallback when
nothing is hit), and `mouse_x()`/`mouse_y()` match the injected coords. Hit tests
use the costume **silhouette**, not the bounding box. Clicks exactly on the canvas
edge (±240, ±180) fire nothing — `mouse.js` requires the pixel to be strictly
inside.

`examples/` and `tests/` are **gitignored, local-only** fixture/demo material; the
tracked end-to-end check is the root project plus `smoke.txt` (below). `GSDEV_PROJECT=DIR`
runs another project with the same tools.

## Deterministic time

Prefer frames over sleeps: `frame` (counter), `wait_frame N [--timeout ms]`,
`step N` (pauses and advances exactly N), `pause`, `resume`, `restart` (green flag
again, keeps variables). Session verbs: `waitframe N [ms]`, `step N`, `pause`,
`resume`, `restart`, `frame`. Manual stepping does not advance wall-clock, so
`wait`/timer blocks need real time (`run`/`watch`).

Prefer **events over polling**: the host dispatches `gsdev:frame` (step entry) and
`gsdev:render` (draw complete) every frame. `record SELECTOR...` captures
`[frame, ...values]` for every frame (contiguous; `--max` stops with an explicit
`overflow` instead of dropping); `trace [--clear]` drains the rows; `until SELECTOR
OP VALUE --pause` records the exact hit frame and optionally freezes there;
`render` prints `{frame, rendered, frameEvents, renderEvents}`. `wait_frame` /
`waitframe` resolve on the frame event (no polling), and the example tests use
`waitframe` instead of `sleep`. In-page code can
`window.addEventListener('gsdev:frame' | 'gsdev:render', …)`. Local-only unit
tests (all headless): `python tests/test_time.py`, `tests/test_events.py`.

`wait_until SELECTOR OP VALUE [--timeout ms]` and `wait_pixel X Y #rrggbb
[--timeout ms]` resolve from `gsdev:render` (event-driven, no poll); a timeout
exits 1. `wait_pixel` samples the pixel with one `gl.readPixels` per frame (not a
full-stage snapshot). Session verbs: `waituntil sel op value [ms]`,
`waitpixel x y #hex [ms]`. In `session --json`, `watch` returns its samples in the
JSON result instead of printing `[WATCH]` rows.

## Broadcasts, introspection & errors

A broadcast is `runtime.startHats` — fire a message without a script:
`broadcast NAME` starts the `on "NAME"` hats; `broadcast_wait NAME [ms]` fires and
waits (event-driven, on `gsdev:render`) until every started thread finishes (needs
the runtime running; a timeout exits 1). Session verbs: `broadcast NAME`,
`broadcast_wait NAME [ms]`.

Targets are named with an optional clone suffix: `main`, `main#1` (clone #1;
indices are stable only within a frame). `inspect [TARGET]` dumps targets,
variables, costumes, sounds, extensions; `props TARGET` prints properties;
`prop TARGET NAME [VALUE]` reads/writes one (`x`,`y`,`direction`,`size`,`visible`,
`costume`,`rotationStyle`,`draggable`,`layer front|back`); `clones` counts clones
per sprite. The `#N` form works in `get`/`set`/`watch`/`expect` too.

Errors: scratch-vm fires no error event, so the host wraps `_step` and listens for
`window.onerror`/`unhandledrejection`. `errors` dumps `{count, logErrors, errors}`;
`expect_no_errors` exits 1 if either is non-zero. `run` prints `[ERROR vm …]` for a
thrown thread and `[ERROR <sprite> …]` for a goboscript `error` block.
`run --no-reload` reuses the project already on the page (no build, no reload) to
keep watching a live project. (Local-only fixtures/tests that exercise these live
under the gitignored `examples/` and `tests/`.)

## Test runner

`python tools/gsdev.py test FILE... [--headless] [--json]` runs session files,
reloading the project before each (the project is inferred from the file's
directory when it has `goboscript.toml`, else `GSDEV_PROJECT` is used), collects
asserts + errors, saves a screenshot to `debug/` on failure, prints a summary, and
exits non-zero on failure. `--json` prints one report
`{files:[{path,asserts,failures,errors,screenshot?}], totals:{files,asserts,failures}}`.
`--artifacts DIR` redirects the screenshots. The tracked end-to-end check is the
root project + `smoke.txt`: `run --leave-running` (logs), `session < smoke.txt`
(injected input, pixel readback, and a live `set` observed via `expectpixel`),
`pixel X Y` (CLI read), and `screenshot` (rendering). `tests/test_runner.py` is
local-only.

## Reading output

- Log lines: `[LOG HH:MM:SS sprite] message`, plus `[WARN …]` / `[ERROR …]`.
- `project stopped` means all threads ended.
- Screenshots land in `debug/*.png` (stage only, gitignored).

## Options that matter

| Flag | Use |
| --- | --- |
| `--headless` | no browser window; **agents always pass this** |
| `--duration S` | auto-stop after S seconds — **always bound agent runs** |
| `--leave-running` | keep the project running for `get`/`set`/`watch` |
| `--cpu N` | emulate an N× slower CPU (e.g. `4` ≈ a phone) |
| `--software` | force software GL on headless boxes without a GPU |
| `--no-build` | reuse the existing `.sb3` (only when `.gs` didn't change) |
| `--no-reload` | `run`: reuse the project already on the page (no build/reload) |
| `--json` | `session`/`test`: print one JSON object instead of human text |
| `--artifacts DIR` | `test`: where to write failure screenshots (default `debug/`) |
| `--port 0` | auto free port; parallel-safe |

## Don'ts

- Don't `close` between iterations — you pay the cold start again.
- Don't run unbounded `run` as an agent; use `--duration` or it hangs.
- Don't run agents non-headless; the visible window is for VS Code tasks only.
- Keep a `wait` in any `log` loop, or the stream floods.
