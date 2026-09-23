#!/usr/bin/env python3
"""gobo-agent: cross-platform GoboScript dev loop for VS Code tasks and agents.

Everything runs through Python's standard library plus an offline editor
(TurboWarp Desktop or Scratch Desktop) and the GoboScript compiler. There is no
Bun, Node, npm, pip, or bridge bundle to install.

Set GSDEV_BACKEND=turbowarp to drive TurboWarp Desktop instead of the default
Scratch Desktop. A cold launch hands the build to the editor as a command line
argument so it loads natively and the editor's own startup project load is never
raced. A warm run refreshes by injecting the rebuilt project over the Chrome
DevTools Protocol, which avoids the editor's unsaved-changes unload prompt.

Requires (run `gsdev.py doctor` to check them):
    python 3.10+            the only runtime; no pip packages
    goboscript              the compiler, on PATH
    Scratch Desktop         default editor (direct install or Store package)
    TurboWarp Desktop       optional alternative editor
    VS Code                 optional; only for the Ctrl+Shift+B tasks

On Windows, process and port handling uses the standard library's ctypes
(Toolhelp32 + GetExtendedTcpTable), so no PowerShell, WMI, taskkill, or netstat
is required. On macOS/Linux it falls back to the usual ps/pgrep/lsof/fuser.

Commands:
    build       Compile the project to <project>.sb3
    run         Build, open in the editor, and start the project
    screenshot  Build, run briefly, and capture the stage to a PNG
    stop        Stop the running project, leaving the editor open
    close       Force-kill the isolated editor process (no save dialog)
    status      Print the sprites in the open editor
    doctor      Check that the required tools are installed
    selftest    Check the per-platform path/flag logic (covers macOS)

Pass --cpu RATE (or set GSDEV_CPU) to emulate a slower device, e.g. --cpu 4
approximates a mid-range phone. It throttles the editor's renderer through CDP,
so the project itself runs slower without changing the .sb3.

Pass --headless to launch the editor with no visible window. Electron has no
real headless mode: on Windows --headless creates the window hidden, and on
Linux the tool adds --ozone-platform=headless (plus software GL when no
DISPLAY/WAYLAND_DISPLAY is set) so it runs on a display-less server or CI.

Pass --port 0 (or set GSDEV_CDP_PORT=0) to auto-pick a free CDP port and record
it in tools/.gsdev-port for later commands, so multiple headless runs can A/B
test in parallel without their CDP connections colliding.

Both backends stream the project's log/warn/error blocks. TurboWarp does it by
enabling its built-in Debugger addon in a dedicated profile and patching the
native block callbacks. Scratch Desktop cannot load the Scratch Addons browser
extension, so the tool instead finds the VM in the React tree and hooks
procedures_call, which captures the same messages. For Scratch the dedicated
profile is pre-seeded with the telemetry opt-out so its share-data modal never
covers the stage.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import (  # noqa: E402
    CDP,
    CDPError,
    connect,
    find_editor_target,
    list_targets,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SB3_PATH = PROJECT_ROOT / (PROJECT_ROOT.name + ".sb3")
PROFILE_DIR = PROJECT_ROOT / "tools" / "turbowarp-profile"
SCRATCH_PROFILE_DIR = PROJECT_ROOT / "tools" / "scratch-profile"
DEBUG_DIR = PROJECT_ROOT / "debug"
DEFAULT_PORT = int(os.environ.get("GSDEV_CDP_PORT", "9223"))
PORT_FILE = PROJECT_ROOT / "tools" / ".gsdev-port"
BACKEND = os.environ.get("GSDEV_BACKEND", "scratch").strip().lower()

# TurboWarp stores addon toggles in localStorage under this key. Enabling the
# Debugger addon registers the native log/warn/error blocks and their palette.
ENABLE_DEBUGGER_JS = r"""(() => {
  let addons;
  try { addons = JSON.parse(localStorage.getItem('tw:addons') || '{"_":5}'); }
  catch (error) { addons = {}; }
  if (addons.debugger && addons.debugger.enabled) return false;
  addons.debugger = Object.assign({}, addons.debugger, {enabled: true});
  localStorage.setItem('tw:addons', JSON.stringify(addons));
  return true;
})()"""

# Patch the addon block callbacks so every log/warn/error block appends to a
# page-level queue that Python drains. Mirrors goboscript-mcp's bridge host.
INSTALL_SHIM_JS = r"""(() => {
  const g = (window.__gsdev = window.__gsdev || {});
  g.logs = [];
  g.stopped = false;
  if (typeof vm === 'undefined' || !vm || !vm.runtime) return JSON.stringify({fatal: 'no runtime'});
  if (typeof vm.runtime.getAddonBlock !== 'function') {
    return JSON.stringify({fatal: 'getAddonBlock unavailable'});
  }
  const Z = '\u200B\u200B';
  const patch = (opcode, level) => {
    const block = vm.runtime.getAddonBlock(opcode);
    if (!block || typeof block.callback !== 'function') return false;
    const original = block.callback.__gsdev ? block.callback.__gsdevOriginal : block.callback;
    const wrapped = function (...args) {
      let sprite = 'unknown';
      try {
        const runtimeArg = args[1];
        const target = (runtimeArg && runtimeArg.thread && runtimeArg.thread.target)
          || (vm.runtime.thread && vm.runtime.thread.target);
        if (target && target.sprite) sprite = target.sprite.name;
      } catch (error) {}
      let value = args[0];
      if (value && typeof value === 'object' && 'content' in value) value = value.content;
      if (typeof value !== 'string') {
        try { value = JSON.stringify(value); } catch (error) { value = String(value); }
      }
      g.logs.push({
        sprite: sprite === 'Stage' ? 'stage' : sprite,
        level: level,
        value: String(value),
        time: Date.now()
      });
      return original.apply(this, args);
    };
    wrapped.__gsdev = true;
    wrapped.__gsdevOriginal = original;
    block.callback = wrapped;
    return true;
  };
  const patched = {
    log: patch(Z + 'log' + Z + ' %s', 'log'),
    warn: patch(Z + 'warn' + Z + ' %s', 'warn'),
    error: patch(Z + 'error' + Z + ' %s', 'error')
  };
  if (!g.stopHooked) {
    g.stopHooked = true;
    try { vm.on('PROJECT_RUN_STOP', () => { g.logs.push(null); g.stopped = true; }); } catch (error) {}
  }
  return JSON.stringify(patched);
})()"""

DRAIN_LOGS_JS = (
    "JSON.stringify(window.__gsdev.logs.splice(0, window.__gsdev.logs.length))"
)
IS_STOPPED_JS = "!!(window.__gsdev && window.__gsdev.stopped)"
STAGE_RECT_JS = (
    "(() => { const canvas = vm.runtime.renderer.canvas; if (!canvas) return null;"
    " const rect = canvas.getBoundingClientRect();"
    " return JSON.stringify({x: rect.x + window.scrollX, y: rect.y + window.scrollY,"
    " width: rect.width, height: rect.height}); })()"
)
# renderer.requestSnapshot reads the stage back from the GPU, so it works even
# when the window is hidden and Page.captureScreenshot's clip coordinates no
# longer line up with the (hidden) window's surface.
STAGE_SNAPSHOT_JS = (
    "(async () => {"
    " const renderer = (typeof vm !== 'undefined' && vm.runtime) ? vm.runtime.renderer : null;"
    " if (!renderer || typeof renderer.requestSnapshot !== 'function') {"
    "   return JSON.stringify({error: 'requestSnapshot unavailable'});"
    " }"
    " const url = await new Promise((resolve) => {"
    "   try { renderer.requestSnapshot((u) => resolve(u || null)); } catch (error) { resolve(null); }"
    " });"
    " if (!url) return JSON.stringify({error: 'snapshot failed'});"
    " return JSON.stringify({url: url, width: renderer.canvas.width, height: renderer.canvas.height});"
    " })()"
)


def log(message: str) -> None:
    print(f"[gsdev] {message}", flush=True)


def _first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        try:
            if candidate and candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def turbowarp_candidates(platform: str | None = None) -> list[Path]:
    """Where TurboWarp may live, per platform (used by find_turbowarp/selftest)."""
    platform = platform or sys.platform
    candidates: list[Path] = []
    override = os.environ.get("TURBOWARP_EXE")
    if override:
        candidates.append(Path(override).expanduser())
    if platform == "win32":
        store_root = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "WindowsApps"
        try:
            candidates += sorted(
                store_root.glob("*TurboWarpDesktop*/app/TurboWarp.exe"), reverse=True
            )
        except OSError:
            pass
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Programs" / "TurboWarp" / "TurboWarp.exe")
        candidates.append(
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "TurboWarp" / "TurboWarp.exe"
        )
    elif platform == "darwin":
        candidates += [
            Path("/Applications/TurboWarp.app/Contents/MacOS/TurboWarp"),
            Path.home() / "Applications" / "TurboWarp.app/Contents/MacOS/TurboWarp",
        ]
    else:
        for name in ("turbowarp-desktop", "turbowarp", "TurboWarp"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        candidates += [
            Path("/var/lib/flatpak/exports/bin/org.turbowarp.TurboWarp"),
            Path.home() / ".local" / "bin" / "turbowarp-desktop",
        ]
    return candidates


def find_turbowarp() -> Path | None:
    return _first_existing(turbowarp_candidates())


def scratch_candidates(platform: str | None = None) -> list[Path]:
    """Where Scratch Desktop may live, per platform."""
    platform = platform or sys.platform
    candidates: list[Path] = []
    override = os.environ.get("SCRATCH_EXE")
    if override:
        candidates.append(Path(override).expanduser())
    if platform == "win32":
        for base in (
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        ):
            if base:
                candidates.append(Path(base) / "Scratch 3" / "Scratch 3.exe")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Programs" / "Scratch 3" / "Scratch 3.exe")
    elif platform == "darwin":
        candidates.append(Path("/Applications/Scratch.app/Contents/MacOS/Scratch"))
    else:
        for name in ("scratch-desktop", "scratch"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    return candidates


def find_scratch() -> Path | None:
    return _first_existing(scratch_candidates())


def electron_env() -> dict:
    """Environment for launching an Electron app.

    VS Code's extension host sets ELECTRON_RUN_AS_NODE, which makes a launched
    Electron binary behave as plain Node and exit immediately. Drop it so the
    editor actually starts as a GUI app.
    """
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env.pop("NODE_OPTIONS", None)
    return env


def port_open(port: int, timeout: float = 0.3) -> bool:
    """Fast check for a listener, so a closed port does not cost a HTTP timeout."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def free_port() -> int:
    """An unused TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def resolve_port(requested: int) -> int:
    """Resolve ``--port 0`` / ``GSDEV_CDP_PORT=0`` to a concrete port.

    A port of 0 means "auto": reuse this project's last auto port while a
    listener is still there, otherwise pick a fresh free one and remember it in
    tools/.gsdev-port so later commands (status/stop/close) find the same
    editor. The file is per project root, so parallel runs in separate
    worktrees stay independent.
    """
    if requested and requested > 0:
        return requested
    if PORT_FILE.exists():
        try:
            saved = int(PORT_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            saved = 0
        if saved and port_open(saved):
            return saved
    chosen = free_port()
    try:
        PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(str(chosen), encoding="utf-8")
    except OSError:
        pass
    log(f"auto-selected port {chosen}")
    return chosen


def headless_flags(platform: str | None = None) -> list[str]:
    """Editor flags that run it with no visible window.

    Electron has no true headless mode. On Windows and macOS ``--headless`` asks
    for a hidden window. On Linux that still needs a display, so also use
    Chromium's Ozone headless platform; with no display at all, force software GL
    (SwiftShader) instead of disabling the GPU, because TurboWarp's WebGL
    renderer must initialize for the stage (and screenshots) to exist. The Linux
    flags are deliberately Linux-only: macOS has no ``DISPLAY`` either and would
    otherwise get them by mistake.
    """
    platform = platform or sys.platform
    flags = ["--headless"]
    if platform.startswith("linux"):
        flags.append("--ozone-platform=headless")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            flags += ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    return flags


def prepare_scratch_profile() -> None:
    """Seed telemetry.json so Scratch never shows its share-data modal.

    The renderer only shows the modal when ``getTelemetryDidOptIn`` is not a
    boolean, so writing ``optIn`` up front keeps screenshots clean.
    """
    SCRATCH_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    telemetry = SCRATCH_PROFILE_DIR / "telemetry.json"
    data: dict = {}
    if telemetry.exists():
        try:
            data = json.loads(telemetry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    if data.get("optIn") is False:
        return
    data.setdefault("clientID", str(uuid.uuid4()))
    data.setdefault("packetQueue", [])
    data["optIn"] = False
    telemetry.write_text(json.dumps(data), encoding="utf-8")


def find_scratch_target(port: int) -> dict | None:
    try:
        targets = list_targets(port=port, timeout=2.0)
    except Exception:
        return None
    for target in targets:
        url = str(target.get("url", ""))
        if (
            target.get("type") == "page"
            and url.endswith("renderer/index.html")
            and target.get("webSocketDebuggerUrl")
        ):
            return target
    return None


def ensure_scratch_runtime(
    port: int,
    project: Path | None = None,
    ready_timeout: float = 60.0,
    headless: bool = False,
) -> tuple[CDP, bool]:
    launched = False
    target = find_scratch_target(port) if port_open(port) else None
    if target is None:
        exe = find_scratch()
        if exe is None:
            raise SystemExit(
                "Scratch Desktop not found. Install it or set SCRATCH_EXE to the "
                "Scratch 3 executable and retry."
            )
        prepare_scratch_profile()
        arguments = [str(exe)]
        # Scratch parses argv with minimist, which swallows a trailing positional
        # argument when the preceding flag has no "=". Put the project first.
        if project is not None:
            arguments.append(str(project))
            log(f"launching {exe} with {Path(project).name}")
        else:
            log(f"launching {exe}")
        arguments += [
            f"--user-data-dir={SCRATCH_PROFILE_DIR}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]
        if headless:
            arguments += headless_flags()
            log("launching headless (no window)")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        subprocess.Popen(
            arguments,
            env=electron_env(),
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        launched = True
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            target = find_scratch_target(port)
            if target is not None:
                break
            time.sleep(0.5)
    if target is None:
        raise SystemExit(
            f"Scratch did not open a debug port on {port} within {ready_timeout:.0f}s."
        )
    cdp = connect(target)
    wait_for_scratch_editor(cdp)
    return cdp, launched


def wait_for_scratch_editor(cdp: CDP, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if cdp.evaluate("!!document.querySelector('[class*=green-flag-button]')"):
                return
        except (CDPError, TimeoutError, OSError):
            pass
        time.sleep(0.3)
    raise SystemExit("Scratch editor did not finish loading.")


def scratch_sprite_names(cdp: CDP) -> list[str]:
    raw = cdp.evaluate(
        "JSON.stringify([...document.querySelectorAll('[class*=sprite-name]')]"
        ".map(e => e.textContent))"
    )
    return json.loads(raw) if raw else []


def wait_for_scratch_project(cdp: CDP, expected: list[str], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    actual: list[str] = []
    while time.monotonic() < deadline:
        actual = sorted(scratch_sprite_names(cdp))
        if actual == expected:
            return
        time.sleep(0.2)
    raise SystemExit(f"project did not load in Scratch; expected {expected}, got {actual}")


def scratch_green_flag(cdp: CDP) -> None:
    cdp.evaluate(
        "(() => { const b = document.querySelector('[class*=green-flag-button]');"
        " if (b) b.click(); return 'ok'; })()"
    )


def scratch_stop(cdp: CDP) -> None:
    cdp.evaluate(
        "(() => { const b = document.querySelector('[class*=stop-all]');"
        " if (b) b.click(); return 'ok'; })()"
    )


SCRATCH_STAGE_RECT_JS = (
    "(() => {"
    " const c = document.querySelector('[class*=stage_stage] canvas')"
    " || [...document.querySelectorAll('canvas')].find(x => !x.className);"
    " if (!c) return null;"
    " const r = c.getBoundingClientRect();"
    " return JSON.stringify({x: r.x, y: r.y, width: r.width, height: r.height});"
    " })()"
)

# Scratch Desktop does not expose window.vm and cannot load browser extensions,
# but it runs with nodeIntegration and keeps the VM in the React tree. Finding
# it there lets us hook procedure calls, which is what a Scratch Addons-style
# debugger does. findVM lives inside the IIFE so this script can be evaluated
# again on an already-loaded page without redeclaring an identifier.
SCRATCH_INSTALL_SHIM_JS = r"""
(() => {
  const findVM = () => {
    const el = document.getElementById('app');
    if (!el) return null;
    const key = Object.keys(el).find(k => k.startsWith('__reactContainer$') || k.startsWith('_reactRootContainer'));
    if (!key) return null;
    let root = el[key];
    if (root && root.current) root = root.current;
    const seen = new Set();
    const stack = [root];
    let n = 0;
    while (stack.length && n < 30000) {
      const f = stack.pop();
      n++;
      if (!f || seen.has(f)) continue;
      seen.add(f);
      const props = f.memoizedProps;
      if (props && typeof props === 'object' && props.vm && props.vm.runtime) return props.vm;
      if (f.child) stack.push(f.child);
      if (f.sibling) stack.push(f.sibling);
    }
    return null;
  };
  const g = (window.__gsdev = window.__gsdev || {logs: [], stopped: false});
  if (!Array.isArray(g.logs)) g.logs = [];
  // A reused shim may still hold events (and a null stop marker) from a
  // previous run, so start each run with a clean queue.
  g.logs.length = 0;
  g.stopped = false;
  const vm = findVM() || window.__gsdevVm;
  if (!vm) return JSON.stringify({fatal: 'vm not found'});
  window.__gsdevVm = vm;
  const runtime = vm.runtime;
  const prim = runtime._primitives;
  if (!prim || typeof prim['procedures_call'] !== 'function') {
    return JSON.stringify({fatal: 'procedures_call unavailable'});
  }
  const Z = '\u200B\u200B';
  const levels = {};
  levels[Z + 'log' + Z + ' %s'] = 'log';
  levels[Z + 'warn' + Z + ' %s'] = 'warn';
  levels[Z + 'error' + Z + ' %s'] = 'error';
  const existing = prim['procedures_call'];
  const original = existing.__gsdev ? existing.__gsdevOriginal : existing;
  const wrapped = function (args, util) {
    try {
      const procedureCode = args && args.mutation && args.mutation.proccode;
      const level = levels[procedureCode];
      if (level) {
        let value = args.arg0;
        if (value === undefined) {
          for (const key in args) {
            if (key !== 'mutation') { value = args[key]; break; }
          }
        }
        let sprite = 'unknown';
        try {
          const target = util && util.target;
          if (target) sprite = target.getName ? target.getName() : 'unknown';
        } catch (error) {}
        g.logs.push({
          sprite: sprite === 'Stage' ? 'stage' : sprite,
          level: level,
          value: String(value),
          time: Date.now()
        });
        return;
      }
    } catch (error) {}
    return original.apply(this, arguments);
  };
  wrapped.__gsdev = true;
  wrapped.__gsdevOriginal = original;
  prim['procedures_call'] = wrapped;
  if (!g.stopHooked) {
    g.stopHooked = true;
    try { runtime.on('PROJECT_RUN_STOP', () => { g.logs.push(null); g.stopped = true; }); } catch (error) {}
  }
  g.patched = true;
  return JSON.stringify({ok: true});
})()
"""


def install_scratch_shim(cdp: CDP) -> dict:
    raw = cdp.evaluate(SCRATCH_INSTALL_SHIM_JS)
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def inject_scratch_project(cdp: CDP, sb3_path: Path) -> None:
    """Load a rebuilt .sb3 into the already-open Scratch VM.

    Scratch re-reads its file only when the page mounts, and we avoid reloading
    so the unsaved-changes prompt cannot block us, so warm runs load the build
    straight into the VM found by the shim.
    """
    data = Path(sb3_path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    expression = (
        "(async () => {"
        " const vm = window.__gsdevVm;"
        " if (!vm) return 'no-vm';"
        f" const raw = atob('{encoded}');"
        " const bytes = new Uint8Array(raw.length);"
        " for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);"
        " await vm.loadProject(bytes.buffer);"
        " return 'ok';"
        " })()"
    )
    result = cdp.evaluate(expression, await_promise=True, timeout=120)
    if result != "ok":
        raise SystemExit(f"could not load project into Scratch ({result})")


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_TERMINATE = 0x0001
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _AF_INET = 2
    _TCP_TABLE_OWNER_PID_ALL = 5
    _MIB_TCP_STATE_LISTEN = 2

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    _iphlpapi.GetExtendedTcpTable.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    ]
    _iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD


def _win32_process_table() -> list[tuple[int, int, str]]:
    """(pid, parent pid, executable name) for every process, via Win32."""
    if os.name != "nt":
        return []
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE:
        return []
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    rows: list[tuple[int, int, str]] = []
    try:
        if _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                rows.append(
                    (int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile)
                )
                if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        _kernel32.CloseHandle(snapshot)
    return rows


def _win32_terminate(pid: int) -> None:
    handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if handle:
        _kernel32.TerminateProcess(handle, 1)
        _kernel32.CloseHandle(handle)


def _process_subtree(root: int, table: list[tuple[int, int, str]]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, parent, _name in table:
        children.setdefault(parent, []).append(pid)
    seen = {root}
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _windows_port_owner_pids(port: int) -> list[int]:
    """PIDs listening on ``port`` via GetExtendedTcpTable (no external tools)."""
    size = wintypes.DWORD(0)
    _iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0
    )
    if not size.value:
        return []
    buffer = ctypes.create_string_buffer(size.value)
    code = _iphlpapi.GetExtendedTcpTable(
        buffer, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0
    )
    if code != 0:
        return []
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    rows = ctypes.cast(
        ctypes.addressof(buffer) + ctypes.sizeof(wintypes.DWORD),
        ctypes.POINTER(_MIB_TCPROW_OWNER_PID),
    )
    pids: set[int] = set()
    for index in range(count):
        row = rows[index]
        # dwLocalPort is stored in network byte order.
        local_port = ((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF)
        if local_port == port and row.dwState == _MIB_TCP_STATE_LISTEN:
            pids.add(int(row.dwOwningPid))
    return sorted(pids)


def _port_owner_pids(port: int) -> list[int]:
    pids: list[int] = []
    if os.name == "nt":
        pids = _windows_port_owner_pids(port)
        if pids:
            return pids
        netstat = shutil.which("netstat")
        if not netstat:
            return []
        result = subprocess.run(
            [netstat, "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].endswith(f":{port}"):
                    try:
                        pids.append(int(parts[4]))
                    except ValueError:
                        pass
        return pids
    for name, extra in (("lsof", ["-ti", f"tcp:{port}"]), ("fuser", [f"{port}/tcp"])):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            result = subprocess.run(
                [exe, *extra], capture_output=True, text=True, timeout=20
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for token in result.stdout.replace(":", " ").split():
            if token.isdigit():
                pids.append(int(token))
        if pids:
            break
    return pids


def find_editor_pids(port: int, process_name: str, profile_dir: Path) -> list[int]:
    """PIDs of this project's isolated editor serving the debug port.

    On Windows the editor is identified by the process that owns the debug port
    and its descendants, so no WMI/PowerShell is needed and other editor windows
    the user may have open are left alone.
    """
    if os.name == "nt":
        owners = _port_owner_pids(port)
        if not owners:
            return []
        table = _win32_process_table()
        names = {pid: name.lower() for pid, _parent, name in table}
        wanted = process_name.lower()
        pids: set[int] = set()
        for owner in owners:
            for pid in _process_subtree(owner, table):
                if pid == owner or names.get(pid) == wanted:
                    pids.add(pid)
        pids.discard(os.getpid())
        return sorted(pids)
    needle = str(profile_dir)
    pids: set[int] = set()
    result = subprocess.run(
        ["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if needle in stripped:
            token = stripped.split(None, 1)[0]
            if token.isdigit():
                pids.add(int(token))
    for pid in _port_owner_pids(port):
        pids.add(pid)
    pids.discard(os.getpid())
    return sorted(pids)


def find_turbowarp_pids(port: int) -> list[int]:
    return find_editor_pids(port, "TurboWarp.exe", PROFILE_DIR)


def find_scratch_pids(port: int) -> list[int]:
    return find_editor_pids(port, "Scratch 3.exe", SCRATCH_PROFILE_DIR)


def find_pids_by_executable(exe: Path | None) -> list[int]:
    """PIDs whose executable is exactly ``exe`` (last-resort kill match)."""
    if exe is None:
        return []
    pids: set[int] = set()
    if os.name == "nt":
        wanted = Path(exe).name.lower()
        for pid, _parent, name in _win32_process_table():
            if name.lower() == wanted:
                pids.add(pid)
    else:
        pgrep = shutil.which("pgrep")
        if pgrep:
            try:
                result = subprocess.run(
                    [pgrep, "-f", str(exe)], capture_output=True, text=True, timeout=20
                )
                for token in result.stdout.split():
                    if token.isdigit():
                        pids.add(int(token))
            except (OSError, subprocess.TimeoutExpired):
                pass
    pids.discard(os.getpid())
    return sorted(pids)


def terminate_pids(pids: list[int]) -> None:
    for pid in pids:
        if os.name == "nt":
            _win32_terminate(pid)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _parent_map(pids: list[int]) -> dict[int, int]:
    """pid -> parent pid for the given pids (Windows)."""
    if os.name != "nt" or not pids:
        return {}
    wanted = set(pids)
    return {
        pid: parent
        for pid, parent, _name in _win32_process_table()
        if pid in wanted
    }


def _root_pids(pids: list[int]) -> list[int]:
    """Processes in the set that are not children of another process in it."""
    parents = _parent_map(pids)
    if not parents:
        return pids[:1]
    roots = [pid for pid in pids if parents.get(pid) not in pids]
    return roots or pids[:1]


def kill_editor_processes(pids: list[int], port: int) -> None:
    """Kill an isolated editor without triggering its crash dialog.

    Killing a renderer before the browser process makes Chromium's main process
    answer child-process-gone with a modal "Crashed" dialog. Kill the root (the
    process owning the debug port, or the top of the parent chain) first and
    alone; the OS reaps the children with it.
    """
    if not pids:
        return
    roots = [pid for pid in _port_owner_pids(port) if pid in pids] or _root_pids(pids)
    terminate_pids(roots)
    time.sleep(0.5)
    remaining = sorted(set(find_turbowarp_pids(port)) | set(find_scratch_pids(port)))
    leftovers = [pid for pid in remaining if pid not in roots]
    if leftovers:
        terminate_pids(leftovers)


def wait_for_vm(cdp: CDP, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if cdp.evaluate("typeof vm !== 'undefined' && vm && vm.runtime ? 'ready' : 'loading'", timeout=5) == "ready":
                return
        except (CDPError, TimeoutError, OSError) as error:
            last_error = error
        time.sleep(0.3)
    raise SystemExit(f"TurboWarp runtime did not become ready: {last_error}")


def reconnect(port: int, timeout: float = 30.0) -> CDP:
    deadline = time.monotonic() + timeout
    while True:
        target = find_editor_target(port=port, timeout=2.0)
        if target is not None:
            try:
                cdp = connect(target)
                wait_for_vm(cdp, timeout=max(5.0, deadline - time.monotonic()))
                return cdp
            except (CDPError, TimeoutError, OSError, SystemExit):
                pass
        if time.monotonic() >= deadline:
            raise SystemExit("lost the TurboWarp editor while reloading it")
        time.sleep(0.5)


def reload_editor(cdp: CDP, port: int) -> CDP:
    # Best effort: clear the page's unload handler so a reload is not blocked by
    # an unsaved-changes prompt. Only used for one-time Debugger addon setup.
    try:
        cdp.evaluate("(() => { try { window.onbeforeunload = null; } catch (e) {} return 'ok'; })()")
    except (CDPError, TimeoutError, OSError):
        pass
    try:
        cdp.call("Page.reload", timeout=10)
    except (CDPError, TimeoutError, OSError):
        pass
    cdp.close()
    time.sleep(0.5)
    return reconnect(port)


def enable_debugger(cdp: CDP, port: int) -> CDP:
    try:
        changed = cdp.evaluate(ENABLE_DEBUGGER_JS)
    except (CDPError, TimeoutError) as error:
        log(f"warning: could not check Debugger addon ({error})")
        return cdp
    if changed:
        log("enabling the TurboWarp Debugger addon (one-time profile setup)")
        return reload_editor(cdp, port)
    return cdp


def ensure_runtime(
    port: int,
    project: Path | None = None,
    ready_timeout: float = 30.0,
    headless: bool = False,
) -> tuple[CDP, bool]:
    """Attach to the isolated editor, launching it when needed.

    TurboWarp Desktop opens files passed as positional arguments, so passing
    ``project`` lets the editor load the build itself instead of racing its own
    startup project load. Returns the connection and whether we launched it.
    """
    launched = False
    target = find_editor_target(port=port, timeout=2.0) if port_open(port) else None
    if target is None:
        exe = find_turbowarp()
        if exe is None:
            raise SystemExit(
                "TurboWarp Desktop not found. Install it or set TURBOWARP_EXE to the "
                "TurboWarp executable and retry."
            )
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        arguments = [
            str(exe),
            f"--user-data-dir={PROFILE_DIR}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]
        if headless:
            arguments += headless_flags()
            log("launching headless (no window)")
        if project is not None:
            arguments.append(str(project))
            log(f"launching {exe} with {Path(project).name}")
        else:
            log(f"launching {exe}")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        subprocess.Popen(
            arguments,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        launched = True
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            target = find_editor_target(port=port, timeout=2.0)
            if target is not None:
                break
            time.sleep(0.5)
    if target is None:
        raise SystemExit(
            f"TurboWarp did not open a debug port on {port} within {ready_timeout:.0f}s."
        )
    cdp = connect(target)
    wait_for_vm(cdp)
    return enable_debugger(cdp, port), launched


def build_project() -> Path:
    goboscript = shutil.which("goboscript")
    if goboscript is None:
        raise SystemExit("goboscript is not on PATH. Install the GoboScript compiler.")
    log(f"building {PROJECT_ROOT}")
    # Rely on cwd for the input: newer goboscript uses -i/--input and rejects a
    # positional directory, while older builds took a positional one.
    result = subprocess.run(
        [goboscript, "build", "-o", str(SB3_PATH)],
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        raise SystemExit(f"goboscript build failed (exit {result.returncode}).")
    return SB3_PATH


def project_target_names(sb3_path: Path) -> list[str]:
    with zipfile.ZipFile(sb3_path) as archive:
        project = json.loads(archive.read("project.json").decode("utf-8"))
    return [target["name"] for target in project["targets"]]


def project_sprite_names(sb3_path: Path) -> list[str]:
    with zipfile.ZipFile(sb3_path) as archive:
        project = json.loads(archive.read("project.json").decode("utf-8"))
    return [target["name"] for target in project["targets"] if not target.get("isStage")]


def runtime_target_names(cdp: CDP) -> list[str]:
    raw = cdp.evaluate(
        "JSON.stringify(vm.runtime.targets.map(function (t) { return t.getName(); }))"
    )
    return json.loads(raw) if raw else []


def editor_has_file(cdp: CDP) -> bool:
    """Whether the open editor window is backed by a project file on disk.

    TurboWarp Desktop re-reads that file whenever the page mounts, so a page
    reload is enough to pull in a fresh build without injecting it over CDP.
    """
    try:
        return bool(
            cdp.evaluate(
                "(async () => (await EditorPreload.getInitialFile()) !== null)()",
                await_promise=True,
                timeout=15,
            )
        )
    except (CDPError, TimeoutError, OSError):
        return False


def wait_for_project(cdp: CDP, expected: list[str], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    actual: list[str] = []
    while time.monotonic() < deadline:
        actual = sorted(runtime_target_names(cdp))
        if actual == expected:
            return
        time.sleep(0.2)
    raise SystemExit(f"project did not load; expected {expected}, got {actual}")


def load_project(cdp: CDP, sb3_path: Path) -> None:
    data = Path(sb3_path).read_bytes()
    if len(data) > 8 * 1024 * 1024:
        log("warning: project is large; in-page loading may be slow")
    encoded = base64.b64encode(data).decode("ascii")
    expression = (
        "(async () => {"
        f" const raw = atob('{encoded}');"
        " const bytes = new Uint8Array(raw.length);"
        " for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);"
        " await vm.loadProject(bytes.buffer);"
        " return 'ok';"
        " })()"
    )
    # TurboWarp Desktop loads its own startup project as the editor opens. If
    # that load is still in flight when we inject the project, its targets can
    # be appended alongside ours (e.g. a leftover "Sprite1"). Verify the loaded
    # target set and reload until the runtime matches the project exactly.
    expected = sorted(project_target_names(sb3_path))
    actual: list[str] = []
    for _ in range(3):
        cdp.evaluate(expression, await_promise=True, timeout=120)
        time.sleep(0.4)
        actual = sorted(runtime_target_names(cdp))
        if actual == expected:
            return
        log(f"warning: unexpected targets {actual} after load; reloading")
    raise SystemExit(
        f"project did not load cleanly; expected {expected}, got {actual}"
    )


def install_shim(cdp: CDP) -> dict:
    raw = cdp.evaluate(INSTALL_SHIM_JS)
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def set_cpu_throttle(cdp: CDP, rate: float) -> None:
    """Emulate a slower CPU for the renderer via CDP.

    Chromium multiplies the renderer's task scheduling by ``rate`` (1 disables
    it), which is a close stand-in for a slower device such as a phone. The
    setting lives on the page target, so re-apply it after any reload.
    """
    if not rate or rate <= 1:
        return
    cdp.call("Emulation.setCPUThrottlingRate", {"rate": float(rate)})
    log(f"emulating a {rate:g}x slower CPU")


def start_project(cdp: CDP) -> None:
    cdp.evaluate("(() => { vm.greenFlag(); return 'ok'; })()")


def stop_project(cdp: CDP) -> None:
    cdp.evaluate("(() => { vm.stopAll(); return 'ok'; })()")


def drain_logs(cdp: CDP) -> list:
    raw = cdp.evaluate(DRAIN_LOGS_JS)
    return json.loads(raw) if raw else []


def is_stopped(cdp: CDP) -> bool:
    return bool(cdp.evaluate(IS_STOPPED_JS))


def print_event(event: dict) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{event['level'].upper()} {stamp} {event['sprite']}] {event['value']}", flush=True)


def capture_screenshot(
    cdp: CDP, out_path: Path, rect_js: str = STAGE_RECT_JS
) -> tuple[Path, dict]:
    cdp.call("Page.bringToFront", timeout=10)
    raw_rect = cdp.evaluate(rect_js)
    if not raw_rect:
        raise SystemExit("Stage canvas not found; is a project loaded?")
    rect = json.loads(raw_rect)
    if not rect.get("width") or not rect.get("height"):
        raise SystemExit("Stage canvas has no size; is the editor window visible?")
    result = cdp.call(
        "Page.captureScreenshot",
        {
            "format": "png",
            "clip": {
                "x": rect["x"],
                "y": rect["y"],
                "width": rect["width"],
                "height": rect["height"],
                "scale": 1,
            },
            "captureBeyondViewport": False,
        },
        timeout=30,
    )
    data = result.get("data")
    if not data:
        raise SystemExit("captureScreenshot returned no image data.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(data))
    return out_path, rect


def capture_stage_snapshot(cdp: CDP, out_path: Path) -> tuple[Path, dict]:
    """Capture the stage through the renderer's snapshot, not window geometry."""
    raw = cdp.evaluate(STAGE_SNAPSHOT_JS, await_promise=True, timeout=60)
    try:
        info = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        info = {}
    if "error" in info or not info.get("url"):
        raise SystemExit(f"stage snapshot failed ({info.get('error', 'no data')})")
    url = info["url"]
    if not url.startswith("data:image/png;base64,"):
        raise SystemExit("stage snapshot returned unexpected data")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(url.split(",", 1)[1]))
    rect = {"x": 0, "y": 0, "width": info.get("width", 0), "height": info.get("height", 0)}
    return out_path, rect


