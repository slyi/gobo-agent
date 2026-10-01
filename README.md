# gobo-agent

An automation and test harness for **GoboScript projects on the vanilla Scratch
runtime**. It builds a project, runs it in a real browser, drives the VM (mouse and
keyboard), inspects and edits live state, validates behaviour with assertions, and
profiles hot procedures — with no Scratch desktop app, Electron, Node, Rust, or
compiler toolchain.

It is built for a fast loop: keep the browser warm, batch checks with `session`, and
reload in place after source edits. The same commands work from VS Code, a terminal,
or a coding agent.

## Capabilities

- **Build & run** a GoboScript project (`.gs` → `.sb3`) headlessly or visibly in
  Chrome/Edge/Chromium, on upstream `@scratch/scratch-vm` + `scratch-render`.
- **Drive the UI and VM** — click, mouse move/press/release, and key press/release,
  including character keys for project-defined text controls. Input is injected at the
  VM level; it does not automate Scratch editor menus or native text fields.
- **Read/write live state** — variables, lists, sprite properties, clones; `set_batch`
  writes several values in one evaluation.
- **Assert behaviour** — session files with `expect`, `expectpixel` and `capture`, plus
  `errors`/`expect_no_errors` (covers VM/page exceptions and goboscript `error` logs).
- **Failure bundles** — `session --bundle` and `test` write structured evidence:
  assertion records with the state captured at assertion time, recent events, a
  reproduction recipe, and an optional stage image, under `--artifacts`.
- **Inspect & capture** — `inspect`, `props`, `prop`, `clones`, `broadcast`, `record`,
  `trace`, `until … --pause`, stage screenshots and pixel readback.
- **Profile** — a built-in procedure profiler (`profile`, `profiling`) attributes
  executed operations per procedure/top-level script and pen draws per step.
- **Check on real Scratch** — `tools/gsbridge.py` runs the same style of verbs against
  the GitHub player, the production editor, a production embed, or a URL.

## Choose the tool

| Tool | Purpose |
| --- | --- |
| `tools/gsdev.py` | Local build/run loop, unit and functional tests, live tuning, profiling |
| `tools/gsbridge.py` | Same-style verbs on the GitHub player, editor, embed, or a URL |

Host internals (the static host page and pinned `@scratch/*` bundles) live in
`tools/scratchhost/`. Iteration guidance is in [AGENTS.md](AGENTS.md).

## Requirements

- **A Chromium-based browser.** Windows already has Edge built in; on macOS you need
  **Chrome** (Safari is out of scope); on Linux, Chrome or Chromium. `GSDEV_BROWSER`
  overrides the executable.
- **No administrator rights.** Downloads and executable launches remain subject to
  machine policy; setup never bypasses it.

## Install

Anywhere with Python:

```sh
python tools/gsdev.py setup
python tools/gsdev.py doctor
```

