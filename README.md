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

Host internals (the static host page) live in `tools/scratchhost/`; the pinned
`@scratch/*` bundles are shared under the tools root's `bundles/` and mounted at
`/vendor/` by the host server, so they are downloaded once per machine. Iteration
guidance is in [AGENTS.md](AGENTS.md).

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

`GSDEV_PYTHON` overrides the interpreter; otherwise the launchers check the tools
root's `python/` (or `GSDEV_PYTHON_DIR`) and then `python3`/`python` on `PATH`,
requiring 3.10+ in every case. The tools root is `GSDEV_TOOLS`, else `GSDEV_HOME`,
else a per-user cache (`%LOCALAPPDATA%\gobo-agent` on Windows,
`~/.cache/gobo-agent` on macOS/Linux), so goboscript, sb2gs, the portable Python and
the `@scratch/*` host bundles are downloaded once per machine and shared by every
project. `setup` skips anything already on `PATH`. The examples below use the Windows launcher (`tools\gsdev.ps1`);
on macOS/Linux use `./tools/gsdev`, or `python tools/gsdev.py` on any OS.

**goboscript** is fetched from gobo-agent's pinned prebuilt release (a fixed build of
upstream `main`), because upstream's newest release predates the goboscript#158
negative-literal fix; a `goboscript` on `PATH` still takes precedence.

Setup also provides **sb2gs** (the Scratch → GoboScript importer). It installs a pinned
copy (source commit + wheels) under the tools root's `sb2gs/`, with no pip/uv, and runs
it through the interpreter (`python <tools-root>/sb2gs/run.py …`) after applying
gobo-agent's patches; an `sb2gs` on `PATH` (or `GSDEV_SB2GS`) is only reported by
`doctor`, never executed or patched. It needs Python 3.14+. Decompile a Scratch project
and adopt it in one flow:

```powershell
tools\gsdev.ps1 sb2gs --id 12345678 my_project.sb3   # download + decompile
tools\gsdev.ps1 init --project my_project            # adopt with gobo-agent
my_project\gobo-agent\runtime\tools\gsdev.ps1 run    # then use the project's own launcher
```

Conversion is best-effort (unsupported opcodes, non-filename sprite names, …); see
the integration guide's "Known upstream limitations" for the failure classes.

The VS Code tasks (`.vscode/tasks.json`) use the same launchers — `./setup.sh` and
`./tools/gsdev` on macOS/Linux, `setup.ps1`/`gsdev.ps1` on Windows — so Setup, Build,
Run, Screenshot, Stop, Close and Check dependencies behave identically on both.
`python tools/gsdev.py tasks [--run]` verifies that resolution for the current OS.

Putting gobo-agent into a project you already have (rather than using this repo as
the template)? See the [integration guide](docs/integration.md) — `gsdev init`
adopts a project into a single `gobo-agent/` folder.

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

`run` and `screenshot` also take `--sb3 PATH` to load an existing artifact instead
of building (e.g. an imported or externally built `.sb3`).

An optional extension in `vscode-extension/` surfaces the manifest in VS Code's **Test
Explorer**: it discovers `gobo-tests.json` (at the workspace root, or under `gobo-agent/`
for an adopted project) via `test --manifest … --list`, offers Headless and Visible run
profiles, and runs `test --manifest … --json` through the project's launcher — execution
stays in the harness, and it needs no Node at runtime. It uses a per-project auto port, so
two projects don't collide. It is not required (the tasks and terminal do the same work).
A built `vscode-extension/gobo-agent-tests-<version>.vsix` is committed: install it from
**Extensions** (`Ctrl+Shift+X`) → **⋯** (top-right) → **Install from VSIX…** (activates in
the current window, no restart), or `code --install-extension …`. Maintainers can rebuild
it with `npx @vscode/vsce package`. Step-by-step use and troubleshooting are in
[`vscode-extension/README.md`](vscode-extension/README.md).

### MCP server

`tools/gobo_mcp.py` exposes the harness as **MCP tools** (`gobo_run`, `gobo_gsdev`,
`gobo_screenshot`, `gobo_session`, `gobo_doctor`, `gobo_close`, `gobo_gsbridge`, plus a
`gobo://status` resource). A stdio MCP server is spawned by the harness host rather than
by a sandboxed shell tool, so a `gobo_run` call can launch the browser normally.
`gobo_gsbridge` runs the same verbs against a live Scratch page (`--target production
--project-id N` for the real embed, or `editor` / `github` / `localhost`).

```sh
python tools/gobo_mcp.py                        # stdio transport
python tools/gobo_mcp.py --http 127.0.0.1:8770  # MCP Streamable HTTP at /mcp
```