def prepare(cdp: CDP, args: argparse.Namespace, sb3_path: Path) -> None:
    expected = sorted(project_target_names(sb3_path))
    if editor_has_file(cdp):
        # The editor loads the project from disk, including on page reload, so
        # just wait for it to finish instead of injecting it over CDP.
        wait_for_project(cdp, expected)
    else:
        # The window has no backing file (e.g. manually opened); inject it.
        load_project(cdp, sb3_path)
    set_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
    # The Debugger addon registers its blocks slightly after the editor loads,
    # so a fresh launch can miss them on the first try.
    patched: dict = {}
    deadline = time.monotonic() + 5.0
    while True:
        patched = install_shim(cdp)
        if any(patched.values()) or "fatal" in patched:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if "fatal" in patched:
        log(f"warning: log capture unavailable ({patched['fatal']})")
    elif not any(patched.values()):
        log("warning: no log/warn/error blocks found; enable the Debugger addon manually")
    elif args.verbose:
        log(f"log capture: {patched}")


def cmd_build(args: argparse.Namespace) -> int:
    build_project()
    log(f"built {SB3_PATH.name}")
    return 0


def cmd_run_scratch(args: argparse.Namespace) -> int:
    if not args.no_build:
        build_project()
    cdp, launched = ensure_scratch_runtime(args.port, SB3_PATH, headless=args.headless)
    expected = sorted(project_sprite_names(SB3_PATH))
    try:
        wait_for_scratch_project(cdp, expected)
        set_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
        shim: dict = {}
        deadline = time.monotonic() + 5.0
        while True:
            shim = install_scratch_shim(cdp)
            if "fatal" not in shim:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if "fatal" in shim:
            log(f"warning: log capture unavailable ({shim['fatal']})")
        if not launched and not args.no_reload:
            # Warm editor: load the rebuilt project into the VM (the page only
            # re-reads its file on mount, and we do not reload).
            inject_scratch_project(cdp, SB3_PATH)
            wait_for_scratch_project(cdp, expected)
            install_scratch_shim(cdp)
        scratch_green_flag(cdp)
        log("running; press Ctrl+C to stop")
        deadline = time.monotonic() + args.duration if args.duration else None
        try:
            while True:
                for event in drain_logs(cdp):
                    if event is None:
                        log("project stopped")
                        return 0
                    print_event(event)
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if is_stopped(cdp):
                    log("project stopped")
                    return 0
                time.sleep(0.2)
        except KeyboardInterrupt:
            log("interrupted")
    finally:
        try:
            scratch_stop(cdp)
        except (CDPError, TimeoutError, OSError):
            pass
        cdp.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if BACKEND == "scratch":
        return cmd_run_scratch(args)
    if not args.no_build:
        build_project()
    cdp, launched = ensure_runtime(args.port, SB3_PATH, headless=args.headless)
    if not args.no_reload and not launched:
        # A warm editor has finished loading, so inject the fresh build over CDP
        # instead of reloading. TurboWarp prompts before unload once the project
        # is unsaved, which would block a page reload.
        load_project(cdp, SB3_PATH)
    try:
        prepare(cdp, args, SB3_PATH)
        start_project(cdp)
        log("running; press Ctrl+C to stop")
        deadline = time.monotonic() + args.duration if args.duration else None
        try:
            while True:
                for event in drain_logs(cdp):
                    if event is None:
                        log("project stopped")
                        return 0
                    print_event(event)
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if is_stopped(cdp):
                    log("project stopped")
                    return 0
                time.sleep(0.2)
        except KeyboardInterrupt:
            log("interrupted")
    finally:
        try:
            stop_project(cdp)
        except (CDPError, TimeoutError, OSError):
            pass
        cdp.close()
    return 0


