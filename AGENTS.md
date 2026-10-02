# Agent workflow

`gobo-agent` builds a GoboScript project and runs it on the **vanilla Scratch VM** in a
real browser. It provides input injection, live state inspection/tuning, session
assertions with failure bundles, and a **built-in procedure profiler**. Use these to
reproduce, understand, edit and verify a project in a warm browser.

- `tools/gsdev.py` — local build/run loop, tests, live tuning, input injection, profiling.
- `tools/gsbridge.py` — the same style of verbs against a live Scratch site.

Command reference: [README.md](README.md). On macOS/Linux, `./tools/gsdev` and
`./setup.sh` mirror the PowerShell launchers. Putting gobo-agent into an existing
project (rather than using this repo as the template)? See the
[integration guide](docs/integration.md) — `gsdev init` adopts a project into a single
`gobo-agent/` folder.

## Default working loop

**Start one host and reuse it for the whole task.** Batch validation with
`session --file`; rebuild/reload in the same browser after source edits; use the
profiler the harness already provides instead of writing counters.

| OS | Setup | Commands |
| --- | --- | --- |
| Windows PowerShell | `setup.ps1` | `tools\gsdev.ps1 ...` |
| macOS/Linux | `./setup.sh` | `./tools/gsdev ...` |

Windows resolves/downloads portable Python when needed; POSIX launchers require a
Python 3.10+ (configured, portable, or on `PATH`). Prefer the launchers over assuming
`python` is on `PATH`. Examples below use the Windows launcher (`tools\gsdev.ps1`); on
macOS/Linux substitute `./tools/gsdev`, or use `python tools/gsdev.py` on any OS.

```powershell
# Start once; keep project + browser for later commands.
tools\gsdev.ps1 run --headless --duration 2 --leave-running
tools\gsdev.ps1 inspect main
tools\gsdev.ps1 session --file checks.txt --bundle
# Edit .gs, then rebuild/reload in that same host.
tools\gsdev.ps1 run --headless --duration 2 --leave-running
tools\gsdev.ps1 session --file checks.txt --bundle
```

Default agent work is **headless** unless a visible run is requested. Bound every `run`
with `--duration` and add `--leave-running` when later commands must observe running
scripts. The user's VS Code run task (Ctrl+Shift+B) opens a **visible** host: match that
mode, or `close` it first rather than starting a second browser.

## Decide which operation you need

| Task | Action |
| --- | --- |
| Tune live values | `set` / `set_batch` — no rebuild or reload |
| Validate current state | `get`, `inspect`, or `session --file` on the standing host |
| Apply changed source/assets | `run --headless --duration 2 --leave-running` |
| Observe without replacing the project | add `--no-reload` to `run` |
| Repeatable test from reloaded state | `test FILE --headless --json` |
| Find expensive procedures, keep execution | `profile --no-reload --no-restart --seconds 2 --no-follow --leave-running`, then `setprofiling off` |
| Capture a freshly loaded image | `screenshot --no-build` (skips compile but reloads/stops the project) |
| End project execution | `stop` — host stays available |
| Finish an owned session | `close`, once work using it is done |

`--no-build` is valid only when source is unchanged; it still reloads. `--no-reload`
preserves loaded code/state. Live data edits never replace code. Cold starts are
justified for initial setup, a deliberate browser-mode change, or a lifecycle test;
otherwise use warm iteration. Keep independent hosts on explicit ports;
`--port 0` remembers its choice. A warm host in the wrong headed/headless mode must be
closed or moved; inspect conflicts, never kill unrelated listeners.

## Debug and release builds

`build` / `run` / `screenshot` / `test` / `profile` take `--mode debug|release` (default
`debug`; `GSDEV_MODE` overrides). A project opts in with `%include tools/gsdev_mode` and
calls the harness macros as statements — no trailing `;` (the macro owns it):

```text
DBG_LOG("...")   DBG_SAY("...")   DBG_ADD(target, n)   DBG_SET(target, v)
```

The project ships a checked-in `tools/gsdev_mode.gs` (debug by default); gobo-agent
rewrites it for the requested mode, builds, and restores it, so the working tree is
unchanged. Release expands the macros to nothing, erasing the call **and its arguments**
at compile time. Switching modes needs a rebuild. Keep safety checks and real state
changes in release; only diagnostic code belongs in these macros.

