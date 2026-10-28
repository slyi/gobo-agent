# Integrating gobo-agent into an existing project

gobo-agent compiles a GoboScript project, runs it on the vanilla Scratch VM, and
lets you inspect, drive, and profile it from the command line. This guide is for
putting it into a project you already have. (Starting from scratch instead? Clone
gobo-agent and use it as the project — see the [README](../README.md).)

Everything gobo-agent adds lives in a single `gobo-agent/` folder. Outside that
folder it touches only `.gitignore` and `.vscode/tasks.json` (merged, not
replaced), plus `.gitmodules` if you use the submodule layout. Your `*.gs`
sources, `goboscript.toml`, and assets are never modified.

## The `gobo-agent/` folder

```text
your-project/
├── goboscript.toml              project files (untouched)
├── main.gs  stage.gs  assets/
├── .gitignore                   merged: adds gobo-agent ignore rules
├── .vscode/tasks.json           merged: adds "gobo-agent:" tasks
├── .gitmodules                  only with the submodule layout
└── gobo-agent/
    ├── runtime/                 the harness (copy, or a git submodule)
    ├── gobo-agent.json          project config
    ├── gobo-tests.json          test manifest
    ├── smoke.txt                default session
    ├── gsdev_mode.gs            debug/release include
    ├── VERSION                  install marker (layout, harness commit)
    ├── .install.json            ownership manifest (created files + hashes)
    └── state/                   run state: port file, browser profiles (ignored)
```

Build output (your `.sb3`, `debug/`) stays at the project root.

## Before you start

- A Chromium-based browser. Windows already has Edge; on macOS install Chrome.
- Python. On Windows you need none — `setup` fetches a portable Python 3.12. On
  macOS/Linux you need Python 3.12+ (the Scratch importer and setup baseline).
- One gobo-agent checkout to run `init` from: `git clone <gobo-agent>` or
  download the repository ZIP. `init` copies or references the runtime; you do
  not edit the harness.

## Adopt an existing project

Run `init` from the gobo-agent checkout, pointing at your project. It is
non-destructive and idempotent.

```powershell
# 1. Preview — prints every write, changes nothing
python <gobo>\tools\gsdev.py init --project <project> --dry-run

# 2. Apply — copies the runtime and merges the root files
python <gobo>\tools\gsdev.py init --project <project>

# 3. Install the tools (optional; first run does it too)
<project>\gobo-agent\runtime\setup.ps1        # POSIX: ./gobo-agent/runtime/setup.sh

# 4. Check and run
<project>\gobo-agent\runtime\tools\gsdev.ps1 doctor
<project>\gobo-agent\runtime\tools\gsdev.ps1 run
```

The first `run`/`build`/`test` fetches anything missing with no admin rights. Tools
are shared per machine (`GSDEV_TOOLS`/`GSDEV_HOME`, else `%LOCALAPPDATA%\gobo-agent` on
Windows / `~/.cache/gobo-agent` elsewhere), so every project after the first downloads
nothing. **goboscript** comes from gobo-agent's pinned prebuilt release (a fixed build
of upstream `main`, which carries the goboscript#158 negative-literal fix); a
`goboscript` on `PATH` still wins. Set `GSDEV_NO_AUTO_SETUP=1` to disable auto-install
and surface an error instead. Use the project's own launcher from then on; the VS
Code tasks `gobo-agent: *` call the same one.

### Submodule layout (git projects)

Preferred for git projects: the harness is a submodule at `gobo-agent/runtime`,
so updates are pinned to a commit.

```powershell
# Scaffold the project files and print the exact git command:
python <gobo>\tools\gsdev.py init --project <project> --layout submodule

# Then run the printed command, e.g.:
git -C <project> submodule add <gobo-agent-url> gobo-agent/runtime
git -C <project> submodule update --init --recursive gobo-agent/runtime

# Re-run init to finish once runtime/ is populated.
python <gobo>\tools\gsdev.py init --project <project> --layout submodule
```

`init --layout submodule` exits non-zero until the submodule exists, so a
partially-finished adoption is visible.

## What `init` changes

Created (project-owned; never overwritten without `--force-owned`):
`gobo-agent.json`, `gobo-tests.json`, `smoke.txt`, `gsdev_mode.gs`.

Created (generated; refreshed with `--force`): the `runtime/` copy, `VERSION`,
`.install.json`.

