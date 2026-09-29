# gobo-agent

An automation and test harness for **GoboScript** projects running on the
**vanilla Scratch runtime**. It builds a project, runs it in a real browser, drives
the UI and VM, checks behavior, and measures performance — with no Scratch desktop
app, Electron, Node, Rust, or compiler toolchain required.

It is built for a fast, repeatable loop: a kept-warm browser, scripted unit tests,
live state inspection, and execution profiling.

## Capabilities

- **Build & run** a GoboScript project (`.gs` → `.sb3`) headlessly or visibly in
  Chrome/Edge/Chromium, on upstream `@scratch/scratch-vm` + `scratch-render`.
- **Drive the UI and VM** — click, mouse move/press/release, and key press/release,
  including character keys for project-defined text controls. Input is injected at
  the VM level; it does not automate Scratch's editor menus or native text fields.
- **Read/write live state** — variables, lists, properties, clones; `set_batch`
  writes several values in one evaluation.
- **Assert behavior** — session files with `expect`/`expectpixel`, plus `errors` and
  `expect_no_errors` (covers VM/page exceptions and goboscript `error` logs).
- **Inspect & capture** — `inspect`, `props`, `prop`, `clones`, `broadcast`,
  `record`, `trace`, `until … --pause`, stage screenshots, and pixel readback.
- **Check on real Scratch** — `tools/gsbridge.py` runs the same style of verbs
  against the GitHub player, the production editor, or a production embed.
- **Measure activity** — live `perf` pseudo-selectors (`@fps`, `@stepfps`,
  `@rendertime`, `@steptime`, `@rendered`, `@gpu`, …) and a procedure
  profiler (`profile`, `profiling`) that ranks hot procedures per VM step.

## Choose the tool

| Tool | Purpose |
| --- | --- |
| `tools/gsdev.py` | Local build/run loop, unit and functional tests, live tuning, perf inspection |
| `tools/gsbridge.py` | Same-style verbs on the GitHub player or production Scratch |

Host internals (the static host page and pinned `@scratch/*` bundles) live in
`tools/scratchhost/`. Iteration guidance is in [AGENTS.md](AGENTS.md).

## Requirements

- **Python 3.10+** (standard library only; no pip steps).
- **Chrome, Edge, or Chromium.** Edge is preferred and normally already present on
  Windows; `GSDEV_BROWSER` overrides the executable.
- **No admin rights.** Downloads and executable launches remain subject to machine
  policy; setup never bypasses it.

## Install