def cmd_screenshot_scratch(args: argparse.Namespace) -> int:
    if not args.no_build:
        build_project()
    out_path = Path(args.out).expanduser() if args.out else DEBUG_DIR / "stage.png"
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    cdp, launched = ensure_scratch_runtime(args.port, SB3_PATH, headless=args.headless)
    expected = sorted(project_sprite_names(SB3_PATH))
    try:
        wait_for_scratch_project(cdp, expected)
        set_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
        install_scratch_shim(cdp)
        if not launched and not args.no_reload:
            inject_scratch_project(cdp, SB3_PATH)
            wait_for_scratch_project(cdp, expected)
        scratch_green_flag(cdp)
        time.sleep(max(0, args.delay) / 1000.0)
        saved, rect = capture_screenshot(cdp, out_path, rect_js=SCRATCH_STAGE_RECT_JS)
        log(f"screenshot: {saved} ({int(rect['width'])}x{int(rect['height'])})")
    finally:
        try:
            scratch_stop(cdp)
        except (CDPError, TimeoutError, OSError):
            pass
        cdp.close()
    return 0


def cmd_screenshot(args: argparse.Namespace) -> int:
    if BACKEND == "scratch":
        return cmd_screenshot_scratch(args)
    if not args.no_build:
        build_project()
    out_path = Path(args.out).expanduser() if args.out else DEBUG_DIR / "stage.png"
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    cdp, launched = ensure_runtime(args.port, SB3_PATH, headless=args.headless)
    if not args.no_reload and not launched:
        # A warm editor has finished loading, so inject the fresh build over CDP
        # instead of reloading. TurboWarp prompts before unload once the project
        # is unsaved, which would block a page reload.
        load_project(cdp, SB3_PATH)
    try:
        prepare(cdp, args, SB3_PATH)
        start_project(cdp)
        time.sleep(max(0, args.delay) / 1000.0)
        try:
            # Works with a hidden window and headless, where a clipped
            # captureScreenshot would be framed against the wrong surface.
            saved, rect = capture_stage_snapshot(cdp, out_path)
        except SystemExit:
            saved, rect = capture_screenshot(cdp, out_path)
        log(f"screenshot: {saved} ({int(rect['width'])}x{int(rect['height'])})")
    finally:
        try:
            stop_project(cdp)
        except (CDPError, TimeoutError, OSError):
            pass
        cdp.close()
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    # Works with whichever editor is actually open, so it does not depend on
    # GSDEV_BACKEND matching the run that started it.
    scratch_target = find_scratch_target(args.port)
    if scratch_target is not None:
        cdp = connect(scratch_target)
        try:
            scratch_stop(cdp)
            log("project stopped")
        finally:
            cdp.close()
        return 0
    target = find_editor_target(port=args.port, timeout=2.0)
    if target is None:
        log(f"no editor on port {args.port}")
        return 0
    cdp = connect(target)
    try:
        stop_project(cdp)
        log("project stopped")
    finally:
        cdp.close()
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    # Kill whichever isolated editor process is running on this port. Always
    # force-kill: Browser.close would trigger the editor's unsaved-changes quit
    # dialog, which needs a manual click and is exactly what we must avoid.
    pids = sorted(set(find_turbowarp_pids(args.port)) | set(find_scratch_pids(args.port)))
    if not pids and port_open(args.port):
        for exe in (find_turbowarp(), find_scratch()):
            pids = sorted(set(pids) | set(find_pids_by_executable(exe)))
    if pids:
        kill_editor_processes(pids, args.port)
        log("killed editor (pid " + ", ".join(str(pid) for pid in pids) + ")")
        return 0
    log(f"no editor on port {args.port}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    # Report the editor that is actually open.
    scratch_target = find_scratch_target(args.port)
    if scratch_target is not None:
        cdp = connect(scratch_target)
        try:
            log(json.dumps({"backend": "scratch", "sprites": scratch_sprite_names(cdp)}))
        finally:
            cdp.close()
        return 0
    target = find_editor_target(port=args.port, timeout=2.0)
    if target is None:
        log(f"no editor on port {args.port}")
        return 1
    cdp = connect(target)
    try:
        state = cdp.evaluate(
            "JSON.stringify({sprites: vm.runtime.targets.map(t => t.getName()),"
            " running: vm.runtime.threads.length > 0})"
        )
        log(state)
    finally:
        cdp.close()
    return 0


def _tool_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0].strip() if output else ""