Merged (existing content preserved): `.gitignore` gains gobo-agent rules;
`.vscode/tasks.json` gains `gobo-agent:` tasks. Namespaced tasks are replaced on
re-run; unrelated tasks are left alone. (A merge rewrites the file as JSON, so
JSONC comments in an existing tasks file are not preserved.)

Re-running `init` is safe: unchanged files are reported and skipped, files you
edited are reported as conflicts and left untouched. An existing `gobo-agent/`
that is not a recognized install (missing/invalid `VERSION` + `.install.json`) is
refused rather than merged into.

Flags: `--project DIR`, `--harness DIR`, `--layout copy|submodule`,
`--harness-url URL`, `--dry-run`, `--force` (generated files),
`--force-owned` (also project-owned files).

## Configuration (`gobo-agent/gobo-agent.json`)

Resolution per value is **env → config → default**. Config *path* values are
relative to the config file's directory; env path values are relative to the
current directory; `modeInclude` is relative to the project root. The config is
discovered from `GSDEV_CONFIG`, else `<project>/gobo-agent.json`, else
`<harness>/../gobo-agent.json`.

```json
{
  "schema": 1,
  "project": "..",
  "harness": "runtime",
  "modeInclude": "gobo-agent/gsdev_mode.gs"
}
```

| key | env | default | meaning |
| --- | --- | --- | --- |
| `schema` | — | `1` | config format version |
| `project` | `GSDEV_PROJECT` | harness parent | the project under test |
| `harness` | — | `runtime` | runtime location (informational) |
| `state` | `GSDEV_STATE` | `<config dir>/state` | run state (port file, profiles) |
| `modeInclude` | `GSDEV_MODE_INCLUDE` | `tools/gsdev_mode.gs` | file rewritten per build |
| `prebuild` | `GSDEV_PREBUILD` | none | command run before each build |
| `env` | — | `{}` | extra environment for `prebuild` |
| `hostPort` | `GSDEV_HOST_PORT` | derived | host-page server port |
| `cdpPort` | `GSDEV_CDP_PORT` | derived | CDP port (`--port` overrides) |

In an adopted project, `hostPort`/`cdpPort` default to a stable pair derived from
the project path. Only one host session runs at a time; `close` it before starting
another. Other useful environment overrides: `GSDEV_PYTHON` (interpreter),
`GSDEV_GOBOSCRIPT` (compiler), `GSDEV_TOOLS`/`GSDEV_HOME` (tools root / shared
per-user home), `GSDEV_SB2GS` (use a specific sb2gs), `GSDEV_BROWSER`, `GSDEV_MODE`.

## Pre-build codegen (`prebuild`)

Some projects generate `.gs` includes before compiling. Set a prebuild command
(an argv list, or a shell-split string):

```json
{
  "prebuild": ["{python}", "tools/bake.py"],
  "env": { "BAKE_FAST": "1" }
}
```

It runs with `cwd` = the project root before every compile
(`build`/`run`/`screenshot`/`test`), with the resolved interpreter's directory on
`PATH`, `GSDEV_MODE`/`GSDEV_PROJECT` set, and these tokens substituted:
`{python}` (the interpreter), `{project}`, `{mode}`. A non-zero exit aborts the
build. It is skipped for data-only commands and with `--no-build`/`--no-reload`.

`doctor` flags a project that `%include`s paths that do not exist and has no
`prebuild` configured ("not build-ready").

## Tests: manifest + smoke

`init` scaffolds `gobo-agent/gobo-tests.json` and a metrics-first
`gobo-agent/smoke.txt`. Test `root` values resolve against the **project root**,
so the nested manifest targets the project with `root: "."`:

```json
{
  "schema": 1,
  "tests": [
    { "id": "smoke", "label": "smoke", "session": "gobo-agent/smoke.txt", "root": "." }
  ]
}
```

```powershell
<project>\gobo-agent\runtime\tools\gsdev.ps1 test --manifest <project>\gobo-agent\gobo-tests.json --list
<project>\gobo-agent\runtime\tools\gsdev.ps1 test --manifest <project>\gobo-agent\gobo-tests.json --headless
```

Author project-specific assertions by adding session files (input, waits,
`capture`/`expect`, `expect_no_errors`); the manifest may list several. The optional
VS Code Test Explorer extension (0.1.3+) discovers this nested manifest and the
project's launcher, so `smoke` appears in the Testing view; it uses a per-project
auto port.

