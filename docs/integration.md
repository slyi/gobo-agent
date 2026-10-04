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
- Python. On Windows you need none — `setup` fetches a portable Python 3.14. On
  macOS/Linux you need Python 3.10+ (3.14+ if you want the Scratch importer).
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
the project path, so two adopted projects do not collide. Other useful
environment overrides: `GSDEV_PYTHON` (interpreter), `GSDEV_GOBOSCRIPT`
(compiler), `GSDEV_TOOLS`/`GSDEV_HOME` (tools root / shared per-user home),
`GSDEV_SB2GS` (use a specific sb2gs), `GSDEV_BROWSER`, `GSDEV_MODE`.

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
build` the result before trusting it. sb2gs needs Python 3.14+ (the Windows
portable is 3.14).

An `--id` import also writes `provenance.json` at the project root (title,
author/username, project id, release date, instructions, notes and credits,
source and author URLs, retrieval time) so the original work stays credited — see
"Credit and provenance" in the repo `AGENTS.md`. Local-file imports have no
metadata, so no file is written.

Names change during conversion. The compiled project uses goboscript **identifiers**,
not the Scratch names: goboscript lowercases a name, strips leading **and** trailing
`_`, maps whitespace/`.`/`-`/`:` to `_`, drops any other symbol, and appends `_`/a
number to avoid a keyword or a collision (gobo-agent also renames a variable/list
name clash, e.g. `x` → `x_list`). So Scratch `Render._stickman` is `Render.stickman`
after import. When poking live values (`set`/`set_batch`) or reading with `get`, use
the compiled name — find it with `inspect`/`props` or from the decompiled `.gs`
(the "gobo-agent patches" list below is where such renames are explained).

## Known upstream limitations (sb2gs / goboscript)

Recorded here so adopters know what to expect. These are upstream behaviours; we
don't fix them and don't file bugs for them. Issue links are for reference.
Conversion is not guaranteed pixel-identical: always `goboscript build`, then run
and compare (e.g. `gsdev run` + `screenshot`) rather than assuming equivalence.

**sb2gs**

- **Sprite names that aren't valid filenames** — each sprite is written as
  `<name>.gs` (`decompile.py:64`), unsanitized. `/` fails on every OS; on Windows
  `* : < > ? |` (and a trailing `.`/space) fail too. Costume/sound names are
  handled (renamed, or hash-named on Windows) by `get_asset_filename`. Upstream:
  [#23](https://github.com/aspizu/sb2gs/issues/23) (sprites, closed not-planned),
  [#4](https://github.com/aspizu/sb2gs/issues/4) (assets). There is no upstream
  fix; use a project without such names, or rename them there.
- **Misc unsupported opcodes/inputs** — e.g. `event_whenbackdropswitchesto`
  ([#25](https://github.com/aspizu/sb2gs/issues/25)), color input fields
  ([#5](https://github.com/aspizu/sb2gs/issues/5)), float→int
  ([#13](https://github.com/aspizu/sb2gs/issues/13)).
- **Vector costumes with an off-center rotation center are rewritten** —
  `fix_vector_center` (`costumes.py`) replaces such an SVG with a 480×360
  stage-sized SVG and drops its `<g transform>`, so the costume's bytes (and md5)
  change and any layout that relied on the transform is lost. This is sb2gs's
  workaround for goboscript having no rotation-center syntax (below), so the md5
  will not match the source project's costume. gobo-agent patches the check to a
  0.5-unit tolerance, so a merely rounded "centre" (e.g. 1015495507's Cat, 0.41
  off) is left alone; a genuinely off-centre pivot (e.g. RBMP) is still rewritten.
- **Bitmap costumes with an off-center rotation center are rewritten** —
  `fix_bitmap_center` (`costumes.py`) pastes the image onto a 960×720 canvas so
  the original pivot lands at the canvas centre, changing the costume's bytes
  (and md5). It **ignores `bitmapResolution`** (and goboscript compiles costumes
  without it), so 2× bitmaps are re-canvased at the wrong scale — on the
  texture-atlas project 1047137851 the tiles survived while the font text
  rendered scrambled. Only genuinely off-centre pivots are affected (the
  0.5-unit/±1-pixel tolerance above leaves rounded centres alone).

**goboscript** (handled by gobo-agent's bundled build)

- **Costume rotation centers can't be set** — the `costumes` statement takes only
  file paths/`as`/globs (no rotation-center option), and the compiled costume
  omits `rotationCenterX`/`rotationCenterY`/`bitmapResolution`; the Scratch VM
  then falls back to the rendered skin's center (`scratch-vm` `loadCostume`), so
  every costume rotates about its image center. This is upstream
  [#269](https://github.com/aspizu/goboscript/issues/269) (closed *not planned*:
  the maintainer's answer is to convert costumes with sb2gs — i.e. the
  `fix_vector_center` step above). Projects that depend on an off-center pivot or
  on Scratch's rotated-bounding-box quirk (e.g. RBMP packing,
  [project 1387642307](https://scratch.mit.edu/projects/1387642307)) cannot be
  reproduced — the decompiled project builds but renders blank, even if the
  original SVGs are preserved.
- Negative number literals are rejected by the last *release* (3.2.1; upstream
  #158 fixed only on `main`) — gobo-agent installs a pinned prebuilt of `main`.
- The standard-library update calls the GitHub API when its cache is stale;
  gobo-agent's prebuilt is patched to fall back offline.

**gobo-agent patches to the installed sb2gs** (applied by `setup`):

- **`--id` downloader** — one pooled HTTP connection (`GSDEV_SB2GS_WORKERS`,
  default 16), a content-addressed asset cache
  (`%LOCALAPPDATA%\gobo-agent\asset-cache`; `GSDEV_SB2GS_CACHE` to relocate,
  `GSDEV_SB2GS_NO_CACHE=1` to disable), retries with backoff, the endpoint's
  zip-wrapped/raw project data, and the sb2gs-required defaults a sparse save
  omits. Large imports are ~20× faster and re-imports are offline.
- **Pen color-parameter menu** — sb2gs read the `COLOR_PARAM` menu as a block
  field, so every `pen_setPenColorParamTo`/`...By` collapsed to the default
  `*_hue` block and brightness/saturation/transparency were lost (e.g. 1127053411
  rendered inverted). The patch flattens the menu so the right block is emitted.
- **Missing costume pivot** — newer saves omit `rotationCenterX`/`rotationCenterY`
  (the VM then uses the skin centre), which sb2gs dereferenced directly, so the
  import failed with `AttributeError`. The patch treats a missing pivot as
  centred, so those projects import (verified: 1307268012).
- **Initial costume** — sb2gs ignored `currentCostume`, so a sprite started on the
  first listed costume instead of the saved one (wrong for a static costume, e.g.
  1015495507's `cat-chess`, whose first listed costume is empty). The patch emits
  `onflag { switch_costume "<name>"; }` for the saved costume — a bare
  `switch_costume` isn't a valid top-level sprite-init statement. Verified: the
  cat-chess floor returns.
- **Drag mode** — sb2gs emitted a nonexistent `set_draggable;`
  (`decompile_sprite.py:81`) for a draggable sprite, which goboscript rejects. The
  patch emits `onflag { set_drag_mode_draggable; }` instead. Verified: 1047137851's
  atlas builds.
- **`control_for_each`** — sb2gs had no decompiler and dropped the loop body
  (upstream [#15](https://github.com/aspizu/sb2gs/issues/15)). The VM treats it as
  a counted loop (variable = 1-based index, run `Number(VALUE)` times), so the
  patch lowers it to `<var> = 0; repeat <VALUE> { <var> += 1; <body> }`. Verified:
  1291298236 now builds (347 vs 72 blocks) and runs its scanner. Caveat: the
  counter *is* the loop variable, so a body that writes that variable diverges
  (the VM re-assigns it each iteration; the lowered loop would be disturbed).
- **Same-named variables/lists** — sb2gs emitted both `var x` and `list x`, or a
  duplicate `var x`/`list x`, on one target, which goboscript rejects ("already
  defined"); its identifier cache maps two identical source names to one
  identifier. The patch renames the colliding entries before decompiling (in the
  variable/list tables and every block field/input that references their id).
  Verified: 1193224850 now builds (2041 blocks).
- **Monitors** — sb2gs ignored the project's `monitors` array, so on-stage
  variable/list readouts lost their positions and slider modes. sb2gs now records
  each monitor (with its compiled variable/list name) in a **`monitors.json`**
  sidecar at the project root, and `build`/`run`/`screenshot`/`test` re-inject the
  full `monitors` array into the **built sb3** (`inject_monitors` in `gsdev.py`),
  mapping each by name to the compiled id — restoring positions, slider
  mode/min-max and visibility. Verified: 1113180719's positioned readouts and the
  `Render: stickman` slider return. Labels show the compiled identifier (`camz`,
  not `@camZ`), since that is the runtime name. A per-target
  `onflag { show/hide }` script is still emitted as a visibility fallback for a
  build made without the injection.

**gobo-agent mitigations:** for large or known-problematic projects, download the
`.sb3` in a browser and pass the local file (`gsdev sb2gs <file.sb3>`);
`gsdev sb2gs` bounds the run and cleans partial `.sb3`/output on failure.

## Everyday loop, builds, profiling

The warm edit-and-check loop, `--mode debug|release`, input injection, screenshots,
and the profiler are identical to the repo's own workflow — see the
[README](../README.md) for the verb reference. The only difference is the launcher
path: `gobo-agent/runtime/tools/gsdev.*` instead of `tools/gsdev.*`.

`preflight` checks the built artifact (or `--sb3 FILE`) offline against Scratch's
project.json/asset/list limits and the mobile **memory** budget — no browser needed;
`--json` for a machine report, `--strict` to exit non-zero on warnings too. It
reports exact numbers (e.g. the binary 5 MiB project.json budget) rather than
"5 MB", and flags unmeasured assets so a pass can't hide them.

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
- **"host belongs to project …" / "different gobo-agent build".** A host is never
  reused across projects or harness versions. Use `--port 0` (or another
  `--port`), or `close --port N`.
- **Wrong compiler version.** A `goboscript` on `PATH` wins over the pinned
  bundle. Set `GSDEV_GOBOSCRIPT` to force one; `doctor` reports which is used.
- **Port already in use.** `GSDEV_HOST_PORT`/`GSDEV_CDP_PORT` (or config
  `hostPort`/`cdpPort`) choose others; `--port 0` auto-picks a CDP port.
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