Register it with the `@deepseek-ai/dsh-mcp-client` entry in
[`tools/gobo-mcp.patch.yml`](tools/gobo-mcp.patch.yml) (append it to the profile's
`cordis.patch.yml`, then restart the client); tools appear as `mcp__gobo__*`. The
allowlist, project containment and timeouts match the CLI's, and
`python tools/test_gobo_mcp.py` checks both transports without a browser.

For language editing, the upstream **`aspizu.goboscript`** extension adds syntax, snippets,
build-on-save diagnostics, and a `.sb3` **preview**. It has **no debugger** — don't press
F5/Run‑Debug; build with gobo-agent and open the built `.sb3` for its preview (▶/⏹/⟳ live in
the preview toolbar). Point it at gobo-agent's pinned compiler so it works without
`goboscript` on `PATH`, e.g. `"goboscript.compilerPath":
"C:/Users/<you>/AppData/Local/gobo-agent/goboscript/goboscript.exe"`. It coexists cleanly
with the gobo-agent-tests extension. Language reference: GoboScript docs (References below).

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
tools\gsdev.ps1 screenshot --no-reload --out debug/live.png   # capture the warm host, keep it running
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
| Read/write state | `get` (`--out FILE` writes the JSON: one object per selector, the list under `["value"]`), `set`, `set_batch` |
| Drive project events | `key`, `mouse`, `click`, `broadcast`, `broadcast_wait` |
| Wait for behaviour | `wait_frame`, `wait_until`, `wait_pixel` (session forms `waitframe`, `waituntil`, `waitpixel`) |
| Capture changes | `record`, `trace`, `until … --pause` |
| Check rendering | `screenshot`, `pixel`, session pixel assertions |
| Check errors | `errors`, `expect_no_errors` |
| Lifecycle | `run` (build + green flag; returns when the VM goes idle), `restart` (re-run the green flag, warm host), `wait_stopped` (event-driven wait for the VM stop), `stop`, `close` |
| Check Scratch fit before publishing | `preflight` (offline; `--sb3 FILE` to scan an artifact, `--json`, `--strict`) |

The standalone `screenshot` command builds/reloads, captures, then stops the project;
`--no-build` skips compilation but still reloads. It is not a state-preserving snapshot
of a running scene. Failure-bundle screenshots use the standing host's stage capture.

### Preflight memory model

`preflight` is an offline *estimate*, not a measurement. It reports two budgets:

- **estimated renderer memory** — bitmap costumes: the stored PNG pixels, ×4 when
  `bitmapResolution` is 1 (the VM upscales it to 2× at load), at 8 bytes/px
  (texture + silhouette); SVG costumes: viewBox area × MIP², where
  `mip = 2^ceil(log2(min(deviceScale, 2048/maxDimension)))`, at 12 bytes/px
  (texture + per-skin canvas + silhouette); plus decoded sound PCM. Default **smooth
  target 0.85 GiB** with a separate **hard limit 1.6 GiB**: above the target is a
  `*-over-target` error (will stutter), above the hard limit is a crash error. Evidence: 1056403018
  0.65 and the cropped 957967074 0.80 play smoothly; 941463385 1.17, 1331442506 1.39 and
  52 sheets 1.55 stutter; 57 sheets 1.70 and 1151108301 fail.
- **project-wide costume pixels** — stored bitmap pixels + 2× SVG raster (draw-scale
  aware), smooth target 110,000,000 and hard limit 300,000,000 (cropped 104M is smooth;
  941463385 185M, 1331442506 249M and 52 sheets 277M stutter; 57 sheets 304M and
  941195677 588M fail).

`deviceScale` defaults to 2 (`canvas.width / 480`, the value the renderer uses for
SVG MIPs). It is an integer setting: use 4 for a 3.5× phone (the MIP is a power of
two, so 3.5 and 4 select the same 4× level) or 1 for a desktop-only scan. Every
threshold is a `GSDEV_PREFLIGHT_*` environment variable or a `gobo-agent.json`
`preflight*` key.

`deviceMemoryMb` (0 = off) scales both budgets linearly from the 2 GB baseline
(2048 → 0.85 GiB target / 110M). The matrix targets 2 GB and up; sub-2 GB hardware is
out of scope.

**Alpha does not change the texture estimate.** Scratch uploads every skin as RGBA
(`Skin._setTexture` uses `gl.RGBA`; `SVGSkin` uploads `getImageData`), and the
silhouette is maintained for opaque and transparent skins alike. An RGBA PNG may be
larger on disk and cost more to download/decode, but an RGB PNG of the same dimensions
uses the same renderer bytes; transparent SVG margins still cost the full viewBox. A
paired 512×512 test measured 2,097,152 B for both RGB and RGBA PNGs (1.9 KB vs 2.2 KB
encoded), and 12,582,912 B for both opaque and 50%-alpha SVG.