## Importing a Scratch project (sb2gs)

Setup provides **sb2gs** (the Scratch → GoboScript importer), preferring an
`sb2gs` already on `PATH` (or `GSDEV_SB2GS`). Decompile a project id, then adopt
the result:

```powershell
# The parent directory must already exist for sb2gs.
New-Item -ItemType Directory -Force .\scratch | Out-Null
<gobo>\tools\gsdev.ps1 sb2gs --id 12345678 .\scratch\my_project.sb3
python <gobo>\tools\gsdev.py init --project .\scratch\my_project
```

Conversion is best-effort: some projects do not decompile. Always `goboscript
build` the result before trusting it. sb2gs needs Python 3.12+ (the Windows
portable is 3.12).

A project that was itself built by gobo-agent carries its `//` notes as calls to a
carrier proc whose definition was stripped; sb2gs decompiles them back to `// note`
lines. `sb2gs --verify` then reports a failure (it runs plain goboscript, which
rejects `//` by design) — that is expected, and not a conversion error. Build such a
project with `gsdev build`, which carries the notes through, and check with `run`.

An `--id` import also writes `provenance.json` at the project root (title,
author/username, project id, release date, instructions, notes and credits,
source and author URLs, retrieval time) so the original work stays credited — see
"Credit and provenance" in the repo `AGENTS.md`. Local-file imports have no
metadata, so no file is written.

Names change during conversion. The compiled project uses goboscript **identifiers**,
not the Scratch names. **sb2gs** applies the normalisation on import: whitespace/`.`/`-`/`:`
become `_`, any other symbol is dropped, leading **and** trailing `_` is stripped, and
`_`/a number is appended to avoid a keyword or a collision (gobo-agent also renames a
variable/list name clash, e.g. `x` → `x_list`) — but the author's **casing is kept**
(goboscript is case-sensitive and preserves what is written), so `camelCase` names and
`xOrigin: %s` argument names stay as written. So Scratch `Render._stickman` is
`Render.stickman` after import, while `gridY2` stays `gridY2`. When poking live values (`set`/`set_batch`) or reading with `get`, use
the compiled name — find it with `inspect`/`props` or from the decompiled `.gs`.

## Known upstream limitations (sb2gs / goboscript)

Conversion is best-effort and not guaranteed pixel-identical: always build, run, and compare (`gsdev run` + `screenshot`) rather than assuming equivalence. These are toolchain limits, not project bugs.

- **Invalid sprite filenames** — sb2gs writes `<sprite>.gs` without sanitising; `/`, Windows-reserved characters, and trailing dots/spaces fail. Rename those sprites in Scratch first.
- **Unsupported opcodes/inputs** — some blocks (for example backdrop-switch hats and color input fields) have no decompiler. sb2gs reports them and may leave blank expressions or invalid source.
- **Off-center costume pivots** — sb2gs re-canvases SVG and bitmap costumes to move the pivot to the image center, changing costume bytes/md5 and sometimes scale. goboscript has no rotation-center syntax, so projects that rely on an off-center pivot or Scratch's rotated-bounds behaviour may build but render blank even with the original assets preserved.
- **Comments** — line-leading `//` notes survive only through `gsdev build`; plain `goboscript build` rejects them, and `#` comments are compile-time only.
- **Compiler version** — the last upstream goboscript release rejects negative literals. `setup` installs a pinned prebuilt of upstream `main`; use `gsdev build` rather than another `goboscript` on `PATH`.
- **Large or complex projects** — prefer a local `.sb3` (`gsdev sb2gs <file.sb3>`). The `--id` downloader is bounded and cleans partial output on failure.

The harness always runs its pinned, patched sb2gs; a user `sb2gs` on `PATH` is only reported by `doctor`, never executed. Compatibility patches cover common failures such as the pen color menu, missing pivots, monitors, numeric coercion, and comments.

## Everyday loop, builds, profiling

The warm edit-and-check loop, `--mode debug|release`, input injection, screenshots,
and the profiler are identical to the repo's own workflow — see the
[README](../README.md) for the verb reference. The only difference is the launcher
path: `gobo-agent/runtime/tools/gsdev.*` instead of `tools/gsdev.*`.

