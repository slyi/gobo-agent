# gobo-agent

A small dev loop for GoboScript that runs your build on the **real Scratch runtime**
— upstream `@scratch/scratch-vm` in a plain browser, the same runtime family as the
scratch.mit.edu player — so an agent (or you) can **build, read live debugger logs,
screenshot the stage, and poke values into a running project**. No Scratch Desktop,
no TurboWarp Desktop, no Electron.

Windows, macOS, and Linux are supported. If you are a coding agent (or want the
shortest path), see [AGENTS.md](AGENTS.md): the fastest warm/cold loop, measured
timings, and the flags that matter.

## What is here

```
gobo-agent/
├── assets/                hello + backdrop SVG costumes
├── main.gs                says "Hello, World!" and logs every 30 frames
├── stage.gs               backdrop
├── goboscript.toml        frame rate and stage size
├── tools/
│   ├── cdp.py             tiny stdlib Chrome DevTools Protocol client (WebSocket)
│   ├── gsdev.py           build / run / screenshot / live-tuning CLI
│   └── scratchhost/       headless scratch-vm host page (AGPL deps installed locally)
└── .vscode/tasks.json     VS Code build tasks (Ctrl+Shift+B runs with live logs)
```

## Prerequisites

| Tool | Why | Get it |
| --- | --- | --- |
| Python 3.10+ | runs the dev loop | python.org |
| [GoboScript](https://github.com/aspizu/goboscript) | compiles `.gs` to `.sb3` | its installer |
| Node + npm | the scratch-vm host page | nodejs.org |
| Chrome or Edge | runs the host page | your browser |

Install the host page's dependencies once (the `@scratch/*` packages are
**AGPL-3.0-only**, so they are installed locally and gitignored, never committed):

```powershell
npm install --prefix tools/scratchhost
```

On Windows, process and port handling uses the standard library's `ctypes`
(Toolhelp32 + `GetExtendedTcpTable`), so **no PowerShell, WMI, taskkill, or
netstat is required**. On macOS/Linux it uses `ps` (plus `pgrep`/`lsof`/`fuser`
where present). VS Code is optional and only needed for the `Ctrl+Shift+B` tasks.

Run the dependency check any time:

```powershell
python tools/gsdev.py doctor
```

It prints one line per dependency (`ok` / `warn` / `FAIL`), lists environment
overrides, and exits non-zero if something required is missing.

**Platform support:** `.github/workflows/macos-smoke.yml` runs
`doctor`/`selftest`/`build`/`run --headless --software`/`screenshot --headless
--software`/`close` on a `macos-14` runner and uploads the stage image, so the
macOS path is checked without owning a Mac. Locally the host uses the **real GPU**
by default; CI forces software GL because runners have no usable GPU.

## Runtime (headless scratch-vm host)

`tools/scratchhost/` is a minimal page that loads upstream `scratch-vm` +
`scratch-render` and exposes the VM over CDP. `gsdev.py` serves it on
`127.0.0.1:8077` (override with `GSDEV_HOST_PORT`), launches Chrome/Edge with a
debug port, loads the `.sb3`, green-flags it, and streams logs.

- **Real GPU by default** so performance is representative; `--software` forces
  SwiftShader on boxes without a GPU (e.g. CI). `GSDEV_SOFTWARE=1` does the same
  for every command without threading `--software` through each launch.
- `GSDEV_BROWSER` overrides the browser executable.
- The host page installs the `procedures_call` hook that captures GoboScript's
  `log`/`warn`/`error` blocks, so `run` streams `[LOG ...]` lines without adding
  anything to the project.

## Command line

```powershell
python tools/gsdev.py run                 # build + start + stream logs
python tools/gsdev.py run --duration 5    # exit after 5 seconds
python tools/gsdev.py run --no-build      # reuse the existing .sb3
python tools/gsdev.py run --cpu 4         # emulate a 4x slower CPU (phone)
python tools/gsdev.py run --headless      # no visible browser window
python tools/gsdev.py run --software      # force software GL (CI / no GPU)
python tools/gsdev.py run --leave-running # keep running for get/set/watch
python tools/gsdev.py run --port 0        # auto-pick a free CDP port
python tools/gsdev.py screenshot --out debug/hello.png --delay 1500
python tools/gsdev.py screenshot --headless --software
python tools/gsdev.py pixel 200 -150      # stage colour at Scratch coords
python tools/gsdev.py build
python tools/gsdev.py status              # sprites and run state
python tools/gsdev.py stop                # stop play, keep the host open
python tools/gsdev.py close               # close the host browser + server
python tools/gsdev.py doctor              # check required tools are installed
python tools/gsdev.py selftest            # check host files and flags
python tools/gsdev.py tasks               # check .vscode tasks resolve here
python tools/gsdev.py tasks --run         # ... and execute the non-launching ones
```

