# gobo-agent

A dependency-free dev loop for GoboScript, with a minimal "Hello, World!" as the
example project, so a coding agent (or you) can **build, read live debugger logs,
and screenshot the stage** on **Windows and macOS**.

There is no Bun, Node, npm, pip, Puppeteer, or bridge bundle. Everything is Python
standard library plus two external programs you already need.

If you are a coding agent (or want the shortest path), see [AGENTS.md](AGENTS.md):
the fastest warm/cold loop, measured timings, and the flags that matter.

## What is here

```
gobo-agent/
├── assets/            hello + backdrop SVG costumes
├── main.gs            says "Hello, World!" and logs every 30 frames
├── stage.gs           backdrop
├── goboscript.toml    frame rate and stage size
├── tools/
│   ├── cdp.py         tiny stdlib Chrome DevTools Protocol client (WebSocket)
│   └── gsdev.py       build / run / screenshot / stop / close / status CLI
└── .vscode/tasks.json VS Code build tasks (Ctrl+Shift+B runs with live logs)
```

## Prerequisites

| Tool | Why | Get it |
| --- | --- | --- |
| Python 3.10+ | runs the dev loop | python.org |
| [GoboScript](https://github.com/aspizu/goboscript) | compiles `.gs` to `.sb3` | its installer |
| [Scratch Desktop](https://scratch.mit.edu/download) | runs the project (default) | installer / store |
| [TurboWarp Desktop](https://desktop.turbowarp.org/) | alternative backend | store / dmg |

On Windows, process and port handling uses the standard library's `ctypes`
(Toolhelp32 + `GetExtendedTcpTable`), so **no PowerShell, WMI, taskkill, or
netstat is required**. On macOS/Linux it uses `ps` (plus `pgrep`/`lsof`/`fuser`
where present). VS Code is optional and only needed for the `Ctrl+Shift+B` tasks.

Run the dependency check any time:

```powershell
python tools/gsdev.py doctor
```

It prints one line per dependency (`ok` / `warn` / `FAIL`), lists any environment
overrides, and exits non-zero if something required is missing.

**Platform support:** Windows (Scratch + TurboWarp) and Linux — including
display-less Linux (TurboWarp) — are smoke-tested. macOS has not been run on
real hardware: the platform-neutral code and the macOS path/flag logic are
covered by `selftest`, but launching the `.app` and driving it over CDP is
unverified. `.github/workflows/macos-smoke.yml` runs
`doctor`/`selftest`/`build`/`run`/`screenshot` on a `macos-14` runner so you can
confirm it without owning a Mac.

The default backend is **Scratch Desktop**. Set `GSDEV_BACKEND=turbowarp` to use
TurboWarp instead:

```powershell
$env:GSDEV_BACKEND = "turbowarp"
python tools/gsdev.py run
```

## Scratch Desktop backend

Scratch installs as a Microsoft Store package (`Scratch 3.exe` exits unless
launched through its app id) or, preferably, from the direct installer at
`C:\Program Files (x86)\Scratch 3\Scratch 3.exe`. The tool finds either; override
with `$env:SCRATCH_EXE`. In a dedicated profile (`tools/scratch-profile/`,
gitignored) it pre-writes the telemetry opt-out so Scratch's share-data modal
never covers the stage.

Scratch Desktop cannot load browser extensions (the Scratch Addons debugger
included), so the tool replicates it: because the app runs with `nodeIntegration`
and keeps its VM in the React tree, `gsdev.py` finds the VM and hooks
`procedures_call` to capture GoboScript's `log`/`warn`/`error` blocks. `run`
therefore streams `[LOG ...]` lines, and `screenshot`/`status` work as usual.

## TurboWarp backend

On first run the tool enables TurboWarp's built-in **Debugger addon** in an
isolated profile (`tools/turbowarp-profile/`, gitignored) so the native
`log`/`warn`/`error` blocks work. Your normal TurboWarp profile is untouched.

If TurboWarp is not found automatically, point the tool at it:

```powershell
$env:TURBOWARP_EXE = "C:\path\to\TurboWarp.exe"
```

```bash
export TURBOWARP_EXE="/Applications/TurboWarp.app/Contents/MacOS/TurboWarp"
```

## VS Code tasks

Run **Tasks: Run Task**, or use the shortcuts:

- **gobo-agent: Run (Scratch, build + live logs)** — default build task
  (`Ctrl+Shift+B`). Builds, opens Scratch Desktop, starts the project, and
  streams `[LOG ...]` lines into the terminal. Press `Ctrl+C` to stop.
- **gobo-agent: Run (TurboWarp, build + live logs)** — same, using TurboWarp.
- **gobo-agent: Build** — compile only.
- **gobo-agent: Screenshot** — writes `debug/stage.png` (stage only, no editor UI).
- **gobo-agent: Screenshot (TurboWarp)** — same, using TurboWarp.
- **gobo-agent: Stop project** — stop play, keep the editor open.
- **gobo-agent: Close editor** — close the isolated window.
- **gobo-agent: Check dependencies** — run the `doctor` command.

## Command line

```powershell
python tools/gsdev.py run                 # build + start + stream logs
python tools/gsdev.py run --duration 5    # exit after 5 seconds
python tools/gsdev.py run --no-build      # reuse the existing .sb3
python tools/gsdev.py run --no-reload     # reuse the current editor page
python tools/gsdev.py run --cpu 4         # emulate a 4x slower CPU (phone)
python tools/gsdev.py run --headless      # no visible editor window
python tools/gsdev.py run --port 0        # auto-pick a free CDP port
python tools/gsdev.py screenshot --out debug/hello.png --delay 1500
python tools/gsdev.py screenshot --cpu 6  # slow device capture
python tools/gsdev.py screenshot --headless
python tools/gsdev.py build
python tools/gsdev.py status              # sprites and run state
python tools/gsdev.py stop
python tools/gsdev.py close
python tools/gsdev.py doctor              # check required tools are installed
python tools/gsdev.py selftest            # check per-platform path/flag logic
```

`selftest` simulates the win32/darwin/linux branches (install paths and headless
flags) so you can validate the macOS assumptions without a Mac. For a real macOS
check, run `doctor`/`selftest`/`build`/`run` on a Mac or a GitHub Actions
`macos-latest` runner.

## How log capture works

1. The editor is launched once with `--remote-debugging-port=9223` and a
   dedicated `--user-data-dir`, so it can be scripted without touching your setup.
2. `tools/cdp.py` connects to the editor page over the Chrome DevTools Protocol
   using a hand-written WebSocket (no `websockets` package needed).
3. For **TurboWarp**, the tool sets `debugger` in the `tw:addons` localStorage
   key, which registers the native `log`/`warn`/`error` blocks, then reloads
   once, and patches those blocks' callbacks.
   For **Scratch Desktop**, it finds the VM in the React tree (Scratch exposes no
   `window.vm`) and hooks `procedures_call` to intercept the same blocks.
4. Captured messages go into a page queue that Python drains and prints.
5. Screenshots use `Page.captureScreenshot` clipped to the stage canvas rect, so
   only the stage is captured.

The TurboWarp path is the same mechanism `goboscript-mcp` uses, minus its Bun
bridge server: the scripting client talks to TurboWarp directly.

## Notes and limits

- `--cpu RATE` (or `GSDEV_CPU`) emulates a slower device by calling CDP
  `Emulation.setCPUThrottlingRate`, e.g. `--cpu 4` for a mid-range phone. It
  throttles the renderer, not the GPU, and Chromium's throttling is approximate.
- `--headless` launches the editor with no visible window. Electron has no true
  headless mode: on Windows `--headless` creates the `BrowserWindow` hidden; on
  Linux the tool adds `--ozone-platform=headless`, and with no
  `DISPLAY`/`WAYLAND_DISPLAY` also `--use-angle=swiftshader
  --enable-unsafe-swiftshader` so TurboWarp's WebGL renderer still initializes.
  Verified headless screenshots on Windows (Scratch and TurboWarp) and on
  display-less Linux (TurboWarp).
- Screenshots for TurboWarp use `vm.runtime.renderer.requestSnapshot`, which
  reads the stage back from the GPU and is unaffected by the hidden window's
  layout; Scratch uses `Page.captureScreenshot` clipped to the stage canvas.
  The snapshot path exists because `--disable-gpu` would leave no renderer, so
  the tool prefers software GL instead.
- The editor window must be visible (not minimized) for screenshots when not
  using `--headless`.
- A tight `forever` loop with no `wait` can spin very fast and flood the log
  stream. `main.gs` waits one second per iteration, so it logs about once per
  second. Add `wait 1;` in any loop that logs.
- A cold launch loads the build natively from the command line. A warm TurboWarp
  run injects the rebuilt project over CDP instead of reloading, because
  TurboWarp's unsaved-changes prompt would block a page reload. Use `--no-reload`
  to skip refreshing entirely for faster retries.
- `close` always force-kills the isolated process. It never calls `Browser.close`
  (which raises the editor's "are you sure you want to quit" dialog), and it kills
  the root process first so Chromium does not flash its "Crashed" dialog when it
  sees a renderer die before the browser process.
- `log`/`warn`/`error` are the TurboWarp/Scratch Addons debugger blocks, not
  plain Scratch palette blocks. Both backends intercept the same proccodes, so
  they work without adding the blocks to the project.
- The `.sb3` is passed to the editor as a command line argument, so the editor
  loads it natively and re-reads it from disk on each reload. A window that was
  opened without a file falls back to injecting the project over CDP.
- Change the CDP port with `--port` or the `GSDEV_CDP_PORT` environment variable
  if 9223 is taken. `--port 0` auto-picks a free port and remembers it in
  `tools/.gsdev-port` (gitignored) so `status`/`stop`/`close` find the same
  editor.
- Parallel A/B runs: give each run its own port (`--port 0`) and its own project
  directory (a worktree or copy) so they get separate profiles and `.sb3` files.
  Verified on Windows and on display-less Linux — two headless editors, two CDP
  connections, independent logs, and each `close --port 0` kills only its own.
- If you launch the tool from a terminal that sets `ELECTRON_RUN_AS_NODE` (VS
  Code's extension host does), the tool clears it for the editor process so the
  Electron app still starts as a GUI.