`preflight` checks the built artifact (or `--sb3 FILE`) offline against Scratch's
project.json/asset/list limits and the mobile **memory** budget — no browser needed.
See the **Preflight** section below for the budget model and overrides.

## Preflight (memory + size budgets)

`preflight` scans a built `.sb3` offline (no browser). It exits non-zero on error findings, and with `--strict` on warnings; use `--sb3 FILE` to scan an artifact and `--json` for the full report. Without `--sb3` it builds first.

The estimate covers renderer memory (bitmap and SVG textures plus decoded audio), project-wide costume pixels, and a transient load peak. Defaults are a **0.85 GiB / 110M pixel smooth target** and a separate **1.6 GiB / 300M hard limit**; `deviceScale` defaults to 2, `deviceMemoryMb` scales from the 2 GB baseline, and thresholds are overridable through `GSDEV_PREFLIGHT_*` or `gobo-agent.json`. Alpha does not change the estimate.

This is a worst-case offline model, not a measurement: it counts every costume and retained MIP, infers size from saved and literal `set size to` values, and does not model the framebuffer or driver overhead. See the README's preflight section for the memory model and warning classes.

## SVG spritesheets for mobile memory

**Converting SVG costumes to bitmap is the primary fix** for an SVG-heavy project
(it removes the per-costume SVG canvas and the 4× pixel cost — see the preflight
output). Spritesheets are the alternative when you want to keep the **crisp
vector** look instead of a pixelated bitmap: packing many frames into one costume
cuts the costume/texture count while preserving the SVG art.