Keeping the host running makes `run` ~1 s to running; a cold start pays browser
launch + VM parse (plus GPU init). Agents pass `--headless`; the VS Code tasks run
the host with a visible window and the real GPU. Set `GSDEV_PROJECT=DIR` to run a
different project with the same tools (the host page stays in `tools/`).

## Live tuning

For fast iteration you can poke values into an already-running project and read
the effect on the next frame, instead of rebuilding and reloading:

```powershell
python tools/gsdev.py run --duration 1 --leave-running   # start and stay running
python tools/gsdev.py get QuadFiller.res QuadFiller.fps  # read variables
python tools/gsdev.py set QuadFiller.res 4               # write, applies next frame
python tools/gsdev.py set_batch main.dotx=0 main.doty=0       # several at once, atomically
python tools/gsdev.py watch QuadFiller.drawcount --duration 2
```

Use **`set_batch`** instead of several `set`s when values must change together: it
applies all the assignments in one `evaluate`, so the VM cannot step between them
and no thread ever sees a half-updated pair (there *is* a race with separate
`set`s). Lists and `list[index]` work too, e.g.
`set_batch QuadFiller.cube_ph[1]=77 QuadFiller.res=2`.

Selectors are `sprite.variable`, `sprite.list[index]` (1-based), or a bare name
for a Stage (global) variable. `set` replaces a whole variable/list, or one list
element when an index is given. Variables and lists are read dynamically, so a
`set` applies on the next frame. Use this for **small value changes**; **big logic
changes still need a full rebuild and reload**, since structural script edits are
not injected into a running project.

Each command attaches to the warm host, which costs about half a second of Python
startup. For sweeps, batch the operations over one connection with `session`
(reads `set`/`get`/`watch`/`sleep` lines from stdin):

```powershell
@"
set QuadFiller.res 2
sleep 400
watch 600 QuadFiller.res QuadFiller.drawcount QuadFiller.rendertime
set QuadFiller.res 8
sleep 400
watch 600 QuadFiller.res QuadFiller.drawcount QuadFiller.rendertime
"@ | python tools/gsdev.py session
```

`watch` prints `[WATCH +Nms] name=value` per sample plus a summary line. This is
data injection, not code injection: values can be poked freely, but structural
script changes still need a reload.

## Input injection (for tests)

Agents can drive the running project like a test would. Injection happens inside
the VM (`postIOData`), so it works headless and is deterministic. Coordinates are
Scratch stage coordinates (0,0 is centre, +x right, +y up):

```powershell
python tools/gsdev.py mouse 0 0            # move
python tools/gsdev.py mouse 0 0 --down     # press and hold
python tools/gsdev.py mouse 0 0 --up       # release
python tools/gsdev.py click 0 0            # press then release
python tools/gsdev.py key space            # press and release
python tools/gsdev.py key space --down     # hold
python tools/gsdev.py key space --up       # release
```

Keys: `space`, `enter`, `up`/`down`/`left`/`right` (or `arrowup` etc.), or any
single character. Batch a whole test over one connection with `session`; `expect`
prints `[ASSERT ok|FAIL]` and exits non-zero if any expectation fails:

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

`expect` supports `==`, `!=`, `>`, `<`, `>=`, `<=` (numeric when both sides parse
as numbers, otherwise string compare). Input injection is VM-level, so it needs
the project running (`run --leave-running`).

Verify colours with `pixel` — Scratch coordinates, read from the rendered stage via
the renderer's snapshot:

```powershell
python tools/gsdev.py pixel 200 -150     # {"rgba":[0,0,0,255],"hex":"#000000","x":200,"y":-150}
```