## Ground rules

- Run browsers **headless** unless a visible investigation is requested.
- **Bound** every `run`; **keep the host warm** between edits (do not `close` each time).
- **Rebuild/reload after code changes**; `set`/`set_batch` change data only.
- Never reuse a personal browser profile; never treat an unknown port owner as ours.

## Fast iteration

```powershell
tools\gsdev.ps1 run --headless --duration 2 --leave-running   # keep the host warm
tools\gsdev.ps1 get main.tx main.ty
tools\gsdev.ps1 set_batch main.tx=120 main.ty=0               # one evaluation
tools\gsdev.ps1 screenshot --headless --out debug/check.png
```

Prefer live variable edits for iteration: tune with `set`, read the next frame — no
rebuild/reload/restart. Prefer `set_batch` when changing several related values so the
VM cannot step between writes (it is not a rollback transaction if a write fails).
`GSDEV_PROJECT=DIR` runs another project with the same host page.

## Profiling: find hotspots before guessing

**Use the built-in `profile` command before writing custom counters or timing hooks.**
For the scene already loaded in a warm host:

```powershell
tools\gsdev.ps1 profile --headless --no-reload --no-restart --seconds 2 --no-follow --leave-running --json debug/profile.json
tools\gsdev.ps1 setprofiling off
tools\gsdev.ps1 profile --headless --no-reload --no-restart --seconds 2 --sort total --children _3dEngine --no-follow --leave-running
tools\gsdev.ps1 setprofiling off
# open the window once the scene is ready (excludes load/setup work):
tools\gsdev.ps1 profile --headless --seconds 2 --wait-until Tick.loaded --wait-op == --wait-value 1 --no-follow
```

Omit `--headless` for a visible host. A plain `profile` builds/reloads and restarts the
project (choose the preservation flags deliberately); without `--leave-running` it
disables profiling and stops the project after capture, and with it profiling stays
enabled until `setprofiling off`. Gate capture at an event with
`--wait-until SEL --wait-op OP --wait-value V` (e.g. wait for `Tick.loaded == 1`), and
bound it with `--wait-timeout` (default 60 s). Reread the
current report with `profiling`; manual capture uses `setprofiling on`, `profilereset`,
then `setprofiling off`.

