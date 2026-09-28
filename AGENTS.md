# Quick iteration guide

`gobo-agent` builds a GoboScript project, runs it on the **vanilla Scratch VM** in a
real browser, and lets you drive, inspect, and profile it — so you can change code and
see what it actually does in a tight loop. No Scratch desktop app, Electron, Node,
Rust, or compiler toolchain required.

- `tools/gsdev.py` — local build/run loop, unit and functional tests, live tuning,
  input injection, and profiling.
- `tools/gsbridge.py` — the same style of verbs against a live Scratch site.

Command reference: [README.md](README.md). On macOS/Linux, `./tools/gsdev` and
`./setup.sh` mirror the PowerShell launchers (same arguments, no admin needed; a
Chromium-based browser is assumed, Safari is out of scope).

## Ground rules

- Run browsers **headless** unless a visible investigation is explicitly requested.
- **Bound** every `run` with `--duration`; add `--leave-running` when later commands
  must observe scripts still running.
- **Keep the host warm** between edits; do not `close` after every command.
- **Rebuild/reload after source changes** (`run` does this). `set`/`set_batch` change
  data only — they never replace code.
- **Relaunch after editing `host.js`**: a warm page keeps the previous JavaScript.
- Never reuse a personal browser profile; never treat an unknown port owner as ours.

## Fast iteration

```powershell
python tools/gsdev.py run --headless --duration 2 --leave-running   # keep the host warm
python tools/gsdev.py get main.tx main.ty
python tools/gsdev.py set_batch main.tx=120 main.ty=0               # one evaluation
python tools/gsdev.py screenshot --headless --out debug/check.png
```

- **Prefer live variable edits for iteration.** Tune values with `set` and read the
  effect on the next frame — no rebuild, reload, or browser restart. Only
  rebuild/reload when the *code* changed (new/edited blocks, costumes, or `host.js`),
  never just to try a new value.
- **Prefer `set_batch` when changing several related values.** It evaluates them in
  one go, so the VM cannot step between the writes; separate `set` calls are separate
  evaluations and the VM can observe a half-updated state (a race).
- `GSDEV_PROJECT=DIR` runs another project with the same host page.
- `--no-build` only when source is unchanged; `run --no-reload` observes the loaded
  project without building or loading. `stop` ends scripts; `close` ends the session
  and its owned server.
- Use explicit ports for independent hosts (CDP default 9230, HTTP 8077). `--port 0`
  remembers its choice. A warm host with the wrong headed/headless mode must be
  closed or moved to another port; inspect conflicts, never kill unrelated listeners.

## Repeatable unit tests

- `test FILE...` builds/reloads each file's project, collects assertions and errors,
  exits non-zero on failure, and saves a failure screenshot. `--artifacts DIR`
  redirects images; `--json` gives a structured report.
- `session --file PATH` runs verbs against an existing host (one command per line).
  Use **UTF-8** files: PowerShell 5.1 pipes are not UTF-8, so use `--file` for CJK.
  Quote `@` selectors in PowerShell.
- Selectors: `sprite.variable`, `sprite.list[index]` (1-based), bare name = Stage
  global; `sprite#N` = clone (indices stable only within a frame).
- Prefer event-driven `wait_frame` / `wait_until` / `wait_pixel` (session spellings
  `waitframe` / `waituntil` / `waitpixel`) to sleeps; condition waits fail on timeout.
- `pause` / `step N` / `resume` control logical steps, not elapsed time: timer and
  `wait` blocks still need real time. The host uses 30-Hz compatibility mode.
- Input is VM-level: coordinates are Scratch coordinates (centre `0,0`, +x right,
  +y up). Hold/release keys across frames for polling scripts.
- Check `errors` / `expect_no_errors`, not just the log stream — captured errors
  include VM/page exceptions and goboscript `error` logs. Yield in logging loops.
- The root project and `smoke.txt` are the portable baseline. `examples/` and
  `tests/` are local fixtures — do not assume they exist in a fresh checkout.

## Inspection and debugging

`inspect`, `props`, `prop`, and `clones` expose target state. `broadcast` starts
hats; `broadcast_wait` waits for their threads (runtime must be running).
`record` / `trace` capture frame-by-frame values and report overflow; `until …
--pause` captures a matching frame. Prefer the `gsdev:frame` / `gsdev:render`
events over repeated CDP polling.

Pixel checks must account for sprite visibility, costume silhouettes, and renderer
pixel alignment: pen widths 1 and 3 have a half-pixel rule, so do not apply a blanket
offset; canvas-edge clicks are not equivalent to clicks strictly inside the stage.

## Live-site checks

```powershell
python tools/gsbridge.py run --target github --project . --headless --duration 6 --leave-running
python tools/gsbridge.py get main.tx "@fps" "@gpu"
python tools/gsbridge.py expect_no_errors
python tools/gsbridge.py targets      # confirms the loaded project replaced the page's own
python tools/gsbridge.py close
```

Targets are `localhost`, `github`, `editor`, `production` (`--project-id N`), and
`url`. `--project DIR` / `--sb3 FILE` load a build; production loads what is already
on the page. The bridge uses fresh isolated profiles (CDP 9400 default) and exposes a
**subset** of gsdev — no `watch`/`record`/`until`/`step`/property tools. Per-draw
profiling is off by default (`--perf on` enables it). Keep one session open only while
needed, then `close`.

`run` waits for the page's own project to load before replacing it, so the session is
never a merge of the two: `targets` should list exactly `Stage` plus your sprites
(re-load, never sprite deletion, if a page applies its project late). Live variable
edits work on `github`/`editor`; key injection can depend on page focus, so verify with
`get` rather than assuming it landed.

## Measuring without fooling yourself

- `profile` counts executed-primitive operations per VM step and attributes them per
  procedure and top-level script (`sum(self) + unattributed == total_ops`). It uses the
  same wrapper as `setlogic on`, so it is intrusive: use it to find hotspots, never as
  a timing verdict. `profile` streams one line per step by default (`--no-follow` for
  the summary), `profiling` reprints the current report, `setprofiling on|off` is a
  silent toggle, `profile --wait-until SEL --wait-op OP --wait-value V` starts the
  window at an event, and `profilereset` opens one manually.
- `@fps` / `@renderfps` are smoothed renderer-call rates, not physical display FPS or
  elapsed time; `@stepfps` is the VM step rate. Pen/stamp times are **CPU
  submission**, not GPU completion. `--software` is for behaviour, not performance.
- Keep conditions equal when comparing runs (browser and version, headed vs headless,
  GPU, stage size, inputs, background load) and discard modal-obscured runs rather than
  trusting them. One machine's browser ranking generalises to nothing.

## Scratch compatibility

Scratch 3 commonly stores bitmaps at **2×** with `bitmap_resolution = 2` (Scratch 2
used `1`); scratch-vm honours it, so do not "fix" a rendering discrepancy by
downsampling assets — check the actual project. TurboWarp-only options such as
`high_quality_pen`, `frame_interpolation`, and custom clone limits must not be assumed
to work on production Scratch.

## Fidelity notes

- Semantics are upstream Scratch's (interpreter, no compiler).
- The real GPU is used by default, including `--headless`; `--software` forces
  ANGLE/SwiftShader (much slower) — trust behaviour there, not performance.
- The host steps at 30-Hz compatibility. The audio engine is not attached, so
  projects that use sound may need `scratch-audio`.

## Cleanup

Close the sessions you own when you are done; leave a host running if something else
still needs it.