Loading also has a **transient peak** above the retained total: decoded bitmap RGBA
buffers coexist with the textures being uploaded and the compressed assets still held
by the loader. `preflight` reports `load peak = retained + 4 bytes × bitmap raster +
compressed assets`; above the hard limit it raises `load-peak-over-budget`. This is a
worst-case all-bitmap-costumes model, not a measurement of how many decodes a browser
runs at once; SVG already carries its per-skin canvas/ImageData in the retained 12 B/px.
It catches the AVIF floor project `floor-avif-wasd-192`: 1.30 GiB retained, but
~2.03 GiB peak, which fits the 2 GB Android run and the consistent iPad crash.

Device profiles (provisional, set with `GSDEV_PREFLIGHT_MEMORY_BUDGET` /
`GSDEV_PREFLIGHT_PIXEL_BUDGET`):

| profile | target | hard limit | evidence |
| --- | --- | --- | --- |
| Android/iPad, 2 GB | 0.85 GiB / 110M | 1.6 GiB / 300M | 1056403018 (0.65) and cropped 957967074 (0.80) smooth; 941463385 (1.17), 1331442506 (1.39) and 52 sheets (1.55) stutter; 57 sheets (1.70/304M) and 1151108301 fail |
| Chromebook, 4 GB (single unit: 3.79 GiB usable, 5.5 GiB swap) | 5 GiB / 900M | 5 GiB / 900M | 941195677 (3.36/588), 951197417 (4.57/790) and 627583842 (3.17/568) pass/stutter; 117487433 (14.46/2.59G) fails |
| Desktop, 16+ GB | 16 GiB / 3G | 16 GiB / 3G | 117487433 (14.46 GiB/2.59G) passes on a high-RAM desktop |

Desktop is RAM/GPU-dependent: the 14.46 GiB Bad Apple passes on a high-RAM desktop
(16 GiB / 3G budget), so the mobile and Chromebook lines are not desktop limits.

`svg-hang-risk` warns when SVG raster alone exceeds the memory budget — count doesn't
matter (4096 4×4 skins are ~65k px and fine). 117487433's 15 GiB of SVG raster drove a
4 GB Chromebook into a full swap hang at ~25% (power cycle), not just a tab crash.

`slow-load-risk` warns when stored SVG pixels exceed 100M — 1331442506's 124M predicts
the ~4 min phone load. `svg-complexity` warns above ~50k elements — 1331442506's 61
frames hold **472k elements** (max 7,873), the DOM/parse side of the same load; measured
controls: 1k and 10k elements load instantly, 100k takes ~30 s on desktop and mobile.
The 30 s is node-count-bound, not text-length-bound: ~4 MB of SVG text in one element
loads in 136 ms, 10k padded nodes in 3.4 s, and 100k zero-size nodes in 19.4 s — paint
accounts for the last ~10 s.
`svg-css-math` warns when an SVG uses CSS `calc()`, `matrix3d()`
or variables. A 1k-node control at 1000% loads in 367 ms static vs 278/291/432 ms for
vars/calc/trig+pow — CSS math is not a major load driver at that scale; node count is.
`svg-nested-data-uri` warns when an SVG embeds another SVG as a data URI (the packed
floor sheets do this): the scanner measures the outer document, so inner element count
and CSS math are not included.
1041228311's own startup is its floor sheets at 1000%: only 5 of the 11 floor costumes
are ever drawn, each becoming a 1536×1728 texture (31.85 MB with canvas + silhouette),
~159 MB in total, generated on first draw in ~2.7 s and stuttery until cached; the
other 6 costumes never rasterise. The site's ~5 s page load (embed DOM 4.48 s) is
GUI/network on top of the ~2.7 s local VM load; the ~3 s after Play is this texture
generation, not CSS.

Large SVG sets sometimes **prewarm** on load (`switch_costume` + `stamp` in a green-flag
loop) to avoid first-draw hitches; 1387738760 preloads 181 costumes this way. Prewarming
doesn't change the retained total, only when it is paid — so it shows up as startup time,
not a new memory cost.

ChromeOS tolerates more than mobile because it swaps/zrams and has a desktop-class
GPU; the Chromebook line is therefore between 4.57 and 14.46 GiB, and 5 GiB / 900M is
the lowest known passing bound (951197417 uses ~4.6 GiB of a 9.3 GiB RAM+swap budget
and stutters). This is a **4 GB** machine, not a 2 GB one; a true 2 GB Chromebook
would need its own (lower) tier.