The summary line reports `steps`, `Step ms` (instrumented, inflated ~2x), `Draws/step`
(pen line/point **and stamp** submissions, counted only while profiling),
`Blocks/step` (project-wide mean), `total`, `self`, `unattributed` — and
`sum(self) + unattributed == total`. The table lists one row per `sprite :: procedure`:
`total%` (whole subtree), `self%` (own body), `kids%` (callees, `total% = self% +
kids%`), `ctx`, `blocks/step` (the row's **total** subtree per step), `calls`,
`calls/step`. Rows are selected by `--sort` (`self`, the default) and printed in
`total%` order. `top-level:` footer rows are each sprite's `event_*` scripts, shown at
≥ `--hats-min` percent (default 2). Clones aggregate by original sprite.

Interpret carefully:

- Blocks are executed primitive dispatches, not editor blocks; totals overlap across procedures.
- `ctx=ui` is screen-refresh scheduling (cut at the step's work budget); `ctx=all` is
  without-screen-refresh (warp). These are not separate OS threads, and warp can yield.
- Profiling adds substantial overhead and can alter pacing/yields; use it to locate
  work and compare **shares**, never as a timing verdict. Verify elapsed-time
  improvements with profiling off.
- Draw counts are pen submissions (not proof of GPU saturation); Step ms may include
  synchronous rendering and does not measure GPU completion.
- Read `screen-refresh budget` warnings: scheduler-limited `blocks/step` understates backlog.

Plain runs already expose basic `@fps`, `@renderfps`, `@stepfps`, `@rendertime`,
`@steptime`, `@frame`, `@rendered`, `@gpu` (and `@software`) — profiling is unnecessary
just to obtain them. `@fps`/`@renderfps` are renderer-call rates, not physical display
FPS; `@stepfps` is the VM step rate.

## Reviewing the profile JSON

The stdout table is a top-N summary. `--json PATH` also writes the **full** report
(gsdev and gsbridge), which is what to query for anything beyond the printed rows:

```powershell
tools\gsdev.ps1 profile --seconds 2 --no-follow --json debug/profile.json
```

Top-level fields:

- `schema`, `instrumented`, `units`, `aggregated`, `enabled` — format/version metadata.
- `steps`, `step_boundaries`, `total_ops`, `self_total`, `unattributed`, `unknown` —
  the invariant is `self_total + unattributed == total_ops`.
- `ops_per_step_mean`/`_max`, `per_step[]`, `per_step_capped`,
  `step_series[]` (`{frame, t, step_ms, draws, blocks}`) — per-step blocks/series.
- `draws_per_step`, `rendered_frames`, `budget_steps`, `work_time_ms` — pen draw counts
  and the screen-refresh budget.
- `time_ms_total`, `time_samples`, `ui_time_ms`, `warp_time_ms`, `unattributed_time_ms`,
  `sample_every` — sampled time shares (instrumented; compare shares only).
- `procedures[]` — the full list; each row has `key` (`sprite :: procedure`), `self`,
  `inclusive`, `share`, `calls`, `context`, `ui_ops`/`warp_ops`,
  `held_steps`/`budget_pct`, `time_ms`/`time_share`, and `dominant` (top opcodes).
- `edges[]` — call graph (`caller`, `callee`, `calls`, `inclusive`).
- `frames[]` (+ `frames_truncated`) and `ids{}` — the bounded per-frame per-procedure
  matrix and the id→key table.
- `sources` — `.gs` mapping (gsdev only): `files[]` with `lines`/`sha256`, plus
  per-procedure `file`/`line`/`resolved`/`search` when a `proc` matched. The bridge
  report has no `sources` (no local project files).

Query it with Python (no extra deps) or `jq`:

```sh
python -c "import json;r=json.load(open('debug/profile.json'));print(sorted(((p['inclusive'],p['key']) for p in r['procedures']),reverse=True)[:10])"
jq -r '.procedures[] | "\(.inclusive)\t\(.key)"' debug/profile.json | sort -rn | head
```

`--sort`, `--top`, `--children` and `--hats-min` only change the printed view; the JSON
always carries the full set. State the capture window and whether profiling was on when
reporting numbers.

## Session validation and failure evidence

Prefer one UTF-8 session file of input, event-driven waits and assertions over many
shell calls. `session` uses one connection against loaded state; `test` builds/reloads
each test's project. Both report failure through exit status.

```text
key space down
waitframe 4
key space up
capture main.keys main.tx
expect main.keys >= 1
expect_no_errors
```

Replace selectors with real project names. `capture SELECTOR...` reads the listed state
together with the next `expect`, in one in-page evaluation, so a failed assertion
carries assertion-time state. `session --bundle` writes failure evidence under
`--artifacts` (manifest, failures with captured state, events, reproduction, optional
stage image); `test --artifacts DIR --json` reports bundle paths. Screenshots are later
observations. `session --events PATH` records structured assertions as JSONL. Inspect
the bundle before improvising another run.

`test --manifest PATH` runs the tests declared in a versioned `gobo-tests.json`
(schema 1): explicit project roots and session files, with `buildMode` reserved for
debug/release builds. `--list` validates and lists them read-only (schema, duplicate
ids, missing files, and root/session path escapes). The shipped manifest runs the root
`smoke.txt`; the VS Code `Run Tests` task wraps this command in the standing host's
browser mode — keep fixtures local (root project + `smoke.txt`), not checked in.

An optional maintainer extension (`vscode-extension/`) surfaces the manifest in the
Test Explorer (discover via `--list`, run via `--json`, Headless and Visible profiles);
execution stays in the harness. It is not required by end users.

## Selectors, waits and input

Selectors are `sprite.variable`, `sprite.list[index]` (1-based), a bare name (Stage
global), or `sprite#N` clones (indices stable only within a frame). Discover names with
`inspect`/`props`/`clones`. A leading `@` selects a harness metric, not a project
variable. Quote `@` selectors in PowerShell.

Prefer event-driven `wait_frame` / `wait_until` / `wait_pixel` (session spellings
`waitframe` / `waituntil` / `waitpixel`) to sleeps; condition waits fail on timeout.
`pause` / `step N` / `resume` control logical steps, not elapsed time — timer and `wait`
blocks still need real time. Input is VM-level: coordinates are Scratch's (centre `0,0`,
+x right, +y up). Hold/release keys across frames for polling scripts; it does not
operate editor menus or native fields. `broadcast` starts hats; `broadcast_wait` waits
for their threads (runtime must be running). Check `errors`/`expect_no_errors`, not just
the log stream. `--software` is for behaviour, not performance.

## Inspection and debugging

`inspect`, `props`, `prop`, `clones` expose target state. `record`/`trace` capture
frame-by-frame values and report overflow; `until … --pause` captures a matching frame.
Prefer the `gsdev:frame`/`gsdev:render` events over repeated CDP polling. Pixel checks
must account for sprite visibility, costume silhouettes and renderer alignment: pen
widths 1 and 3 have a half-pixel rule, and canvas-edge clicks may lie outside the stage.

## Live-site checks

```powershell
python tools/gsbridge.py run --target github --project . --headless --duration 6 --leave-running
python tools/gsbridge.py get main.tx "@fps" "@gpu"
python tools/gsbridge.py expect_no_errors
python tools/gsbridge.py targets      # confirms the loaded project replaced the page's own
python tools/gsbridge.py close
```

Targets are `localhost`, `github`, `editor`, `production` (`--project-id N`), and `url`.
`--project DIR`/`--sb3 FILE` load a build; production loads what is already on the page.
The bridge uses fresh isolated profiles (CDP 9400 default) and exposes a **subset** of
gsdev — no `watch`/`record`/`until`/`step`/property tools — but the shared procedure
profiler works there (`profile`, `profiling`, `setprofiling`, `profilereset`). `run`
waits for the page's own project before replacing it, so `targets` should list exactly
`Stage` plus your sprites (re-load, never delete sprites, if a page applies its project
late). Live variable edits work on `github`/`editor`; key injection can depend on page
focus, so verify with `get`. Run gsbridge with a resolved interpreter (on Windows
`.\.tools\python\python.exe tools\gsbridge.py ...`); the gsdev launcher does not forward.

## Platform, runtime and Scratch conventions

Use UTF-8 session files, especially for Japanese/CJK on Windows; PowerShell 5.1 pipes
are not UTF-8. Launcher paths and output support Unicode, but verify target-machine
behaviour rather than assume every policy/locale is identical. Keep conditions equal
when comparing runs (build, browser/version, headed vs headless, GPU, stage size,
inputs) and discard modal-obscured runs rather than trusting them. State the capture
window and whether profiling was on when reporting numbers.

Scratch 3 commonly stores bitmaps at **2×** with `bitmap_resolution = 2`; do not
"fix" a rendering discrepancy by downsampling assets — check the project. TurboWarp-only
options (`high_quality_pen`, `frame_interpolation`, custom clone limits) must not be
assumed on production Scratch. The local host uses 30-Hz compatibility and has no
attached audio engine, so sound-dependent behaviour needs review. The root project and
`smoke.txt` are the portable baseline; `examples/` and `tests/` are local gitignored
fixtures that may be absent on another machine.

## Credit and provenance

If you reuse code, assets, or an idea from a published Scratch project (or any
other production project), add a provenance comment in the GoboScript source so
the original authors are credited — next to the code you borrowed, not only in a
README:

```text
# Adapted from "<project title>" by <author> (scratch.mit.edu/projects/<id>), <license>.
```

Scratch projects are shared under CC BY-SA 2.0 by default, but state whatever
license the project actually uses. This applies to converted projects too: when a
project was decompiled (for example with sb2gs), record where the original came
from before editing it.

## References and completion

- [README and setup](README.md)
- [Integrating gobo-agent into an existing project](docs/integration.md)
- [GoboScript language documentation](https://aspiz.uk/goboscript/docs/language/syntax.html)
- [Scratch block opcodes](https://en.scratch-wiki.info/wiki/List_of_Block_Opcodes)

Consult the language docs rather than inventing syntax; verify exact compilation and
runtime behaviour with the installed compiler, generated SB3 and pinned VM. Opcode
names do not establish execution cost. Verify changed behaviour with a relevant scenario
and error checks, report what was tested plus limitations and artifact paths, and keep
the host warm while related work continues. Close only the sessions you own.