Windows (works with no Python installed — it fetches a portable interpreter):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\gsdev.ps1 doctor
```

Anywhere with Python:

```sh
python tools/gsdev.py setup
python tools/gsdev.py doctor
```

macOS/Linux launchers (mirror the PowerShell ones; no admin needed, and a
Chromium-based browser is expected to be installed — Safari is out of scope):

```sh
./setup.sh                        # find Python 3.10+, then install the tools
./tools/gsdev doctor              # resolve Python and run the dev loop
./setup.sh --only vendor --force  # other bootstrap flags pass straight through
```

`GSDEV_PYTHON` overrides the interpreter; otherwise the launchers check
`.tools/python/bin/python3` (or `GSDEV_PYTHON_DIR`) and then `python3`/`python`
on `PATH`, requiring 3.10+ in every case.

The VS Code tasks (`.vscode/tasks.json`) use the same launchers — `./setup.sh` and
`./tools/gsdev` on macOS/Linux, `setup.ps1`/`gsdev.ps1` on Windows — so Setup, Build,
Run, Screenshot, Stop, Close, and Check dependencies behave identically on both.
`python tools/gsdev.py tasks [--run]` verifies that resolution for the current OS.

Setup downloads the pinned prebuilt goboscript compiler and the AGPL-3.0-only
`@scratch/*` browser bundles (gitignored). PowerShell equivalents are `-Only`,
`-Force`, and `-Offline`; `--offline` reports a missing Python immediately instead of
downloading.

## Sample scenarios

**1. First run**

```powershell
python tools/gsdev.py run --headless --duration 3 --leave-running   # build, load, stream logs
python tools/gsdev.py close
```

**2. Edit-and-check loop (keep the browser warm)**

```powershell
python tools/gsdev.py run --headless --duration 2 --leave-running
python tools/gsdev.py set_batch main.tx=120 main.ty=0               # live data, no rebuild
# ...edit .gs...
python tools/gsdev.py run --headless --duration 2 --leave-running   # rebuild + reload
python tools/gsdev.py close
```

**3. A repeatable unit test** — save `tests.txt`:

```text
key space down
waitframe 4
key space up
expect main.keys >= 1
set main.tx 120
waitpixel 120 0 #4c97ff 3000
expect_no_errors
```

```powershell
python tools/gsdev.py run --headless --duration 2 --leave-running
python tools/gsdev.py session --file tests.txt          # exits non-zero on failure
python tools/gsdev.py test tests.txt --headless --json  # builds each file's project
```

**4. Debug live state**

```powershell
python tools/gsdev.py inspect main
python tools/gsdev.py get main.tx "@stepfps"
python tools/gsdev.py watch main.tx --duration 2
python tools/gsdev.py errors
python tools/gsdev.py profile --seconds 2 --headless --json debug/profile.json  # streams + summary
python tools/gsdev.py profile --no-follow --seconds 2 --headless   # summary only
python tools/gsdev.py setprofiling on                # silent toggle; read it with:
python tools/gsdev.py profiling --steps 5            # current report (no new capture)
python tools/gsdev.py profile --wait-until Tick.framecount --wait-op '>' --wait-value 0
                                                     # start the window at an event (load/setup excluded)
python tools/gsdev.py setprofiling on; python tools/gsdev.py profilereset   # manual window start
python tools/gsdev.py profile --seconds 2 --children _3dEngine  # that procedure's callees
python tools/gsdev.py profile --seconds 2 --sort incl          # rank by whole subtree
```

**5. Screenshot and pixel**

```powershell
python tools/gsdev.py screenshot --headless --out debug/frame.png
python tools/gsdev.py pixel 0 0
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

## Non-ASCII (Japanese) text

Use UTF-8 session files with `session --file` / `test`. Windows PowerShell 5.1
*pipes* are not UTF-8 and can destroy CJK text before Python receives it, so avoid
piping non-ASCII input. JSON may print `\uXXXX` escapes — valid and reversible.

## Configuration

| Setting | Purpose |
| --- | --- |
| `GSDEV_PROJECT` | Project directory (default: this repository) |
| `GSDEV_PYTHON`, `GSDEV_PYTHON_DIR` | Windows interpreter / portable install directory |
| `GSDEV_GOBOSCRIPT`, `GSDEV_TOOLS` | Compiler override / bundled-tools directory |
| `GSDEV_BROWSER` | Browser executable |
| `GSDEV_CDP_PORT`, `--port` | Local CDP port (default 9230); `--port 0` picks one |
| `GSDEV_HOST_PORT` | Local static-server port (default 8077) |
| `GSBRIDGE_PORT` | Bridge CDP port (default 9400) |
| `GSDEV_SOFTWARE=1` | Force software rendering |
| `GSDEV_NO_AUTO_SETUP=1` | Never auto-download dependencies |

## Troubleshooting

| Problem | Next step |
| --- | --- |
| Missing dependencies | Run setup, then `doctor` |
| Python opens the Microsoft Store | Use `setup.ps1` / `tools\gsdev.ps1` |
| Scripts or executables blocked | Machine policy applies; execution-policy bypass is not enough |
| Download/TLS failure | Check access to python.org, GitHub releases, and the npm registry |
| Host port occupied | Set `GSDEV_HOST_PORT`; do not kill an unrelated service |
| Browser modal covers the project | Discard timing; inspect the dialog and launcher output |
| Source changes appear ignored | Rebuild `.gs`; relaunch after host JavaScript changes |

## Validation and layout

`doctor` checks dependencies; `selftest` checks the host configuration;
`tasks --run` checks VS Code tasks. Functional coverage starts with the root project
and `smoke.txt`; `examples/` and `tests/` are local-only, gitignored material.
Broad Windows/macOS CI smoke is not proof for every machine policy or locale — the
strictest check is a clean, non-elevated standard-user install on Japanese Windows.

## License

MIT (see [LICENSE](LICENSE)): use, modify, and redistribute freely, with no copyleft
obligation. The compiler and browser bundles that `setup` downloads are **not** part of
this repository and keep their own upstream licenses — goboscript and the
`@scratch/*` packages (AGPL-3.0-only) are fetched into gitignored directories, so the
MIT grant here covers only gobo-agent's own source.