Windows (no Python needed — setup fetches a portable interpreter):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
tools\gsdev.ps1 doctor
```

macOS/Linux launchers (mirror the PowerShell ones; a Chromium-based browser is
expected to be installed):

```sh
./setup.sh                        # find Python 3.10+, then install the tools
./tools/gsdev doctor              # resolve Python and run the dev loop
./setup.sh --only vendor --force  # other bootstrap flags pass straight through
```

`GSDEV_PYTHON` overrides the interpreter; otherwise the launchers check
`.tools/python/bin/python3` (or `GSDEV_PYTHON_DIR`) and then `python3`/`python` on
`PATH`, requiring 3.10+ in every case. On Windows the portable install is
`.\.tools\python\python.exe`. The examples below use the Windows launcher
(`tools\gsdev.ps1`); on macOS/Linux use `./tools/gsdev`, or `python tools/gsdev.py` on
any OS.

The VS Code tasks (`.vscode/tasks.json`) use the same launchers — `./setup.sh` and
`./tools/gsdev` on macOS/Linux, `setup.ps1`/`gsdev.ps1` on Windows — so Setup, Build,
Run, Screenshot, Stop, Close and Check dependencies behave identically on both.
`python tools/gsdev.py tasks [--run]` verifies that resolution for the current OS.

## VS Code

Open the project folder. **Ctrl+Shift+B** on Windows/Linux (**Cmd+Shift+B** on macOS)
runs the default **`gobo-agent: Run (web, build + live logs)`** task: it checks setup,
builds the project, opens or reuses a **visible browser**, and streams logs and basic
performance readings in the task terminal until you stop it. It does **not** pass
`--headless`, so the default task is a visible, warm host.

`Terminal > Run Task` lists Setup, Build, **Run Tests**, Screenshot, Stop project,
Close host and Check dependencies. `Run Tests` runs the test manifest
(`gobo-tests.json`) in the same browser mode as the standing host and is the default
test-group task; Screenshot is no longer a test. Stop ends project execution; Close
releases the session (the two are different). The integrated terminal then runs the
complete automation workflow — input injection, live inspection/tuning, session
validation, failure bundles and profiling. Those are CLI commands; there is no
dedicated profiling task, and no extension is required.

Keep the browser mode consistent with the standing host: a visible VS Code host should
receive visible CLI runs (or you close it first and start a headless one). Mixing modes
without closing leaves a warm host that CLI commands will refuse to reuse.

```powershell
tools\gsdev.ps1 run --duration 2 --leave-running
tools\gsdev.ps1 session --file checks.txt --bundle
tools\gsdev.ps1 profile --no-reload --no-restart --seconds 2 --no-follow --leave-running
tools\gsdev.ps1 setprofiling off
```

An optional extension in `vscode-extension/` surfaces the manifest in VS Code's **Test
Explorer**: it discovers `gobo-tests.json` via `test --manifest … --list`, offers Headless
and Visible run profiles, and runs `test --manifest … --json` through the launcher —
execution stays in the harness, and it needs no Node at runtime. It is not required (the
tasks and terminal do the same work). A built
`vscode-extension/gobo-agent-tests-<version>.vsix` is committed: install it from **Extensions**
(`Ctrl+Shift+X`) → **⋯** (top-right) → **Install from VSIX…** (activates in the current
window, no restart), or `code --install-extension …`. Maintainers can rebuild it with
`npx @vscode/vsce package`. Step-by-step use and troubleshooting are in
[`vscode-extension/README.md`](vscode-extension/README.md).

For language editing support, see the GoboScript documentation (References below).

## Warm edit-and-check loop

Start the host once, then keep using it. Bounded `run` calls stream logs and leave the
project running:

```powershell
tools\gsdev.ps1 run --headless --duration 2 --leave-running
tools\gsdev.ps1 inspect main
tools\gsdev.ps1 set_batch main.tx=120 main.ty=0
tools\gsdev.ps1 session --file checks.txt --bundle
# edit the .gs source, then rebuild and reload in the same browser:
tools\gsdev.ps1 run --headless --duration 2 --leave-running
```

`set`/`set_batch` change live data and never rebuild or reload. Source changes need a
build/reload, which `run` performs in place while reusing a compatible browser. A warm
browser does not mean the project state is unchanged — reloading replaces the project.
End the session when the work is done rather than closing between edits:

```powershell
tools\gsdev.ps1 close
```

## Sample scenarios

**1. First run**

```powershell
tools\gsdev.ps1 run --headless --duration 3 --leave-running   # build, load, stream logs
tools\gsdev.ps1 close
```

**2. Edit-and-check loop (keep the browser warm)**

```powershell
tools\gsdev.ps1 run --headless --duration 2 --leave-running
tools\gsdev.ps1 set_batch main.tx=120 main.ty=0               # live data, no rebuild
# ...edit .gs...
tools\gsdev.ps1 run --headless --duration 2 --leave-running   # rebuild + reload
tools\gsdev.ps1 close
```

**3. A repeatable unit test** — save `checks.txt`:

```text
key space down
waitframe 4
key space up
capture main.keys main.tx
expect main.keys >= 1
set main.tx 120
waitpixel 120 0 #4c97ff 3000
expect_no_errors
```

```powershell
tools\gsdev.ps1 run --headless --duration 2 --leave-running
tools\gsdev.ps1 session --file checks.txt          # exits non-zero on failure
tools\gsdev.ps1 session --file checks.txt --bundle # + failure bundle under debug/
tools\gsdev.ps1 test checks.txt --headless --json  # builds each file's project
```

**4. Debug live state**

```powershell
tools\gsdev.ps1 inspect main
tools\gsdev.ps1 get main.tx "@stepfps"
tools\gsdev.ps1 watch main.tx --duration 2
tools\gsdev.ps1 errors
tools\gsdev.ps1 profile --seconds 2 --headless --json debug/profile.json  # see Profiling
```

**5. Screenshot and pixel**

```powershell
tools\gsdev.ps1 screenshot --headless --out debug/frame.png
tools\gsdev.ps1 pixel 0 0
```

**6. Check it on real Scratch**

```powershell
python tools/gsbridge.py run --target github --project . --headless --duration 6 --leave-running
python tools/gsbridge.py get main.tx "@fps" "@gpu"
python tools/gsbridge.py expect_no_errors
python tools/gsbridge.py targets                 # confirm the page's project was replaced
python tools/gsbridge.py profile --seconds 3      # per-procedure hotspots + Draws/step
python tools/gsbridge.py close
```

Targets: `localhost`, `github`, `editor`, `production` (`--project-id N`), `url`.

## Automation and validation

The harness injects input at the VM level: click project buttons, move/hold/release
the mouse, and press/release keys, including character keys used by project-defined
text controls. Coordinates are Scratch stage coordinates (centre `0,0`, +x right,
+y up). It does not operate Scratch editor menus or native text fields. Hold/release
keys across frames for polling scripts, and verify with `get` that input landed.

A session file is UTF-8, one command per line. `capture SELECTOR...` reads the listed
state together with the next `expect`, in one in-page evaluation, so a failure carries
assertion-time state rather than a later re-read:

```text
capture main.keys main.tx
expect main.keys >= 1
```

```powershell
tools\gsdev.ps1 session --file checks.txt --bundle   # failure bundle under --artifacts
tools\gsdev.ps1 test checks.txt --headless --json    # builds/reloads each test's project
tools\gsdev.ps1 test --manifest gobo-tests.json --headless   # run the manifest's tests
tools\gsdev.ps1 test --manifest gobo-tests.json --list       # validate + list (read-only)
```

Both report failure through a non-zero exit. `--artifacts DIR` chooses the bundle
location; a bundle contains a manifest, `failures.json` (with captured state),
`state.json`, `events.jsonl`, `reproduction.txt`, and an optional `stage.png`, written
atomically. Passing runs write no bundle. `session --events PATH` writes the versioned
assertion records as JSONL without assembling a bundle.

`test --manifest PATH` reads a versioned `gobo-tests.json` (schema 1) declaring project
roots and session files; `--list` validates and lists them read-only (schema, duplicate
ids, missing files, and root/session path escapes). `buildMode` is reserved for
debug/release builds and currently ignored.

Useful inspection commands:

| Need | Commands |
| --- | --- |
| Discover state | `inspect`, `props`, `prop`, `clones` |
| Read/write state | `get`, `set`, `set_batch` |
| Drive project events | `key`, `mouse`, `click`, `broadcast`, `broadcast_wait` |
| Wait for behaviour | `wait_frame`, `wait_until`, `wait_pixel` (session forms `waitframe`, `waituntil`, `waitpixel`) |
| Capture changes | `record`, `trace`, `until … --pause` |
| Check rendering | `screenshot`, `pixel`, session pixel assertions |
| Check errors | `errors`, `expect_no_errors` |

The standalone `screenshot` command builds/reloads, captures, then stops the project;
`--no-build` skips compilation but still reloads. It is not a state-preserving snapshot
of a running scene. Failure-bundle screenshots use the standing host's stage capture.

## Debug and release builds

`build`, `run`, `screenshot`, `test` and `profile` accept `--mode debug|release`
(default `debug`; `GSDEV_MODE` overrides). A project opts in to the harness diagnostic
macros with `%include tools/gsdev_mode`:

```text
DBG_LOG("player spawned")     # `log` in debug, erased in release
DBG_SAY("paused")             # `say` in debug, erased in release
DBG_ADD(drawCount, 1)         # drawCount += 1 in debug, erased in release
DBG_SET(timer, 0)             # timer = 0 in debug, erased in release
```

The macros are statements and own the trailing `;` — do not add one at the call site. The
project ships a checked-in `tools/gsdev_mode.gs` (debug by default, so a plain
`goboscript build` also works); `gobo-agent` rewrites it for the requested mode, builds,
and restores it, so your working tree is unchanged. Release expands the macros to
nothing, removing the call **and its arguments** at compile time. Switching modes needs a
rebuild (`--no-build`/`--no-reload` reuse whatever is built). Put only explicitly
diagnostic code in these macros — keep safety checks and real state changes in release.

## Profiling

`profile` counts the primitive operations the VM executes per step and attributes them
to procedures, so you can see where the work is. It is intrusive (roughly 2x step
time), so use it to find hotspots and compare **shares**, never as a timing verdict.

```powershell
tools\gsdev.ps1 profile --seconds 3 --headless      # stream one line per step
tools\gsdev.ps1 profile --seconds 3 --no-follow     # summary only
tools\gsdev.ps1 profiling --steps 5                 # reprint the current report
tools\gsdev.ps1 setprofiling off                    # stop counting
# open the window only once the scene is ready, so load/setup work is excluded:
tools\gsdev.ps1 profile --seconds 2 --wait-until Tick.loaded --wait-op == --wait-value 1 --no-follow
```

To profile the scene already loaded in a warm host without disturbing it, add
`--no-reload --no-restart --leave-running`; otherwise `profile` builds/reloads and
restarts the project. Without `--leave-running`, capture disables profiling and stops
the project (the browser stays open); with it, profiling stays enabled until
`setprofiling off`. `--wait-until SEL --wait-op OP --wait-value V` opens the window once
a condition holds — e.g. `--wait-until Tick.loaded --wait-op == --wait-value 1` — so
loading/setup work is excluded (default timeout 60 s, override with `--wait-timeout`).

The summary line and table look like this (a pen-heavy project with several sprites;
`--top 10`. Rows are the top 10 by `self%` — the default selection — printed in
`total%` order):

```text
steps=91 | Step ms=33.13 | Draws/step=562.7 | Blocks/step=47197.8 | total=4342657 self=4342657 unattributed=0
procedure                                 total%   self%   kids%  ctx  blocks/step   calls calls/step
vectorRooms :: _3dEngine                   32.7%   23.1%    9.6%  all     15587.00      91       1.00
wallShader :: greekKeyWallPattern          27.8%    4.4%   23.4%  all     13272.00     364       4.00
renderFrame :: drawWallShader...           14.3%   13.9%    0.4%  all      6801.00     637       7.00
wallShader :: emit_dashed_row...            8.7%    8.3%    0.4%  all      4164.00    1456      16.00
wallShader :: emit_logical_hline...         8.1%    4.3%    3.8%  all      3876.00    5460      60.00
renderFrame :: trapezoid...                 6.0%    5.9%    0.0%  all      2846.00     728       8.00
vectorRooms :: process_room...              4.8%    4.6%    0.2%  all      2275.00      91       1.00
wallShader :: emit_vertical_element...      3.7%    3.1%    0.6%  all      1764.00    3276      36.00
floorShader :: azexTriangleHelper...        3.6%    3.6%    0.0%  all      1738.00     546       6.00
wallShader :: emit_line...                  3.4%    3.3%    0.1%  all      1620.00    3276      36.00
... 45 more procedure(s) in the JSON report
top-level: vectorRooms :: event_whenbroadcastrec...   0.0% self  33.0% total  ui
top-level: wallShader :: event_whenbroadcastrece...   0.0% self  29.9% total  ui
top-level: renderFrame :: event_whenbroadcastrec...   0.0% self  27.2% total  ui
top-level: floorShader :: event_whenbroadcastrece...   0.1% self   6.3% total  ui
```

Summary line (one per capture):

| Field | Meaning |
| --- | --- |
| `steps=91` | completed VM steps in the window (the host steps at ~30 Hz) |
| `Step ms=33.13` | mean instrumented `stepThreads` ms per step — inflated by the profiler (~2x), so compare shares, not ms |
| `Draws/step=562.7` | mean pen draw submissions (line/point and stamp) per step |
| `Blocks/step=47197.8` | mean executed primitive dispatches per step, project-wide |
| `total=4342657` | primitive dispatches attributed in the window |
| `self=4342657` | sum of the procedures' own-body operations |
| `unattributed=0` | operations not attributed to a tracked procedure/script |

`sum(self) + unattributed == total` always holds. "Blocks" are *executed primitive
dispatches* (VM operations), not Scratch editor blocks. The summary `Blocks/step` is the
project-wide mean; the table's `blocks/step` column is each row's **total** subtree per
step, so it need not descend with the row order.

The ranked table lists one row per `sprite :: procedure` (several sprites side by side):
`total%` is the whole subtree, `self%` the procedure's own body, and `kids%` its callees
(`total% = self% + kids%`). Rows are selected by `--sort` (`self%`, the default) and
printed in `total%` order; `ctx` is `ui` (screen-refresh, cut at the step's work budget)
or `all` (without-screen-refresh / warp); `calls` and `calls/step` are invocation
counts. `top-level:` rows are each sprite's `event_*` scripts (containers), shown when
they hold at least `--hats-min` percent (default 2). Clones aggregate by original sprite.

Three blocks follow: a per-frame share spread (median and p10–p90 across frames, plus
how many frames each row appears in); the slowest frames with their top contributors and
draw counts; and a sampled wall-time share per procedure. A separate `screen-refresh
budget` line flags a large loop sitting in a hat — its `blocks/step` saturates at the
budget, so read the flag, not the count.

Views: `--top N`, `--sort self|kids|total` (`incl` aliases `total`), `--children NAME`
(a procedure's callees), `--hats-min P`, `--json PATH`, and `--wait-until SEL --wait-op
OP --wait-value V`. Draw submissions are counted while profiling; basic `@fps`/
`@stepfps`/`@steptime` readings do not require it.

## Check on live Scratch sites

`tools/gsbridge.py` uses the same verbs against hosted pages; the `gsdev` launcher
invokes gsdev, not gsbridge, so run gsbridge with a resolved interpreter (on Windows,
`.\.tools\python\python.exe tools\gsbridge.py ...` when Python is not on `PATH`).

```powershell
python tools/gsbridge.py run --target github --project . --headless --duration 6 --leave-running
python tools/gsbridge.py targets
python tools/gsbridge.py expect_no_errors
python tools/gsbridge.py profile --seconds 3
python tools/gsbridge.py close
```

Targets are `localhost`, `github`, `editor`, `production` (`--project-id N`), and `url`.
`production` runs the page's existing project; other loading paths accept a
project/`.sb3`. Confirm replacement with `targets` (entry: re-load, never delete
sprites). The bridge exposes a subset of local commands plus the shared profiler — check
its help rather than assuming parity. Key input can depend on page focus; verify with
`get`.

## OS and locale support

The launchers support Windows, macOS and Linux with Chromium-based browsers. Python
acquisition differs by OS as described above, and supported platforms do not imply every
managed device permits setup.

Non-English system locales, Unicode project paths and Japanese/CJK text are intended to
work. Use UTF-8 session files with `--file`/`test`, especially on Windows: PowerShell 5.1
pipes can alter non-ASCII input before Python receives it. JSON `\uXXXX` escapes are
valid representations of the original text. Different OS/GPU/browser combinations can
render and time differently — validate the behaviour you rely on.

## Configuration and troubleshooting

`--cpu RATE` throttles the renderer's CPU via CDP (e.g. `--cpu 4` ≈ a slow phone).
It is accepted by `gsdev` `run`/`screenshot`/`profile` and `gsbridge` `run`/`session`.
It changes timing, so use it to exercise slow-device behaviour, not as a performance
verdict.

| Setting | Purpose |
| --- | --- |
| `GSDEV_PROJECT` | Project directory (default: this repository) |
| `GSDEV_PYTHON`, `GSDEV_PYTHON_DIR` | Interpreter / portable install directory |
| `GSDEV_GOBOSCRIPT`, `GSDEV_TOOLS` | Compiler override / bundled-tools directory |
| `GSDEV_BROWSER` | Browser executable |
| `GSDEV_CDP_PORT`, `--port` | Local CDP port (default 9230); `--port 0` picks one |
| `GSDEV_HOST_PORT` | Local static-server port (default 8077) |
| `GSBRIDGE_PORT` | Bridge CDP port (default 9400) |
| `GSDEV_SOFTWARE=1` | Force software rendering |
| `GSDEV_NO_AUTO_SETUP=1` | Never auto-download dependencies |

Run `doctor` for dependencies, `selftest` for host configuration, and `tasks --run` to
check VS Code task resolution. Windows `setup.ps1` offers `-Only`, `-Force`, `-Offline`;
`setup --offline` reports a missing Python immediately instead of downloading.

| Problem | Next step |
| --- | --- |
| Missing dependencies | Run setup, then `doctor` |
| Python opens the Microsoft Store | Use `setup.ps1` / `gsdev.ps1` |
| Scripts or executables blocked | Machine policy applies; execution-policy bypass is not enough |
| Download/TLS failure | Check access to python.org, GitHub releases, and the npm registry |
| Host port occupied | Set `GSDEV_HOST_PORT`; do not kill an unrelated service |
| Browser modal covers the project | Discard timing; inspect the dialog and launcher output |
| Source changes appear ignored | Rebuild `.gs`; relaunch the host after a harness update |
| Unicode problems | Use UTF-8 session files |

## Validation and layout

`doctor` checks dependencies; `selftest` checks the host configuration; `tasks --run`
checks VS Code tasks. Functional coverage starts with the root project and `smoke.txt`;
`examples/` and `tests/` are local-only, gitignored material and may be absent on
another machine. Broad Windows/macOS CI smoke is not proof for every machine policy or
locale — the strictest check is a clean, non-elevated standard-user install on Japanese
Windows. The local host uses 30-Hz compatibility and has no attached audio engine, so
sound-dependent behaviour needs review.

## References

- [Agent iteration guide](AGENTS.md)
- [GoboScript syntax and language documentation](https://aspiz.uk/goboscript/docs/language/syntax.html)
- [Scratch opcode reference](https://en.scratch-wiki.info/wiki/List_of_Block_Opcodes)

Check generated SB3s and installed compiler/runtime versions for exact behaviour; the
opcode reference identifies blocks but does not define their runtime costs.

## License

MIT (see [LICENSE](LICENSE)): use, modify, and redistribute freely, with no copyleft
obligation. The compiler and browser bundles that `setup` downloads are **not** part of
this repository and keep their own upstream licenses — goboscript and the `@scratch/*`
packages (AGPL-3.0-only) are fetched into gitignored directories, so the MIT grant here
covers only gobo-agent's own source.