A project that plays many full-screen frames (video-like animation) crashes phones
for **two independent reasons**, both measured on-device with
[957967074](https://scratch.mit.edu/projects/957967074):

- **Raw SVG costumes hit a rasterised-pixel ceiling** (~10M px): 50 × 480×360
  survives (stutters), 100 × 480×360 crashes at ~55, while **1000 × 4×4 raw SVGs
  are smooth** — so for raw SVG it is the rasterised size, not the count.
- **base64 `<image>` costumes hit a per-costume ceiling** (~30–90) regardless of
  size: 156 costumes at 480×360 (116 MB) *and* at 48×36 (15 MB) both stutter then
  "Aw, Snap!".

The fix is therefore **fewer, packed costumes** — spritesheets. Verified: 156
frames → **10 costumes** (5×5 sheets) runs perfectly on Android, where the same
frames as 156 individual costumes crashed. The working build is published as
[scratch.mit.edu/projects/1388281452](https://scratch.mit.edu/projects/1388281452)
(the 480×360 form below; preflight 5,291,429 px → 0.10 GiB).

Per sprite:

1. **Crop each frame around its rotation centre.** What a sprite shows is the
   480×360 stage window centred on the costume's `rotationCenterX/Y` — *not* the
   canvas centre and *not* a `slice` crop (the subject is intentionally clipped,
   e.g. the bird's legs/tail). Crop each source SVG to
   `viewBox="(rcx-240) (rcy-180) 480 360"`.
2. **Pack 5×5 cells** (25 frames) per sheet: sheet `viewBox="0 0 480 360"`
   (cells 96×72), each an `<image width="96" height="72"
   href="data:image/svg+xml;base64,…">` of the cropped frame, and
   **`set_size 500;`**. Scratch rasterises an SVG at the drawable's size, so the
   sheet is sharp at 2400×1800 (the Android cap). The `<image>`/base64 wrapper is
   what makes the browser rasterise each cell at the display resolution — a plain
   small vector is rasterised *tiny* and upscaled, so it looks blurry. A 480×360
   sheet (cells 96×72, `set_size 500`) is a touch sharper than the 240×180 form
   (cells 48×36, `set_size 1000`) for about +1.3M px; both are sharp, so either
   works. (A baked `2400×1800` sheet at 100% is the CPU-cheap fallback.)
3. **Inject the pivot.** goboscript has no rotation-centre syntax and derives the
   pivot from the SVG content, which is wrong for a sheet. After `build`, set each
   sheet costume's `rotationCenterX/Y` to the sheet canvas centre — `240,180` for
   `480×360`, `120,90` for `240×180` — the same idea as sb2gs's `costumes.json` /
   `inject_costumes`. Then place cell `c` (0..24) with
   `x = 960 - 480 * (c mod 5)`, `y = 360 * floor(c / 5) - 720`.
4. **Keep the scripts.** Convert from the **sb2gs GoboScript** and rewrite only the
   costume switches: `switch_costume "costumeN"` →
   `frame = N; update_frame;`, `next_costume` → `frame += 1; update_frame;`, and
   `costume_number()` → `frame`, with
   `proc update_frame { switch_costume framecostume[frame]; goto framex[frame], framey[frame]; }`.
   Backdrop switches, `broadcast`/`on "…"` hats, waits and effects are untouched, so
   a broadcast-sequenced animation runs in the same order as the original.

## VS Code

Three things, often confused:

- **`gobo-agent: Run` task (Ctrl+Shift+B)** — the harness: builds the project and runs it
  in a real browser with live logs. This is what "run" means for gobo-agent.
- **`gobo-agent-tests` extension** (`vscode-extension/`) — the Test Explorer for
  `gobo-agent/gobo-tests.json`; discovery plus Headless/Visible runs, per-project auto port.
- **`aspizu.goboscript` extension** — editor support: syntax/grammar, build-on-save
  diagnostics, and a `.sb3` **preview**. It has **no debugger** — don't press F5/Run‑Debug.
  Build with gobo-agent, then open the built `.sb3` (double-click it) and use the preview's
  ▶/⏹/⟳ toolbar. Because `goboscript` is not on `PATH`, set `goboscript.compilerPath` to
  gobo-agent's pinned compiler so its build-on-save works, e.g.
  `"C:/Users/<you>/AppData/Local/gobo-agent/goboscript/goboscript.exe"` (the same
  `<tools-root>` as `GSDEV_TOOLS`/`GSDEV_HOME`). It coexists with the gobo-agent-tests
  extension.

## Troubleshooting

- **A command says the host is headless but you asked for headed (or vice
  versa).** A warm host keeps its mode. Pass the matching flag
  (`screenshot`/`profile` default to headed) or `close` it and start again.
- **"another host session is running".** Only one host is allowed. `close` the
  existing session; `--port` selects the port, it does not create a second host.
- **Wrong compiler version.** A `goboscript` on `PATH` wins over the pinned
  bundle. Set `GSDEV_GOBOSCRIPT` to force one; `doctor` reports which is used.
- **Port already in use.** `GSDEV_HOST_PORT`/`GSDEV_CDP_PORT` (or config
  `hostPort`/`cdpPort`) choose others; `--port 0` auto-picks a CDP port. Close the
  current session first if it is ours.
- **"host page did not finish loading" / "no debuggable page appeared".** The browser or
  its host server did not come up. A launch that yields no debuggable page is retried
  (`GSDEV_LAUNCH_ATTEMPTS`, default 2), and a retry keeps the host server alive; the failure
  message prints the page's own state, so read it — `location.href` of
  `chrome-error://chromewebdata/` means the browser could not reach the host server (a port
  conflict, or a blocked browser), while a missing `__host` with `readyState: complete`
  means the page loaded but its script never finished. Raise `GSDEV_HOST_TIMEOUT` (attach),
  `GSDEV_READY_TIMEOUT` (page ready, default 10 s) or `GSDEV_HOSTSERVER_TIMEOUT` (server
  ready line, default 2 s) for a slow machine.
- **`sb2gs` fails with `ModuleNotFoundError: No module named 'rich'`.** The installed sb2gs
  venv is incomplete (it presents as that import error from `run.py`). Rebuild it with
  `gsdev setup --only sb2gs --force`, which re-installs its wheels and re-applies
  gobo-agent's patches.
- **Generated files show in `git status`.** Ensure `.gitignore` kept the gobo-agent
  rules (`gobo-agent/state/`, `*.sb3`, `debug/`). `init` adds them.

## Updating the runtime

There is no in-place upgrade command yet. For the **copy** layout, re-run
`init --force` from an updated checkout to refresh the generated runtime files
(your config/manifest/smoke are preserved). For the **submodule** layout, bump
the submodule and commit the new pointer.

## Verification checklist

1. `doctor` is clean (python, goboscript, host bundles, sb2gs, build readiness).
2. `build` emits your `.sb3` at the project root.
3. `run --headless` streams `[LOG]` lines, then `close`.
4. `session --file gobo-agent/smoke.txt` passes.
5. `test --manifest gobo-agent/gobo-tests.json --headless` passes.
6. `git status` shows only intended files (no generated state).