It uses the largest of each sprite's saved size and literal `set size to` requests
across its blocks, including procedures and unused branches. This handles scripts
that grow on a tiny costume and then switch to a large sheet: the saved costume's
size clamp does not bound later costumes. A variable/reporter size input or any
`change size by` block has no inferred bound; `runtime-size-unknown` warns and uses
each SVG's maximum renderer MIP (and fails `--strict`). Bitmap textures keep their
fixed loaded dimensions. JSON `memory.targetSizes` records saved and inferred sizes;
`memory.largest` records draw scale, raster scale and raster dimensions. The text
report shows runtime enlargement and the largest raster dimensions too.

The model retains one largest MIP per costume and counts every costume, including
unused/hidden ones; the full viewBox still costs texels. On a 1× canvas a
37.5% sprite uses the 0.5× MIP (1331442506 measures 356 MB), but on 2×/3.5× the same size
requests 0.75/1.31 and still rounds to the 1× MIP because the 1920-wide viewBox caps at
1× — so the mobile estimate stays 1.39 GiB. The stage framebuffer, extra scale MIPs and
driver overhead are not modelled, so treat a pass as "no detected texture risk", never as
a guarantee of crash-free mobile playback.

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
invokes gsdev, not gsbridge, so run gsbridge with a resolved interpreter (the shared
`tools-root/python/python.exe`, or a `python` on `PATH`).

```powershell
python tools/gsbridge.py run --target github --project . --headless --duration 6 --leave-running
python tools/gsbridge.py targets
python tools/gsbridge.py expect_no_errors
python tools/gsbridge.py profile --seconds 3
python tools/gsbridge.py close
```

`run --leave-running` records the port in `.gsbridge/current_<DSH_SESSION_ID>.json`;
later commands with no `--port` use it (explicit `--port` and `GSBRIDGE_PORT` override).
`GSBRIDGE_STATE_DIR` moves the state root. `close` clears the record and the per-port
host state.

Every launch also writes `.gsbridge/<app>/browser_<port>.json` (PID + profile)
immediately after spawn. `close` uses the session token when present and that registry
otherwise, killing only a PID whose command line/profile/start time matches the launch;
`run` reaps dead records, and `close` reaps live ones for this DSH session. A ctypes
`TerminateProcess` fallback covers sandboxes that deny `os.kill`. On POSIX, ownership
uses `/proc/<pid>/cmdline` (or `ps`), termination uses `SIGTERM`/`SIGKILL`, and host
records match `ps -o comm=` names.

Maintainer-only cleanup scenarios (gitignored, not shipped):
`tools/test_gsbridge_cleanup.py`, `tools/test_gsbridge_session.py`, and
`tools/test_gsdev_cleanup.py` (Windows and POSIX).

Targets are `localhost`, `github`, `editor`, `production` (`--project-id N`), and `url`.
`production` runs the page's existing project; other loading paths accept a
project/`.sb3`. Confirm replacement with `targets` (entry: re-load, never delete
sprites). The bridge shares gsdev's whole verb surface — 39 verbs with identical syntax
and result shapes (only the build verbs stay gsdev-only) — and that parity is checked by
`tools/verb_parity.py` via `gsdev selftest`, so the same commands and asserts work against a
live site. Key input can depend on page focus; verify with `get`.

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
| `GSDEV_GOBOSCRIPT` | Compiler override (else `PATH`, else the pinned bundle) |
| `GSDEV_TOOLS`, `GSDEV_HOME` | Tools root override / shared per-user home |
| `GSDEV_SB2GS` | A user sb2gs to *report* (`doctor`/`status`); never executed or patched — the harness always runs its own pinned, patched copy |
| `GSDEV_BROWSER` | Browser executable |
| `GSDEV_CDP_PORT`, `--port` | Local CDP port (default 9230); `--port 0` picks one |
| `GSDEV_HOST_PORT` | Local static-server port (default 8077) |
| `GSDEV_HOSTSERVER_TIMEOUT` | Seconds to wait for the host server's ready line (default 2) |
| `GSDEV_HOST_TIMEOUT` | Seconds to attach to the browser, and the fallback when it prints no `DevTools listening` line (default 5) |
| `GSDEV_READY_TIMEOUT` | Seconds for the page to reach `gsdev:ready` (default 10) |
| `GSDEV_CDP_TIMEOUT` | Seconds for one CDP call (default 2) |
| `GSDEV_READ_TIMEOUT` | Seconds for one live read/write, e.g. `get`/`set`/`inspect` (default 5) |
| `GSDEV_LAUNCH_ATTEMPTS` | Browser launches to try before failing; a retry keeps the host server alive (default 2) |
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
- [Integrating gobo-agent into an existing project](docs/integration.md)
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