def cmd_doctor(args: argparse.Namespace) -> int:
    failures = 0
    warnings = 0

    def report(status: str, label: str, detail: str = "") -> None:
        suffix = f" - {detail}" if detail else ""
        print(f"[{status:4}] {label}{suffix}", flush=True)

    def ok(label: str, detail: str = "") -> None:
        report("ok", label, detail)

    def warn(label: str, detail: str = "") -> None:
        nonlocal warnings
        warnings += 1
        report("warn", label, detail)

    def fail(label: str, detail: str = "") -> None:
        nonlocal failures
        failures += 1
        report("FAIL", label, detail)

    if sys.version_info >= (3, 10):
        ok("python", f"{platform.python_version()} at {sys.executable}")
    elif sys.version_info >= (3, 8):
        warn("python", f"{platform.python_version()} works, but 3.10+ is recommended")
    else:
        fail("python", f"{platform.python_version()} is too old; install 3.10+")

    goboscript = shutil.which("goboscript")
    if goboscript:
        ok("goboscript", _tool_version([goboscript, "--version"]) or goboscript)
    else:
        fail("goboscript", "not on PATH; install from github.com/aspizu/goboscript")

    turbowarp = find_turbowarp()
    scratch = find_scratch()
    if turbowarp:
        ok("TurboWarp Desktop", str(turbowarp))
    else:
        warn("TurboWarp Desktop", "not found; set TURBOWARP_EXE for the turbowarp backend")
    if scratch:
        ok("Scratch Desktop", str(scratch))
    else:
        warn("Scratch Desktop", "not found; set SCRATCH_EXE for the scratch backend")

    if BACKEND not in ("scratch", "turbowarp"):
        fail("GSDEV_BACKEND", f"unknown value {BACKEND!r}; expected scratch or turbowarp")
    else:
        chosen = scratch if BACKEND == "scratch" else turbowarp
        if chosen:
            ok(f"selected backend ({BACKEND})", str(chosen))
        else:
            fail(f"selected backend ({BACKEND})", "its editor was not found")

    if sys.platform == "win32":
        ok("win32 process/port APIs", "ctypes Toolhelp32 + GetExtendedTcpTable (stdlib)")
        if not shutil.which("netstat"):
            warn("netstat", "optional fallback for port lookup; not required")
    else:
        for name in ("ps", "pgrep", "lsof", "fuser"):
            path = shutil.which(name)
            if path:
                ok(name, path)
            else:
                warn(name, "optional; used for process/port handling")

    code = shutil.which("code")
    if code:
        ok("VS Code CLI", code)
    else:
        warn("VS Code CLI", "optional; only needed to launch tasks from a terminal")
    tasks = PROJECT_ROOT / ".vscode" / "tasks.json"
    if tasks.exists():
        ok(".vscode/tasks.json", str(tasks))
    else:
        warn(".vscode/tasks.json", "missing; Ctrl+Shift+B tasks will not be available")

    if port_open(args.port):
        target = find_scratch_target(args.port) or find_editor_target(port=args.port, timeout=1.0)
        if target:
            ok(f"port {args.port}", "an editor is already connected")
        else:
            warn(f"port {args.port}", "in use by another program; set --port or GSDEV_CDP_PORT")
    else:
        ok(f"port {args.port}", "free")

    for key in ("TURBOWARP_EXE", "SCRATCH_EXE", "GSDEV_BACKEND", "GSDEV_CPU", "GSDEV_CDP_PORT"):
        if os.environ.get(key):
            report("info", key, os.environ[key])

    summary = f"{failures} failure(s), {warnings} warning(s)"
    if failures:
        report("FAIL", "dependencies", summary)
    else:
        ok("dependencies", summary)
    return 1 if failures else 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Check the per-platform discovery and flag logic without that OS.

    macOS cannot be emulated here, but the parts that differ by platform are
    pure data: which install paths are searched and which headless flags are
    passed. This simulates win32/darwin/linux and fails loudly if one drifts.
    """
    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        if not condition:
            failures += 1
        suffix = f" - {detail}" if detail else ""
        print(f"[{'ok' if condition else 'FAIL':4}] {label}{suffix}", flush=True)

    saved_tw = os.environ.pop("TURBOWARP_EXE", None)
    saved_sc = os.environ.pop("SCRATCH_EXE", None)
    try:
        tw = {
            plat: [p.as_posix() for p in turbowarp_candidates(plat)]
            for plat in ("win32", "darwin", "linux")
        }
        sc = {
            plat: [p.as_posix() for p in scratch_candidates(plat)]
            for plat in ("win32", "darwin", "linux")
        }
    finally:
        if saved_tw is not None:
            os.environ["TURBOWARP_EXE"] = saved_tw
        if saved_sc is not None:
            os.environ["SCRATCH_EXE"] = saved_sc

    check(
        "win32 paths",
        any("WindowsApps" in p and p.endswith("TurboWarp.exe") for p in tw["win32"])
        and any(p.endswith("Scratch 3.exe") for p in sc["win32"]),
    )
    check(
        "darwin paths",
        "/Applications/TurboWarp.app/Contents/MacOS/TurboWarp" in tw["darwin"]
        and "/Applications/Scratch.app/Contents/MacOS/Scratch" in sc["darwin"],
        "(macOS .app bundles)",
    )
    check(
        "linux paths",
        any("flatpak" in p for p in tw["linux"]),
        "(flatpak / PATH lookup)",
    )

    check("win32 headless flags", headless_flags("win32") == ["--headless"], str(headless_flags("win32")))
    check("darwin headless flags", headless_flags("darwin") == ["--headless"], str(headless_flags("darwin")))

    saved_display = os.environ.pop("DISPLAY", None)
    saved_wayland = os.environ.pop("WAYLAND_DISPLAY", None)
    try:
        linux_no_display = headless_flags("linux")
        check(
            "linux headless flags (no display)",
            "--ozone-platform=headless" in linux_no_display
            and "--use-angle=swiftshader" in linux_no_display,
            str(linux_no_display),
        )
        os.environ["DISPLAY"] = ":0"
        linux_display = headless_flags("linux")
        check(
            "linux headless flags (with display)",
            "--ozone-platform=headless" in linux_display
            and "--use-angle=swiftshader" not in linux_display,
            str(linux_display),
        )
    finally:
        os.environ.pop("DISPLAY", None)
        if saved_display is not None:
            os.environ["DISPLAY"] = saved_display
        if saved_wayland is not None:
            os.environ["WAYLAND_DISPLAY"] = saved_wayland

    print(
        f"[info] running on {sys.platform}: headless={headless_flags()} "
        f"turbowarp={find_turbowarp()} scratch={find_scratch()}",
        flush=True,
    )
    if failures:
        print(f"[FAIL] selftest - {failures} check(s) failed", flush=True)
    else:
        print("[ok  ] selftest - platform logic matches expectations", flush=True)
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_port(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--port",
            type=int,
            default=DEFAULT_PORT,
            help="CDP port; 0 picks a free port and remembers it (default %(default)s)",
        )

    def add_cpu(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--cpu",
            type=float,
            default=float(os.environ.get("GSDEV_CPU", "0") or 0),
            metavar="RATE",
            help="emulate an RATE-times slower CPU, e.g. 4 for a phone (default off)",
        )

    add_port(subparsers.add_parser("build", help="compile the project"))
    add_port(subparsers.add_parser("status", help="show sprites and run state"))

    doctor = subparsers.add_parser("doctor", help="check that required tools are available")
    add_port(doctor)
    doctor.set_defaults(func=cmd_doctor)

    selftest = subparsers.add_parser(
        "selftest", help="check per-platform path/flag logic (incl. macOS)"
    )
    selftest.set_defaults(func=cmd_selftest)

    run = subparsers.add_parser("run", help="build, start, and stream logs")
    add_port(run)
    add_cpu(run)
    run.add_argument("--no-build", action="store_true", help="skip goboscript build")
    run.add_argument("--no-reload", action="store_true", help="reuse the current editor page")
    run.add_argument("--duration", type=float, default=0.0, help="stop after N seconds")
    run.add_argument("--headless", action="store_true", help="launch the editor with no visible window")
    run.add_argument("--verbose", action="store_true", help="print extra diagnostics")
    run.set_defaults(func=cmd_run)

    shot = subparsers.add_parser("screenshot", help="capture the stage to a PNG")
    add_port(shot)
    add_cpu(shot)
    shot.add_argument("--no-build", action="store_true", help="skip goboscript build")
    shot.add_argument("--no-reload", action="store_true", help="reuse the current editor page")
    shot.add_argument("--out", default="", help="output path (default debug/stage.png)")
    shot.add_argument("--delay", type=int, default=1200, help="wait before capture in ms")
    shot.add_argument("--headless", action="store_true", help="launch the editor with no visible window")
    shot.add_argument("--verbose", action="store_true", help="print extra diagnostics")
    shot.set_defaults(func=cmd_screenshot)

    stop = subparsers.add_parser("stop", help="stop the running project")
    add_port(stop)
    stop.set_defaults(func=cmd_stop)

    close = subparsers.add_parser("close", help="close the isolated editor window")
    add_port(close)
    close.set_defaults(func=cmd_close)

    subparsers.choices["build"].set_defaults(func=cmd_build)
    subparsers.choices["status"].set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "build" and hasattr(args, "port"):
        args.port = resolve_port(args.port)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