**Accuracy notes (measured locally):** `scratch-render`'s `PenSkin` offsets pen
widths 1 and 3 by **+0.5 px** for Scratch 2.0 pixel alignment
(`PenSkin.js drawLine`, LLK/scratch-render#314), so a pen-drawn 1-px dot needs
`hide` plus a `−1` y compensation (and a *zero-length* stroke, else it is a 2-px
line); a 1×1 costume instead needs a `+0.5` offset because it is centred on a
pixel corner. Hit testing uses the **costume silhouette, not its bounding box**,
and `mouse.js` rejects pointer positions not **strictly** inside the canvas, so a
click exactly on the edge (±240, ±180) fires nothing. `set_batch` applies several
writes in one `evaluate`, so the VM cannot step between them.

The `examples/` and `tests/` directories are **local-only** (gitignored) fixture
and demo material, not part of the shipped repo. The tracked end-to-end check is
the workspace-root project plus `smoke.txt` (below).

## Deterministic time (tests)

Instead of sleeping, wait for or advance the runtime by frames:

```powershell
python tools/gsdev.py frame                 # current frame counter
python tools/gsdev.py wait_frame 30         # block until 30 more frames
python tools/gsdev.py pause                 # stop the frame interval
python tools/gsdev.py step 60               # advance exactly 60 frames (stays paused)
python tools/gsdev.py resume
python tools/gsdev.py restart               # re-run green-flag scripts (keeps variables)
```

`wait_frame`/`step` are **logical, not wall-clock**: manual stepping does not
advance time, so `wait`/`timer`/`days since 2000` blocks still need real time
(`run`/`watch`). Agents should prefer `wait_frame` over `sleep` for stable tests.
All of these are also `session` verbs (`waitframe N [ms]`, `step N`, `pause`,
`resume`, `restart`, `frame`).

Prefer **events over polling**: the host dispatches `gsdev:frame` (step entry) and
`gsdev:render` (draw complete, framebuffer ready) every frame, and can record
every frame in-page so nothing is sampled or missed:

```powershell
python tools/gsdev.py render                 # {frame, rendered, frameEvents, renderEvents}
python tools/gsdev.py record QuadFiller.fps  # capture on every frame (no polling)
python tools/gsdev.py step 60                # deterministic frames
python tools/gsdev.py trace                  # drain all recorded rows
python tools/gsdev.py until QuadFiller.drawcount ">" 500 --pause
```

`record` captures `[frame, ...values]` for **every** completed frame (rows are
contiguous); when it reaches `--max` it stops and sets `overflow` instead of
silently dropping frames. `until` records the exact frame a condition first holds
and, with `--pause`, freezes the VM on that frame (race-free). `wait_frame` and
the session `waitframe` resolve on the `gsdev:frame` event rather than polling.
In-page fixtures can also `window.addEventListener('gsdev:frame' | 'gsdev:render',
…)` directly, and the example tests use `waitframe` instead of `sleep`.

## Async waits

Wait for a value or a pixel without sleeping. Both resolve from the per-frame
`gsdev:render` hook (**event-driven, no poll**), and a `--timeout` (default
5000 ms) that elapses is a hard failure, so they double as assertions:

```powershell
python tools/gsdev.py wait_until QuadFiller.drawcount ">" 500 --timeout 3000
python tools/gsdev.py wait_pixel 0 0 "#ff0000" --timeout 2000
```

The `session` verbs take the timeout as a positional: `waituntil flag == 2 5000`
and `waitpixel 0 0 #ff0000 2000`.

## Broadcasts and introspection

A broadcast is just `runtime.startHats('event_whenbroadcastreceived', …)` — the
same call Scratch's own `broadcast` block makes — so an agent can fire a message
without any script:

```powershell
python tools/gsdev.py broadcast go                       # start the `on "go"` hats
python tools/gsdev.py broadcast_wait go --timeout 8000   # ...and wait for them
```

`broadcast_wait` is event-driven (it resolves on the first `gsdev:render` where
every thread the hat started has left `runtime.threads`), not a poll. It needs the
runtime running: a paused runtime never steps, so it can only time out (exit 1).
Both are `session` verbs too (`broadcast go`, `broadcast_wait go 8000`).

Targets are addressed by sprite name with an optional `#N` clone suffix
(`main` = original, `main#1` = first clone; indices are only stable **within a
frame**):

```powershell
python tools/gsdev.py inspect [TARGET]    # targets, variables, costumes, extensions
python tools/gsdev.py props main          # a target's properties
python tools/gsdev.py prop main size 50   # write one property
python tools/gsdev.py prop main x         # (omit the value to read)
python tools/gsdev.py clones              # clone count per sprite
```

`prop` writes through the `RenderedTarget` setters (`x`/`y`, `direction`, `size`,
`visible`, `costume` by name or index, `rotationStyle`, `draggable`, and `layer`
`front`/`back`). The `#N` form also works in `get`/`set`/`watch`/`expect` and
`session`.

## Runtime errors

scratch-vm has no error event and does not catch a throwing thread itself. The
host wraps `runtime._step` (capturing the exception and letting the runtime keep
going) and also listens for `window.onerror` / `unhandledrejection`:

```powershell
python tools/gsdev.py errors             # {count, logErrors, errors: [...]}
python tools/gsdev.py expect_no_errors   # exit 1 on any VM/page or error-log error
```

`run` prints a captured exception as `[ERROR vm <time>] ...` (a page error as
`[ERROR page ...]`) and a goboscript `error` block as `[ERROR <sprite> ...]`.
`errors` reports `count` (VM/page) and `logErrors` (error-level logs); the list is
bounded so a thread that throws every frame cannot grow forever. `errors` and
`expect_no_errors` are `session` verbs too; `run --no-reload` reuses the project
already on the page (no build, no reload) to keep watching a live one.

## Test runner

`smoke.txt` in the project root is the basic end-to-end check against the root
project: `run` confirms the `[LOG ...]` stream, `screenshot` confirms rendering,
and the `session` file confirms injected input (`expect main.keys >= 1`), pixel
readback (`expectpixel 0 0 #4c97ff`), and a **live edit** — `set main.tx 120` moves
the sprite with no rebuild, and `expectpixel 120 0 #4c97ff` observes it:

```powershell
python tools/gsdev.py run --headless --leave-running
python tools/gsdev.py session < smoke.txt
python tools/gsdev.py pixel 0 0                 # {"hex":"#4c97ff",...}
python tools/gsdev.py screenshot --headless --out debug/smoke.png
python tools/gsdev.py close
```

`test` runs one or more session files, each against a fresh reload of its project
(inferred from the file's directory when it has a `goboscript.toml`, otherwise the
current `GSDEV_PROJECT`); example: `python tools/gsdev.py test smoke.txt --headless --json`.

`test` collects `[ASSERT]` results and captured VM/page errors, saves a screenshot
to `debug/<name>-fail.png` on failure (redirect with `--artifacts DIR`), prints a
summary, and exits non-zero if any file fails. `--json` prints a single report
`{files:[{path,asserts,failures,errors,screenshot?}], totals:{files,asserts,failures}}`.
`--no-reload` reuses the running project. `session --json` prints its structured
result as well (including `watch` samples; `watch` emits no human rows in JSON
mode). `wait_pixel` reads the pixel straight from the renderer's WebGL back
buffer, so it costs one `readPixels` per frame rather than a full-stage snapshot.

## How log capture works

1. The host page loads upstream `scratch-vm` + `scratch-render` and exposes
   `window.__host` / `window.__gsdevVm` over the Chrome DevTools Protocol.
2. `tools/cdp.py` connects to the page with a hand-written WebSocket (no
   `websockets` package needed).
3. The page hooks the VM's `procedures_call` primitive and captures GoboScript's
   zero-width `log`/`warn`/`error` proccodes into a page queue; Python drains it
   and prints `[LOG ...]`.
4. Screenshots use `Page.captureScreenshot` clipped to the stage canvas rect, so
   only the stage is captured.

## Notes and limits

- **Encoding:** the CLI reads stdin and writes stdio as UTF-8, so non-ASCII
  selectors and values (CJK etc.) round-trip through `set`/`set` lists and through
  piped `session`/`test` files. JSON output escapes non-ASCII as `\uXXXX` (still
  valid JSON that decodes to the original characters).
- **Docs staging:** in-progress `AGENTS.md` edits live in `agents_staging.txt`
  (gitignored) and are not in `AGENTS.md` yet. Apply them before committing:
  `Copy-Item agents_staging.txt AGENTS.md`. See the plan's "Before committing".
- `--cpu RATE` (or `GSDEV_CPU`) emulates a slower CPU by calling CDP
  `Emulation.setCPUThrottlingRate`, e.g. `--cpu 4` for a mid-range phone. It
  throttles the renderer, not the GPU, and Chromium's throttling is approximate.
- `--headless` hides the window but still uses the GPU; `--software` forces
  SwiftShader (software GL) for GPU-less boxes. Use `--software` on CI (e.g.
  `run --headless --software`); locally omit it for representative performance.
- A tight `forever` loop with no `wait` can spin very fast and flood the log
  stream. `main.gs` waits one second per iteration, so it logs about once per
  second. Add `wait 1;` in any loop that logs.
- Change the CDP port with `--port` or `GSDEV_CDP_PORT` if 9230 is taken.
  `--port 0` auto-picks a free port and remembers it in `tools/.gsdev-port`
  (gitignored). The host static server uses `GSDEV_HOST_PORT` (default 8077).
- Parallel A/B runs: give each run its own port (`--port 0`) and its own project
  directory (a worktree or copy) so they get separate profiles and `.sb3` files.
- `close` kills the host browser by port owner and stops the static server; it
  never triggers an app quit dialog because there is no app.
- `log`/`warn`/`error` are debugger blocks, not plain Scratch palette blocks; the
  host intercepts their proccodes, so they work without adding blocks to the
  project.
