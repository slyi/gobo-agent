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

Conversion is best-effort upstream: some projects do not decompile (unsupported
opcodes, or sprite names that are not valid filenames). Always `goboscript build`
the result before trusting it. sb2gs needs Python 3.14+ (the Windows portable is
3.14).

## Everyday loop, builds, profiling

The warm edit-and-check loop, `--mode debug|release`, input injection, screenshots,
and the profiler are identical to the repo's own workflow — see the
[README](../README.md) for the verb reference. The only difference is the launcher
path: `gobo-agent/runtime/tools/gsdev.*` instead of `tools/gsdev.*`.

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
