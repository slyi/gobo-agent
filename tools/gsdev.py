#!/usr/bin/env python3
"""gobo-agent: GoboScript dev loop on the real Scratch runtime.

Runs goboscript builds against upstream @scratch/scratch-vm in a plain headless
browser (tools/scratchhost) — no Scratch Desktop, no TurboWarp Desktop, no
Electron. The real GPU is used by default, so performance numbers are
representative; --software forces SwiftShader on boxes without a GPU.

Requires (run `gsdev.py doctor` to check them):
    python 3.10+            the only Python runtime; no pip packages
    goboscript              the compiler, on PATH
    node + npm              for the scratch-vm host (one-time npm install)
    Chrome or Edge          the host browser (GSDEV_BROWSER overrides)

On Windows, process and port handling uses the standard library's ctypes
(Toolhelp32 + GetExtendedTcpTable), so no PowerShell, WMI, taskkill, or netstat
is required. On macOS/Linux it falls back to the usual ps/pgrep/lsof/fuser.

Commands:
    build       Compile the project to <project>.sb3
    run         Build, load into the host, and start the project
    screenshot  Build, load briefly, and capture the stage to a PNG
    stop        Stop the running project, leaving the host open
    close       Close the host browser and static server
    status      Print the sprites and run state
    get         Read variables from the running project
    set         Write a variable in the running project
    set_batch   Write several variables/lists in one atomic call
    watch       Sample variables every frame while the project runs
    frame       Print the runtime frame counter
    wait_frame  Wait for N runtime frames
    step        Pause and advance the runtime exactly N frames
    pause       Pause the runtime for manual stepping
    resume      Resume the runtime after pause/step
    restart     Re-run the green-flag scripts (keeps variables)
    render      Print frame/render counters (frame, rendered, event totals)
    record      Record variables on every frame (event-driven, no polling)
    trace       Dump the recorded per-frame rows
    stop_record Stop the frame recorder (keeps the rows)
    until       Record the exact frame a condition first holds
    broadcast   Start the 'when I receive' hats for a message
    broadcast_wait  Broadcast, then wait for its handlers to finish
    inspect     Dump targets, variables, costumes, and extensions
    props       Print a target's properties
    prop        Read or write one target property
    clones      Print the clone count per sprite
    errors      Dump captured VM/page errors and error-log count
    expect_no_errors  Assert there are no VM/page or error-log errors
    wait_until  Wait (event-driven) until a variable satisfies a condition
    wait_pixel  Wait (event-driven) until a stage pixel is a colour
    session     Batch get/set/watch lines from stdin over one connection
    test        Run session files as tests, with a summary or --json report
    doctor      Check that the required tools are installed
    selftest    Check the per-project host files and flags
    tasks       Check that the .vscode tasks resolve on this platform

Agents always pass --headless; the VS Code tasks run the host with a visible
window (real GPU). Keep the host open for a session: cold start is a one-time
cost, and both live edits and rebuilds reuse the warm host.

`run --leave-running` leaves the project running so `set`/`get`/`watch` can tune
values with no rebuild or reload. Selectors are `sprite.variable`,
`sprite.list[index]` (1-based), or a bare name for a Stage global; variables and
lists are read dynamically, so a `set` takes effect on the next frame. `watch`
prints `[WATCH +Nms] name=value` lines. This is data injection: values can be
poked freely, but structural script changes still need a rebuild + reload.

Pass --cpu RATE to emulate a slower CPU (e.g. --cpu 4 ~ a phone) via CDP, or
--port 0 (GSDEV_CDP_PORT=0) to auto-pick a free CDP port recorded in
tools/.gsdev-port for parallel A/B runs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import (  # noqa: E402
    CDP,
    CDPError,
    connect,
    list_targets,
)

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("GSDEV_PROJECT") or TOOLS_DIR.parent).resolve()
SB3_PATH = PROJECT_ROOT / (PROJECT_ROOT.name + ".sb3")
DEBUG_DIR = PROJECT_ROOT / "debug"
DEFAULT_PORT = int(os.environ.get("GSDEV_CDP_PORT", "9230"))
PORT_FILE = TOOLS_DIR / ".gsdev-port"
HOST_DIR = TOOLS_DIR / "scratchhost"
HOST_PROFILE_DIR = TOOLS_DIR / "scratchvm-profile"
HOST_SERVER_PORT = int(os.environ.get("GSDEV_HOST_PORT", "8077"))

# The host page (tools/scratchhost/host.js) installs the log shim and exposes
# window.__gsdev; Python drains it and reads errors/state via `window.__host`.


# When --json is set, commands that support it print one JSON object per result
# instead of human text. Default output is unchanged.
JSON_MODE = False


def configure_stdio() -> None:
    """Use UTF-8 for the CLI's stdio.

    Windows pipes default to the legacy code page, which mangles non-ASCII
    selectors/values (CJK etc.) piped into `session`/`test` and can raise on
    non-ASCII log output when stdout is redirected.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def log(message: str) -> None:
    stream = sys.stderr if JSON_MODE else sys.stdout
    print(f"[gsdev] {message}", file=stream, flush=True)


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


def terminate_pids(pids: list[int]) -> None:
    for pid in pids:
        if os.name == "nt":
            _win32_terminate(pid)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


# --- Headless scratch-vm host -------------------------------------------------

BROWSER_CANDIDATES = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
}


def find_browser() -> str | None:
    override = os.environ.get("GSDEV_BROWSER") or os.environ.get("CHROME_EXE")
    if override and Path(override).exists():
        return override
    for candidate in BROWSER_CANDIDATES.get(sys.platform, []):
        if Path(candidate).exists():
            return candidate
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def host_url() -> str:
    return f"http://127.0.0.1:{HOST_SERVER_PORT}/host.html"


def _detached_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def ensure_host_server() -> None:
    if port_open(HOST_SERVER_PORT):
        return
    if not (HOST_DIR / "host.html").exists():
        raise SystemExit(f"missing {HOST_DIR / 'host.html'}")
    if not (HOST_DIR / "node_modules").is_dir():
        raise SystemExit(
            "host dependencies are not installed; run:\n"
            f'  npm install --prefix "{HOST_DIR}"'
        )
    args = [
        sys.executable, "-m", "http.server", str(HOST_SERVER_PORT),
        "--bind", "127.0.0.1", "--directory", str(HOST_DIR),
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached_kwargs())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if port_open(HOST_SERVER_PORT):
            log(f"host server on http://127.0.0.1:{HOST_SERVER_PORT}")
            return
        time.sleep(0.1)
    raise SystemExit("host server did not start")


def host_target(port: int) -> dict | None:
    try:
        targets = list_targets(port=port, timeout=2.0)
    except Exception:
        return None
    for target in targets:
        if target.get("type") == "page" and "host.html" in str(target.get("url", "")):
            return target
    return None


def ensure_host(port: int, headless: bool, software: bool) -> tuple[CDP, bool]:
    # GSDEV_SOFTWARE=1 forces software GL without threading --software through
    # every command (CI sets it once so the test suites can run on GPU-less boxes).
    software = software or os.environ.get("GSDEV_SOFTWARE", "").strip().lower() not in (
        "", "0", "false",
    )
    target = host_target(port) if port_open(port) else None
    if target is not None:
        return connect(target), False
    browser = find_browser()
    if browser is None:
        raise SystemExit("no Chrome/Edge found; set GSDEV_BROWSER to the executable")
    ensure_host_server()
    HOST_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    flags = [
        browser, f"--remote-debugging-port={port}", f"--user-data-dir={HOST_PROFILE_DIR}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        # The host profile is reused and often killed rather than closed cleanly,
        # so suppress Chrome's "did not shut down correctly / restore pages" bubble
        # and other dialogs that would sit over the stage and steal input.
        "--noerrdialogs", "--disable-infobars",
        "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
        "--disable-features=InfiniteSessionRestore,SessionRestoreBubble",
    ]
    if headless:
        flags.append("--headless=new")
        if software:
            flags += ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    flags.append(host_url())
    log(f"launching {Path(browser).name}{' headless' if headless else ''}")
    subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached_kwargs())
    deadline = time.monotonic() + 60
    conn: CDP | None = None
    while time.monotonic() < deadline:
        target = host_target(port)
        if target is not None:
            try:
                if conn is None:
                    conn = connect(target)
                if conn.evaluate("!!(window.__host && window.__marks && window.__marks.ready)"):
                    log("host ready")
                    return conn, True
            except (CDPError, TimeoutError, OSError):
                pass
        time.sleep(0.2)
    raise SystemExit("scratch-vm host did not become ready")


def open_host(port: int) -> CDP | None:
    target = host_target(port) if port_open(port) else None
    if target is None:
        log(f"no scratch-vm host on port {port}; start the project with run first")
        return None
    return connect(target)


def apply_cpu_throttle(cdp: CDP, rate: float) -> None:
    """Emulate a slower CPU for the renderer (1 disables throttling)."""
    value = float(rate) if rate and rate > 1 else 1.0
    cdp.call("Emulation.setCPUThrottlingRate", {"rate": value})
    if value > 1:
        log(f"emulating a {value:g}x slower CPU")


def load_host_project(cdp: CDP, sb3_path: Path) -> dict:
    encoded = base64.b64encode(Path(sb3_path).read_bytes()).decode("ascii")
    script = (
        "(async () => { const b = atob('" + encoded + "');"
        " const a = new Uint8Array(b.length);"
        " for (let i = 0; i < b.length; i++) a[i] = b.charCodeAt(i);"
        " const t0 = performance.now(); await window.__host.load(a.buffer);"
        " const t1 = performance.now(); window.__host.start();"
        " return JSON.stringify({load: t1 - t0, started: performance.now()}); })()"
    )
    return json.loads(cdp.evaluate(script, await_promise=True, timeout=120))


def stop_host(cdp: CDP) -> None:
    cdp.evaluate("window.__host.stop()")


def close_host(port: int) -> None:
    process_name = Path(find_browser() or "chrome.exe").name.lower()
    pids = find_editor_pids(port, process_name, HOST_PROFILE_DIR)
    if pids:
        terminate_pids(pids)
        log(f"killed host browser (pid {', '.join(str(p) for p in pids)})")
    else:
        log("no host browser to close")
    server_pids = _port_owner_pids(HOST_SERVER_PORT)
    if server_pids:
        terminate_pids(server_pids)
        log(f"stopped host server (pid {', '.join(str(p) for p in server_pids)})")


def capture_stage_png(cdp: CDP) -> bytes:
    rect = host_stage_rect(cdp)
    data = cdp.call(
        "Page.captureScreenshot",
        {"format": "png", "clip": {**rect, "scale": 1}},
        timeout=20,
    )
    return base64.b64decode(data["data"])


def host_stage_rect(cdp: CDP) -> dict:
    return json.loads(cdp.evaluate(
        "(() => { const c = document.getElementById('stage');"
        " const r = c.getBoundingClientRect();"
        " return JSON.stringify({x: r.x + window.scrollX, y: r.y + window.scrollY,"
        " width: r.width, height: r.height}); })()"
    ))


def build_project_at(root: Path) -> Path:
    goboscript = shutil.which("goboscript")
    if goboscript is None:
        raise SystemExit("goboscript is not on PATH. Install the GoboScript compiler.")
    log(f"building {root}")
    sb3_path = root / (root.name + ".sb3")
    # Rely on cwd for the input: newer goboscript uses -i/--input and rejects a
    # positional directory, while older builds took a positional one.
    command = [goboscript, "build", "-o", str(sb3_path)]
    if JSON_MODE:
        # Keep stdout clean for the JSON report.
        result = subprocess.run(command, cwd=str(root), capture_output=True, text=True)
        print((result.stdout or "") + (result.stderr or ""), end="", file=sys.stderr, flush=True)
    else:
        result = subprocess.run(command, cwd=str(root))
    if result.returncode != 0:
        raise SystemExit(f"goboscript build failed (exit {result.returncode}).")
    return sb3_path


def build_project() -> Path:
    return build_project_at(PROJECT_ROOT)


def poll_host(cdp: CDP, since: int = 0) -> dict:
    """One evaluate per run loop: drained logs, stopped flag, and new errors."""
    return cdp.evaluate(f"window.__host.poll({int(since)})") or {}


def print_event(event: dict) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{event['level'].upper()} {stamp} {event['sprite']}] {event['value']}", flush=True)


def cmd_build(args: argparse.Namespace) -> int:
    build_project()
    log(f"built {SB3_PATH.name}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not args.no_build and not args.no_reload:
        build_project()
    t0 = time.monotonic()
    cdp, launched = ensure_host(args.port, args.headless, args.software)
    ready = time.monotonic() - t0
    apply_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
    try:
        if args.no_reload:
            log(
                f"host {'launched' if launched else 'warm'}: browser {ready:.2f}s, "
                "reusing the running project (--no-reload)"
            )
        else:
            info = load_host_project(cdp, SB3_PATH)
            log(
                f"host {'launched' if launched else 'warm'}: browser {ready:.2f}s, "
                f"load {info['load']:.0f} ms"
            )
        log("running; press Ctrl+C to stop")
        deadline = time.monotonic() + args.duration if args.duration else None
        errors_seen = 0
        try:
            while True:
                poll = poll_host(cdp, errors_seen)
                for event in poll.get("logs", []):
                    if event is None:
                        log("project stopped")
                        return 0
                    print_event(event)
                for error in poll.get("errors", []):
                    print_error(error)
                errors_seen = int(poll.get("count", errors_seen))
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if poll.get("stopped"):
                    log("project stopped")
                    return 0
                time.sleep(0.2)
        except KeyboardInterrupt:
            log("interrupted")
    finally:
        if getattr(args, "leave_running", False):
            log("leaving the project running")
        else:
            try:
                stop_host(cdp)
            except (CDPError, TimeoutError, OSError):
                pass
        cdp.close()
    return 0


def cmd_screenshot(args: argparse.Namespace) -> int:
    if not args.no_build:
        build_project()
    out_path = Path(args.out).expanduser() if args.out else DEBUG_DIR / "stage.png"
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    cdp, _ = ensure_host(args.port, args.headless, args.software)
    apply_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
    try:
        load_host_project(cdp, SB3_PATH)
        time.sleep(max(0, args.delay) / 1000.0)
        rect = host_stage_rect(cdp)
        data = cdp.call(
            "Page.captureScreenshot",
            {"format": "png", "clip": {**rect, "scale": 1}},
            timeout=20,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(data["data"]))
        log(f"screenshot: {out_path} ({int(rect['width'])}x{int(rect['height'])})")
    finally:
        try:
            stop_host(cdp)
        except (CDPError, TimeoutError, OSError):
            pass
        cdp.close()
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    cdp = open_host(args.port)
    if cdp is None:
        log("no project to stop")
        return 0
    try:
        stop_host(cdp)
        log("project stopped")
    finally:
        cdp.close()
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    close_host(args.port)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cdp = open_host(args.port)
    if cdp is None:
        return 1
    try:
        state = cdp.evaluate(
            "JSON.stringify({sprites: window.__host.vm.runtime.targets.map(t => t.getName()),"
            " running: window.__host.vm.runtime.threads.length > 0})"
        )
        log(state)
    finally:
        cdp.close()
    return 0


# --- Live tuning: read/write variables in a running project -------------------


def parse_selector(selector: str) -> tuple[str, str, int | None]:
    """Split ``sprite.variable[index]`` into target, name, and optional index.

    A bare name refers to a Stage (global) variable, matching how goboscript
    exposes globals. The index is 1-based, like Scratch lists. The sprite part
    may carry a ``#N`` clone suffix (``main#1.variable``), resolved by the
    host's ``findTarget``.
    """
    index = None
    base = selector
    if selector.endswith("]") and "[" in selector:
        head, _, tail = selector[:-1].rpartition("[")
        try:
            index = int(tail)
            base = head
        except ValueError:
            base = selector
    if "." in base:
        target, name = base.split(".", 1)
        return target or "stage", name, index
    return "stage", base, index


_UNSET = object()


def _var_js(target: str, name: str, index: int | None = None, value=_UNSET) -> str:
    idx = "null" if index is None else str(int(index))
    if value is _UNSET:
        mutate = ""
    else:
        literal = json.dumps(value)
        mutate = (
            " if (_idx === null) { _v.value = %s; }"
            " else if (Array.isArray(_v.value)) { _v.value[_idx - 1] = %s; }"
            " else { return {error: 'not a list: ' + %s}; }"
        ) % (literal, literal, json.dumps(name))
    return (
        "(() => {"
        " const _host = window.__host;"
        " if (!_host || !_host.findTarget) return {error: 'no host (open the project with run first)'};"
        " const _want = " + json.dumps(target) + ";"
        " const _t = _host.findTarget(_want);"
        " if (!_t) return {error: 'target not found: ' + _want};"
        " let _v = null;"
        " for (const _k in _t.variables) { if (_t.variables[_k].name === "
        + json.dumps(name) + ") { _v = _t.variables[_k]; break; } }"
        " if (!_v) return {error: 'variable not found: ' + " + json.dumps(name) + "};"
        " const _idx = " + idx + ";"
        + mutate +
        " const _out = (_idx === null)"
        "   ? _v.value"
        "   : (Array.isArray(_v.value) ? _v.value[_idx - 1] : undefined);"
        " return {value: _out, target: _want, name: " + json.dumps(name) + ", index: _idx};"
        "})()"
    )


def parse_value(raw: str):
    """Parse a CLI value as JSON when possible, otherwise keep it a string."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _watch_install_js(specs: list[tuple], labels: list[str], interval_ms: int) -> str:
    spec_list = "[" + ",".join(
        "{target: %s, name: %s, label: %s, index: %s}"
        % (
            json.dumps(target),
            json.dumps(name),
            json.dumps(label),
            "null" if index is None else str(int(index)),
        )
        for (target, name, index), label in zip(specs, labels)
    ) + "]"
    return (
        "(() => {"
        " const _host = window.__host;"
        " if (!_host || !_host.findTarget) return {error: 'no host (open the project with run first)'};"
        " const _specs = " + spec_list + ";"
        " const _refs = [];"
        " for (const _s of _specs) {"
        "   const _t = _host.findTarget(_s.target);"
        "   if (!_t) return {error: 'target not found: ' + _s.target};"
        "   let _v = null;"
        "   for (const _k in _t.variables) { if (_t.variables[_k].name === _s.name) { _v = _t.variables[_k]; break; } }"
        "   if (!_v) return {error: 'variable not found: ' + _s.name};"
        "   _refs.push({label: _s.label, ref: _v, index: _s.index});"
        " }"
        " const _read = (_r) => (_r.index === null)"
        "   ? _r.ref.value"
        "   : (Array.isArray(_r.ref.value) ? _r.ref.value[_r.index - 1] : undefined);"
        " const _fmt = (x) => {"
        "   if (Array.isArray(x)) { const s = JSON.stringify(x);"
        "     return s.length > 200 ? s.slice(0, 200) + '...(' + x.length + ')' : s; }"
        "   if (x !== null && typeof x === 'object') { try { return JSON.stringify(x); } catch (e) { return String(x); } }"
        "   return x; };"
        " const _prev = window.__gsdevWatch;"
        " if (_prev && _prev.timer) clearInterval(_prev.timer);"
        " const _W = window.__gsdevWatch = {on: true, samples: [], refs: _refs, t0: performance.now()};"
        " _W.timer = setInterval(() => { if (!_W.on) return;"
        "   const _row = [Math.round(performance.now() - _W.t0)];"
        "   for (const _r of _W.refs) _row.push(_fmt(_read(_r)));"
        "   _W.samples.push(_row);"
        "   if (_W.samples.length > 5000) _W.samples.splice(0, _W.samples.length - 2000);"
        " }, " + str(interval_ms) + ");"
        " return {ok: true, labels: _refs.map(r => r.label)};"
        "})()"
    )


DRAIN_WATCH_JS = (
    "JSON.stringify((window.__gsdevWatch"
    " && window.__gsdevWatch.samples.splice(0, window.__gsdevWatch.samples.length)) || [])"
)
STOP_WATCH_JS = (
    "(() => { if (window.__gsdevWatch) { window.__gsdevWatch.on = false;"
    " if (window.__gsdevWatch.timer) clearInterval(window.__gsdevWatch.timer); }"
    " return 'ok'; })()"
)


def open_live_cdp(port: int) -> CDP | None:
    """Attach to the running scratch-vm host."""
    return open_host(port)


def cmd_get(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = {}
        for selector in args.selectors:
            target, name, index = parse_selector(selector)
            result[selector] = cdp.evaluate(_var_js(target, name, index))
        log(json.dumps(result))
    finally:
        cdp.close()
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    target, name, index = parse_selector(args.selector)
    value = parse_value(args.value)
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(_var_js(target, name, index, value))
        log(json.dumps({"selector": args.selector, **result}))
    finally:
        cdp.close()
    return 0


def apply_batch(cdp: CDP, tokens: list[str]) -> None:
    """Set several variables/lists in ONE evaluate, so the VM cannot step between.

    ``tokens`` are ``selector=value`` (lists and indices supported). This is what
    removes the race where a multi-variable fixture is read with only one of the
    pair updated: a single evaluate runs as one JS task on the same thread as the
    VM step, so no thread can observe a half-applied update.
    """
    labels: list[str] = []
    exprs: list[str] = []
    for token in tokens:
        selector, sep, raw = token.partition("=")
        if not sep:
            raise SystemExit(f"bad assignment {token!r} (want selector=value)")
        target, name, index = parse_selector(selector)
        exprs.append(_var_js(target, name, index, parse_value(raw)))
        labels.append(selector)
    script = (
        "(() => { const out = [];"
        + "".join(f" out.push({expr});" for expr in exprs)
        + " return JSON.stringify(out); })()"
    )
    log(json.dumps({"set_batch": labels, "results": json.loads(cdp.evaluate(script))}))


def cmd_set_batch(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        apply_batch(cdp, args.assignments)
    finally:
        cdp.close()
    return 0


def _watch_run(
    cdp: CDP, selectors: list[str], duration_ms: float, interval_ms: int = 20
) -> dict:
    specs = [parse_selector(selector) for selector in selectors]
    labels = list(selectors)
    result = cdp.evaluate(_watch_install_js(specs, labels, max(1, int(interval_ms))))
    if not result or result.get("error"):
        error = (result or {}).get("error", "unknown error")
        if not JSON_MODE:
            log(f"watch failed: {error}")
        return {"ok": False, "error": error, "labels": labels, "rows": [], "summary": []}
    rows: list[list] = []
    deadline = time.monotonic() + duration_ms / 1000.0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        for row in json.loads(cdp.evaluate(DRAIN_WATCH_JS) or "[]"):
            rows.append(row)
            if JSON_MODE:
                continue
            values = " ".join(f"{labels[i]}={row[i + 1]}" for i in range(len(labels)))
            print(f"[WATCH +{row[0]}ms] {values}", flush=True)
    summary = []
    for i, label in enumerate(labels):
        numbers = [row[i + 1] for row in rows if isinstance(row[i + 1], (int, float))]
        if not numbers:
            continue
        mean = sum(numbers) / len(numbers)
        summary.append({
            "label": label, "n": len(numbers),
            "mean": mean, "min": min(numbers), "max": max(numbers),
        })
        if not JSON_MODE:
            print(
                f"[WATCH summary] {label} n={len(numbers)} mean={mean:.4g} "
                f"min={min(numbers):.4g} max={max(numbers):.4g}",
                flush=True,
            )
    return {"ok": True, "labels": labels, "rows": rows, "summary": summary}


def cmd_watch(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = _watch_run(cdp, args.selectors, args.duration * 1000.0, args.interval)
        if JSON_MODE:
            print(json.dumps({"watch": result}, separators=(",", ":")), flush=True)
    finally:
        try:
            cdp.evaluate(STOP_WATCH_JS)
        except (CDPError, TimeoutError, OSError):
            pass
        cdp.close()
    return 0


def run_session_lines(cdp: CDP, lines, quiet: bool = False) -> dict:
    """Run session lines over one open CDP connection.

    Prints the same ``[ASSERT ...]`` / ``[gsdev] ...`` output as the ``session``
    command and returns ``{"asserts": [{"ok", "msg"}], "failures": N}`` where
    ``failures`` counts every failed assertion and every timeout/error verb.
    """
    failures = 0
    asserts = []
    watches = []

    def record(ok: bool, message: str) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        if not quiet:
            print(f"[ASSERT {'ok' if ok else 'FAIL'}] {message}", flush=True)
        asserts.append({"ok": ok, "msg": message})

    try:
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            op = parts[0].lower()
            if op == "set" and len(parts) >= 3:
                target, name, index = parse_selector(parts[1])
                value = parse_value(" ".join(parts[2:]))
                result = cdp.evaluate(_var_js(target, name, index, value))
                log(json.dumps({"set": parts[1], **result}))
            elif op == "set_batch" and len(parts) >= 3:
                try:
                    apply_batch(cdp, parts[1:])
                except SystemExit as error:
                    log(f"session: {error}")
            elif op == "get" and len(parts) >= 2:
                result = {}
                for selector in parts[1:]:
                    target, name, index = parse_selector(selector)
                    result[selector] = cdp.evaluate(_var_js(target, name, index))
                log(json.dumps(result))
            elif op == "sleep" and len(parts) >= 2:
                time.sleep(float(parts[1]) / 1000.0)
            elif op == "watch" and len(parts) >= 3:
                watches.append(_watch_run(cdp, parts[2:], float(parts[1])))
            elif op == "mouse" and len(parts) >= 3:
                x, y = float(parts[1]), float(parts[2])
                mode = parts[3].lower() if len(parts) > 3 else "move"
                is_down = {"down": True, "up": False}.get(mode)
                cdp.evaluate(mouse_js(x, y, is_down))
                log(json.dumps({"mouse": [x, y], "down": is_down}))
            elif op == "click" and len(parts) >= 3:
                x, y = float(parts[1]), float(parts[2])
                cdp.evaluate(mouse_js(x, y, None))
                cdp.evaluate(mouse_js(x, y, True))
                time.sleep(0.05)
                cdp.evaluate(mouse_js(x, y, False))
                time.sleep(0.05)
                log(json.dumps({"click": [x, y]}))
            elif op == "key" and len(parts) >= 2:
                key = input_key_name(parts[1])
                mode = parts[2].lower() if len(parts) > 2 else "press"
                if mode == "down":
                    cdp.evaluate(key_js(key, True))
                elif mode == "up":
                    cdp.evaluate(key_js(key, False))
                else:
                    cdp.evaluate(key_js(key, True))
                    time.sleep(0.05)
                    cdp.evaluate(key_js(key, False))
                log(json.dumps({"key": key, "mode": mode}))
            elif op == "expect" and len(parts) >= 4:
                selector, operator = parts[1], parts[2]
                expected = parse_value(" ".join(parts[3:]))
                actual = cdp.evaluate(select_js(selector)).get("value")
                record(
                    compare(actual, operator, expected),
                    f"{selector} {operator} {expected!r} (actual {actual!r})",
                )
            elif op == "pixel" and len(parts) >= 3:
                result = json.loads(
                    cdp.evaluate(pixel_js(float(parts[1]), float(parts[2])), await_promise=True, timeout=30)
                )
                log(json.dumps(result))
            elif op == "expectpixel" and len(parts) >= 4:
                expected = parts[3].lower()
                actual = json.loads(
                    cdp.evaluate(pixel_js(float(parts[1]), float(parts[2])), await_promise=True, timeout=30)
                ).get("hex", "")
                record(
                    actual.lower() == expected,
                    f"pixel {parts[1]} {parts[2]} == {expected} (actual {actual})",
                )
            elif op == "frame" and len(parts) == 1:
                log(json.dumps({"frame": current_frame(cdp)}))
            elif op == "pause":
                cdp.evaluate("window.__host.pause()")
                log(json.dumps({"paused": True, "frame": current_frame(cdp)}))
            elif op == "resume":
                cdp.evaluate("window.__host.resume()")
                log(json.dumps({"resumed": True, "frame": current_frame(cdp)}))
            elif op == "restart":
                cdp.evaluate("window.__host.restart()")
                log(json.dumps({"restarted": True, "frame": current_frame(cdp)}))
            elif op == "step" and len(parts) >= 2:
                cdp.evaluate("window.__host.pause()")
                before = current_frame(cdp)
                frame = int(cdp.evaluate(f"window.__host.step({int(parts[1])})"))
                log(json.dumps({"stepped": frame - before, "frame": frame}))
            elif op == "waitframe" and len(parts) >= 2:
                count = int(parts[1])
                timeout = float(parts[2]) if len(parts) > 2 else 5000.0
                result = cdp.evaluate(
                    f"window.__host.waitFrames({count}, {int(timeout)})",
                    await_promise=True, timeout=timeout / 1000.0 + 5,
                )
                if result.get("ok"):
                    log(json.dumps({"waited": count, "frame": result.get("frame")}))
                else:
                    record(False, f"waitframe {count} timed out")
            elif op == "render":
                log(json.dumps(cdp.evaluate("window.__host.events()")))
            elif op == "record" and len(parts) >= 2:
                specs = [_spec(selector) for selector in parts[1:]]
                result = cdp.evaluate(f"window.__host.record({json.dumps(specs)}, 100000)")
                log(json.dumps({"record": result}))
            elif op == "trace":
                log(json.dumps(cdp.evaluate("window.__host.trace(false)")))
            elif op == "stoprecord":
                log(json.dumps(cdp.evaluate("window.__host.stopRecord()")))
            elif op == "until" and len(parts) >= 4:
                spec = _spec(parts[1])
                value = parse_value(parts[3])
                pause = len(parts) > 4 and parts[4].lower() == "pause"
                maxf = int(parts[5]) if len(parts) > 5 else 100000
                script = (
                    f"window.__host.until({json.dumps(spec)}, {json.dumps(parts[2])}, "
                    f"{json.dumps(value)}, {'true' if pause else 'false'}, {maxf})"
                )
                log(json.dumps({"until": cdp.evaluate(script)}))
            elif op == "broadcast" and len(parts) >= 2:
                result = cdp.evaluate(
                    f"window.__host.broadcast({json.dumps(parts[1])})") or {}
                log(json.dumps({
                    "broadcast": parts[1], "threads": result.get("threads", 0)
                }))
            elif op == "broadcast_wait" and len(parts) >= 2:
                timeout = float(parts[2]) if len(parts) > 2 else 5000.0
                result = cdp.evaluate(
                    _broadcast_wait_js(parts[1], int(timeout)),
                    await_promise=True, timeout=timeout / 1000.0 + 5,
                ) or {}
                if result.get("ok"):
                    log(json.dumps({
                        "broadcast": parts[1],
                        "threads": result.get("threads", 0),
                        "waitedFrames": result.get("waitedFrames"),
                    }))
                else:
                    record(False, f"broadcast_wait {parts[1]} timed out")
            elif op == "inspect":
                target = parts[1] if len(parts) > 1 else ""
                log(json.dumps(cdp.evaluate(_inspect_js(target))))
            elif op == "props" and len(parts) >= 2:
                result = cdp.evaluate(f"window.__host.props({json.dumps(parts[1])})") or {}
                log(json.dumps(result))
                if result.get("error"):
                    record(False, f"props {parts[1]}: {result['error']}")
            elif op == "prop" and len(parts) >= 3:
                if len(parts) >= 4:
                    value = parse_value(" ".join(parts[3:]))
                    script = (
                        f"window.__host.setProp({json.dumps(parts[1])}, "
                        f"{json.dumps(parts[2])}, {json.dumps(value)})"
                    )
                else:
                    script = (
                        f"window.__host.getProp({json.dumps(parts[1])}, "
                        f"{json.dumps(parts[2])})"
                    )
                result = cdp.evaluate(script) or {}
                log(json.dumps({"target": parts[1], "prop": parts[2], **result}))
                if result.get("error"):
                    record(False, f"prop {parts[1]} {parts[2]}: {result['error']}")
            elif op == "clones":
                log(json.dumps({"clones": cdp.evaluate("window.__host.clones()")}))
            elif op == "errors":
                log(json.dumps(get_errors(cdp)))
            elif op == "expect_no_errors":
                info = get_errors(cdp)
                record(
                    _errors_ok(info),
                    f"expect_no_errors (vm/page {info.get('count', 0)}, "
                    f"log errors {info.get('logErrors', 0)})",
                )
            elif op == "waituntil" and len(parts) >= 4:
                timeout = float(parts[4]) if len(parts) > 4 else 5000.0
                spec = _spec(parts[1])
                value = parse_value(parts[3])
                result = cdp.evaluate(
                    f"window.__host.waitUntil({json.dumps(spec)}, {json.dumps(parts[2])}, "
                    f"{json.dumps(value)}, {int(timeout)})",
                    await_promise=True, timeout=timeout / 1000.0 + 5,
                ) or {}
                if result.get("ok"):
                    log(json.dumps({
                        "waituntil": parts[1], "value": result.get("value"),
                        "frame": result.get("frame"),
                    }))
                else:
                    record(False, f"waituntil {parts[1]} {parts[2]} {value} timed out")
            elif op == "waitpixel" and len(parts) >= 4:
                timeout = float(parts[4]) if len(parts) > 4 else 5000.0
                result = cdp.evaluate(
                    f"window.__host.waitPixel({float(parts[1])!r}, {float(parts[2])!r}, "
                    f"{json.dumps(parts[3])}, {int(timeout)})",
                    await_promise=True, timeout=timeout / 1000.0 + 5,
                ) or {}
                if result.get("ok"):
                    log(json.dumps({
                        "waitpixel": [parts[1], parts[2]], "hex": result.get("hex"),
                        "frame": result.get("frame"),
                    }))
                else:
                    record(False, f"waitpixel {parts[1]} {parts[2]} == "
                                  f"{parts[3].lower()} timed out")
            else:
                log(f"session: unknown command {line!r}")
    finally:
        try:
            cdp.evaluate(STOP_WATCH_JS)
        except (CDPError, TimeoutError, OSError):
            pass
    return {"asserts": asserts, "failures": failures, "watches": watches}


def cmd_session(args: argparse.Namespace) -> int:
    """Run a batch of commands over one CDP connection.

    One operation per line::

        set QuadFiller.res 4
        set_batch main.dotx=0 main.doty=0
        sleep 300
        mouse 0 0 down
        mouse 0 0 up
        click 0 0
        key space press
        watch 1000 QuadFiller.drawcount QuadFiller.rendertime
        broadcast go
        broadcast_wait go 2000
        expect QuadFiller.res == 4

    Coordinates are Scratch stage coordinates (0,0 is centre). `expect` prints
    `[ASSERT ok|FAIL]` and makes the command exit non-zero if any expectation
    fails, so a session file doubles as a unit test. `--json` prints a structured
    result instead of the per-line logs.
    """
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = run_session_lines(cdp, sys.stdin, quiet=JSON_MODE)
    finally:
        cdp.close()
    if JSON_MODE:
        print(json.dumps({"session": result}, separators=(",", ":")), flush=True)
    return 1 if result["failures"] else 0


# --- Test runner --------------------------------------------------------------
#
# `test FILE...` runs one or more session files, reloading the project before
# each by default. The project is inferred from the session file's directory when
# that directory looks like a goboscript project, otherwise GSDEV_PROJECT is
# used. `test --json` prints a single JSON report:
#   {files:[{path, asserts:[{ok, msg}], failures, errors, screenshot?}],
#    totals:{files, asserts, failures}}

ASSERT_RE = re.compile(r"^\[ASSERT (ok|FAIL)\] (.*)$")


def parse_asserts(output: str) -> list[dict]:
    asserts = []
    for line in output.splitlines():
        match = ASSERT_RE.match(line.strip())
        if match:
            asserts.append({"ok": match.group(1) == "ok", "msg": match.group(2)})
    return asserts


def project_for_test(path: Path) -> Path:
    parent = path.resolve().parent
    if (parent / "goboscript.toml").exists():
        return parent
    return PROJECT_ROOT


def cmd_test(args: argparse.Namespace) -> int:
    artifacts = Path(args.artifacts).expanduser() if args.artifacts else DEBUG_DIR
    if not artifacts.is_absolute():
        artifacts = PROJECT_ROOT / artifacts
    report = {"files": [], "totals": {"files": 0, "asserts": 0, "failures": 0}}
    any_failed = False
    for item in args.files:
        path = Path(item)
        root = project_for_test(path)
        sb3_path = root / (root.name + ".sb3")
        entry = {"path": str(path), "asserts": [], "failures": 0, "errors": 0}
        if not args.no_build:
            build_project_at(root)
        cdp, _ = ensure_host(args.port, args.headless, args.software)
        try:
            if not args.no_reload:
                load_host_project(cdp, sb3_path)
        finally:
            cdp.close()
        env = dict(os.environ, GSDEV_PROJECT=str(root))
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "session", "--port", str(args.port)],
            input=path.read_text(encoding="utf-8"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=args.timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if not JSON_MODE:
            print(output, end="", flush=True)
        entry["asserts"] = parse_asserts(output)
        entry["failures"] = sum(1 for item in entry["asserts"] if not item["ok"])
        cdp = open_host(args.port)
        if cdp is None:
            failed = True
        else:
            try:
                info = get_errors(cdp)
                entry["errors"] = int(info.get("count", 0)) + int(info.get("logErrors", 0))
                failed = entry["failures"] > 0 or entry["errors"] > 0 or proc.returncode != 0
                if failed and not args.no_screenshots:
                    artifacts.mkdir(parents=True, exist_ok=True)
                    screenshot = artifacts / f"{path.stem}-fail.png"
                    screenshot.write_bytes(capture_stage_png(cdp))
                    entry["screenshot"] = str(screenshot)
            finally:
                cdp.close()
        any_failed = any_failed or failed
        report["files"].append(entry)
        report["totals"]["files"] += 1
        report["totals"]["asserts"] += len(entry["asserts"])
        report["totals"]["failures"] += entry["failures"]
        if not JSON_MODE:
            log(
                f"[{'ok' if not failed else 'FAIL'}] {path}: {len(entry['asserts'])} assert(s), "
                f"{entry['failures']} failure(s), {entry['errors']} error(s)"
            )
    if JSON_MODE:
        print(json.dumps(report, separators=(",", ":")), flush=True)
    else:
        log(
            f"test: {report['totals']['files']} file(s), "
            f"{report['totals']['asserts']} assert(s), "
            f"{report['totals']['failures']} failure(s)"
        )
    return 1 if any_failed else 0


# --- Input injection (agent-first: deterministic, headless-safe) --------------
#
# Injection happens inside the VM (vm.postIOData), not via OS-level events, so it
# works headless and is fully scriptable for unit tests. Coordinates are Scratch
# stage coordinates (0,0 is centre, +x right, +y up).

INPUT_KEY_ALIASES = {
    "space": " ",
    "enter": "Enter",
    "return": "Enter",
    "up": "ArrowUp",
    "up arrow": "ArrowUp",
    "down": "ArrowDown",
    "down arrow": "ArrowDown",
    "left": "ArrowLeft",
    "left arrow": "ArrowLeft",
    "right": "ArrowRight",
    "right arrow": "ArrowRight",
}


def input_key_name(name: str) -> str:
    return INPUT_KEY_ALIASES.get(name.strip().lower(), name)


def mouse_js(x: float, y: float, is_down: bool | None) -> str:
    down = "undefined" if is_down is None else ("true" if is_down else "false")
    return (
        "(() => {"
        " const vm = window.__host.vm;"
        " const r = document.getElementById('stage').getBoundingClientRect();"
        " const sw = vm.runtime.constructor.STAGE_WIDTH || 480;"
        " const sh = vm.runtime.constructor.STAGE_HEIGHT || 360;"
        f" const px = ({float(x)!r} / sw + 0.5) * r.width;"
        f" const py = (0.5 - {float(y)!r} / sh) * r.height;"
        " vm.postIOData('mouse', {x: px, y: py, canvasWidth: r.width,"
        f" canvasHeight: r.height, isDown: {down}}});"
        " return 'ok'; })()"
    )


def key_js(key: str, is_down: bool) -> str:
    pressed = "true" if is_down else "false"
    return (
        "(() => { window.__host.vm.postIOData('keyboard', {key: "
        + json.dumps(key) + ", isDown: " + pressed + "}); return 'ok'; })()"
    )


def select_js(selector: str) -> str:
    target, name, index = parse_selector(selector)
    return _var_js(target, name, index)


def compare(actual, op: str, expected) -> bool:
    """Compare a VM value with an expected value, numerically when possible."""
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError):
        left, right = str(actual), str(expected)
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    raise SystemExit(f"unknown operator {op!r}")


def cmd_mouse(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        is_down = True if args.down else (False if args.up else None)
        cdp.evaluate(mouse_js(args.x, args.y, is_down))
        log(json.dumps({"mouse": [args.x, args.y], "down": is_down}))
    finally:
        cdp.close()
    return 0


def cmd_click(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        settle = max(0.0, args.settle) / 1000.0
        cdp.evaluate(mouse_js(args.x, args.y, None))
        cdp.evaluate(mouse_js(args.x, args.y, True))
        time.sleep(settle)
        cdp.evaluate(mouse_js(args.x, args.y, False))
        time.sleep(settle)
        log(json.dumps({"click": [args.x, args.y]}))
    finally:
        cdp.close()
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        key = input_key_name(args.name)
        settle = max(0.0, args.settle) / 1000.0
        if args.down:
            cdp.evaluate(key_js(key, True))
            mode = "down"
        elif args.up:
            cdp.evaluate(key_js(key, False))
            mode = "up"
        else:
            cdp.evaluate(key_js(key, True))
            time.sleep(settle)
            cdp.evaluate(key_js(key, False))
            mode = "press"
        log(json.dumps({"key": key, "mode": mode}))
    finally:
        cdp.close()
    return 0


def pixel_js(x: float, y: float) -> str:
    return (
        "(async () => {"
        " const vm = window.__host.vm;"
        " const renderer = vm.runtime.renderer;"
        " if (!renderer || typeof renderer.requestSnapshot !== 'function') {"
        "   return JSON.stringify({error: 'requestSnapshot unavailable'});"
        " }"
        " const url = await new Promise((resolve) => {"
        "   try { renderer.requestSnapshot((u) => resolve(u || null)); } catch (e) { resolve(null); }"
        " });"
        " if (!url) return JSON.stringify({error: 'snapshot failed'});"
        " const img = new Image();"
        " await new Promise((resolve, reject) => { img.onload = resolve; img.onerror = reject; img.src = url; });"
        " const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;"
        " const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0, img.width, img.height);"
        " const sw = vm.runtime.constructor.STAGE_WIDTH || 480;"
        " const sh = vm.runtime.constructor.STAGE_HEIGHT || 360;"
        f" const ix = Math.round(({float(x)!r} / sw + 0.5) * img.width);"
        f" const iy = Math.round((0.5 - {float(y)!r} / sh) * img.height);"
        " const px = ctx.getImageData(Math.min(ix, img.width - 1), Math.min(iy, img.height - 1), 1, 1).data;"
        " const hex = '#' + [px[0], px[1], px[2]].map(v => v.toString(16).padStart(2, '0')).join('');"
        f" return JSON.stringify({{rgba: [px[0], px[1], px[2], px[3]], hex: hex, x: {float(x)!r}, y: {float(y)!r}}});"
        " })()"
    )


def cmd_pixel(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = json.loads(cdp.evaluate(pixel_js(args.x, args.y), await_promise=True, timeout=30))
        if result.get("error"):
            log(f"pixel failed: {result['error']}")
            return 1
        log(json.dumps(result))
    finally:
        cdp.close()
    return 0


# --- Deterministic time -------------------------------------------------------
#
# The host wraps runtime._step to count frames, so tests can wait for frames or
# advance them by hand instead of sleeping. Manual stepping does not advance
# wall-clock time, so timer/`wait` blocks need real time (use run/watch).


def current_frame(cdp: CDP) -> int:
    return int(cdp.evaluate("window.__gsdev.frame"))


def cmd_frame(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        log(json.dumps({"frame": current_frame(cdp)}))
    finally:
        cdp.close()
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        cdp.evaluate("window.__host.pause()")
        log(json.dumps({"paused": True, "frame": current_frame(cdp)}))
    finally:
        cdp.close()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        cdp.evaluate("window.__host.resume()")
        log(json.dumps({"resumed": True, "frame": current_frame(cdp)}))
    finally:
        cdp.close()
    return 0


def cmd_step(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        cdp.evaluate("window.__host.pause()")
        before = current_frame(cdp)
        frame = int(cdp.evaluate(f"window.__host.step({int(args.count)})"))
        log(json.dumps({"stepped": frame - before, "frame": frame}))
    finally:
        cdp.close()
    return 0


def cmd_wait_frame(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(
            f"window.__host.waitFrames({int(args.count)}, {int(args.timeout)})",
            await_promise=True, timeout=args.timeout / 1000.0 + 5,
        )
        if result.get("ok"):
            log(json.dumps({"waited": int(args.count), "frame": result.get("frame")}))
            return 0
        log(f"wait_frame timed out after {args.timeout} ms (frame {result.get('frame')}, wanted +{args.count})")
        return 1
    finally:
        cdp.close()


def cmd_restart(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        cdp.evaluate("window.__host.restart()")
        log(json.dumps({"restarted": True, "frame": current_frame(cdp)}))
    finally:
        cdp.close()
    return 0


def _spec(selector: str) -> dict:
    target, name, index = parse_selector(selector)
    return {"target": target, "name": name, "index": index, "label": selector}


def cmd_render(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        log(json.dumps(cdp.evaluate("window.__host.events()")))
    finally:
        cdp.close()
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        specs = [_spec(selector) for selector in args.selectors]
        result = cdp.evaluate(f"window.__host.record({json.dumps(specs)}, {int(args.max)})")
        log(json.dumps({"record": result}))
    finally:
        cdp.close()
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(f"window.__host.trace({'true' if args.clear else 'false'})")
        log(json.dumps(result))
    finally:
        cdp.close()
    return 0


def cmd_stop_record(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        log(json.dumps(cdp.evaluate("window.__host.stopRecord()")))
    finally:
        cdp.close()
    return 0


def cmd_until(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        spec = _spec(args.selector)
        value = parse_value(args.value)
        script = (
            f"window.__host.until({json.dumps(spec)}, {json.dumps(args.op)}, "
            f"{json.dumps(value)}, {'true' if args.pause else 'false'}, {int(args.max)})"
        )
        log(json.dumps({"until": cdp.evaluate(script)}))
    finally:
        cdp.close()
    return 0


# --- Events: broadcasts -------------------------------------------------------
#
# A broadcast is `runtime.startHats('event_whenbroadcastreceived', ...)`, the
# same call Scratch's own `event_broadcast` primitive makes. `broadcast_wait`
# waits (event-driven, on the per-frame 'gsdev:render' hook) until every thread
# the hat started has left `runtime.threads`. It needs the runtime running: a
# paused runtime never steps, so it can only time out.


def cmd_broadcast(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(f"window.__host.broadcast({json.dumps(args.name)})")
        log(json.dumps({"broadcast": args.name, "threads": (result or {}).get("threads", 0)}))
    finally:
        cdp.close()
    return 0


def _broadcast_wait_js(name: str, timeout_ms: int) -> str:
    return f"window.__host.broadcastWait({json.dumps(name)}, {int(timeout_ms)})"


def cmd_broadcast_wait(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(
            _broadcast_wait_js(args.name, args.timeout),
            await_promise=True, timeout=args.timeout / 1000.0 + 5,
        ) or {}
        if result.get("ok"):
            log(json.dumps({
                "broadcast": args.name,
                "threads": result.get("threads", 0),
                "waitedFrames": result.get("waitedFrames"),
            }))
            return 0
        print(f"[ASSERT FAIL] broadcast_wait {args.name} timed out", flush=True)
        return 1
    finally:
        cdp.close()


# --- Introspection: targets, properties, clones -------------------------------
#
# Targets are addressed by sprite name, with an optional `#N` clone suffix:
# `main` is the original, `main#1` the first clone (see `findTarget` in host.js).
# Clone indices are only stable within a frame — they shift as clones are
# created and deleted — so resolve them at call time.


def _inspect_js(target: str) -> str:
    return f"window.__host.inspect({json.dumps(target)})"


def cmd_inspect(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(_inspect_js(args.target)) or {}
        log(json.dumps(result))
        return 1 if result.get("error") else 0
    finally:
        cdp.close()


def cmd_props(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(f"window.__host.props({json.dumps(args.target)})") or {}
        log(json.dumps(result))
        return 1 if result.get("error") else 0
    finally:
        cdp.close()


def cmd_prop(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        if args.value is None:
            script = (
                f"window.__host.getProp({json.dumps(args.target)}, {json.dumps(args.name)})"
            )
        else:
            value = parse_value(args.value)
            script = (
                f"window.__host.setProp({json.dumps(args.target)}, "
                f"{json.dumps(args.name)}, {json.dumps(value)})"
            )
        result = cdp.evaluate(script) or {}
        log(json.dumps({"target": args.target, "prop": args.name, **result}))
        return 1 if result.get("error") else 0
    finally:
        cdp.close()


def cmd_clones(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        log(json.dumps({"clones": cdp.evaluate("window.__host.clones()")}))
    finally:
        cdp.close()
    return 0


# --- Runtime errors -----------------------------------------------------------
#
# scratch-vm has no error event. The host wraps runtime._step (capturing a
# throwing thread and letting the runtime continue) and listens for
# window.onerror / unhandledrejection. `errors` dumps the captured list plus the
# count of goboscript error-level logs; `expect_no_errors` asserts both are zero.


def get_errors(cdp: CDP) -> dict:
    return cdp.evaluate("window.__host.errors()") or {}


def print_error(error: dict) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(
        f"[ERROR {error.get('where', 'vm')} {stamp}] {error.get('message', 'error')}",
        flush=True,
    )


def _errors_ok(info: dict) -> bool:
    return int(info.get("count", 0)) == 0 and int(info.get("logErrors", 0)) == 0


def cmd_errors(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        log(json.dumps(get_errors(cdp)))
    finally:
        cdp.close()
    return 0


def cmd_expect_no_errors(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        info = get_errors(cdp)
        ok = _errors_ok(info)
        print(
            f"[ASSERT {'ok' if ok else 'FAIL'}] expect_no_errors "
            f"(vm/page {info.get('count', 0)}, log errors {info.get('logErrors', 0)})",
            flush=True,
        )
        return 0 if ok else 1
    finally:
        cdp.close()


# --- Event-driven waits -------------------------------------------------------
#
# `wait_until` / `wait_pixel` resolve from the host's per-frame 'gsdev:render'
# hook (the same event `until` uses), not from a timer poll. A wall-clock
# `--timeout` is only a backstop for a condition that never holds; a timeout is a
# failure so these double as assertions.


def cmd_wait_until(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        spec = _spec(args.selector)
        value = parse_value(args.value)
        script = (
            f"window.__host.waitUntil({json.dumps(spec)}, {json.dumps(args.op)}, "
            f"{json.dumps(value)}, {int(args.timeout)})"
        )
        result = cdp.evaluate(script, await_promise=True, timeout=args.timeout / 1000.0 + 5) or {}
        if result.get("ok"):
            log(json.dumps({
                "wait_until": args.selector,
                "value": result.get("value"),
                "frame": result.get("frame"),
            }))
            return 0
        if result.get("error"):
            log(f"wait_until failed: {result['error']}")
            return 1
        print(f"[ASSERT FAIL] wait_until {args.selector} {args.op} {value} timed out", flush=True)
        return 1
    finally:
        cdp.close()


def cmd_wait_pixel(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        script = (
            f"window.__host.waitPixel({float(args.x)!r}, {float(args.y)!r}, "
            f"{json.dumps(args.hex)}, {int(args.timeout)})"
        )
        result = cdp.evaluate(script, await_promise=True, timeout=args.timeout / 1000.0 + 5) or {}
        if result.get("ok"):
            log(json.dumps({
                "wait_pixel": [args.x, args.y],
                "hex": result.get("hex"),
                "frame": result.get("frame"),
            }))
            return 0
        print(
            f"[ASSERT FAIL] wait_pixel {args.x} {args.y} == {args.hex.lower()} timed out",
            flush=True,
        )
        return 1
    finally:
        cdp.close()


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

    node = shutil.which("node")
    if node:
        ok("node", _tool_version([node, "--version"]) or node)
    else:
        fail("node", "not on PATH (needed by the scratch-vm host)")

    npm = shutil.which("npm")
    if npm:
        ok("npm", _tool_version([npm, "--version"]) or npm)
    else:
        fail("npm", "not on PATH")

    browser = find_browser()
    if browser:
        ok("browser", browser)
    else:
        fail("browser", "no Chrome/Edge found; set GSDEV_BROWSER to the executable")

    if (HOST_DIR / "node_modules").is_dir():
        ok("host deps", str(HOST_DIR / "node_modules"))
    else:
        warn("host deps", f'not installed; run: npm install --prefix "{HOST_DIR}"')

    if port_open(HOST_SERVER_PORT):
        ok("host server", f"http://127.0.0.1:{HOST_SERVER_PORT}")
    else:
        ok("host server", "not running (started on demand)")

    print("", flush=True)
    if failures:
        print(f"[FAIL] doctor - {failures} required dependency(ies) missing", flush=True)
    elif warnings:
        print(f"[ok  ] doctor - no failures ({warnings} warning(s))", flush=True)
    else:
        print("[ok  ] doctor - all dependencies found", flush=True)
    return 1 if failures else 0


def cmd_selftest(args: argparse.Namespace) -> int:
    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        if not condition:
            failures += 1
        suffix = f" - {detail}" if detail else ""
        print(f"[{'ok' if condition else 'FAIL':4}] {label}{suffix}", flush=True)

    check(
        "host page",
        (HOST_DIR / "host.html").exists() and (HOST_DIR / "host.js").exists(),
        str(HOST_DIR),
    )
    check(
        "browser candidates",
        bool(BROWSER_CANDIDATES.get("win32")) and bool(BROWSER_CANDIDATES.get("darwin")),
        "chrome/edge paths",
    )
    check("host port", isinstance(HOST_SERVER_PORT, int) and HOST_SERVER_PORT > 0, str(HOST_SERVER_PORT))
    check(
        "host profile",
        HOST_PROFILE_DIR.name == "scratchvm-profile",
        str(HOST_PROFILE_DIR),
    )

    if failures:
        print(f"[FAIL] selftest - {failures} check(s) failed", flush=True)
    else:
        print("[ok  ] selftest - host logic matches expectations", flush=True)
    return 1 if failures else 0


def _task_platform_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "osx"
    return "linux"


def _task_field(task: dict, key: str, platform_key: str, default=None):
    override = task.get(platform_key) or {}
    if key in override:
        return override[key]
    return task.get(key, default)


def _task_substitute(value, root: Path):
    if isinstance(value, str):
        return value.replace("${workspaceFolder}", str(root))
    if isinstance(value, list):
        return [_task_substitute(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _task_substitute(item, root) for key, item in value.items()}
    return value


def cmd_tasks(args: argparse.Namespace) -> int:
    """Check that the .vscode process tasks resolve on this platform.

    VS Code runs a process task by spawning its command with the configured
    args/cwd/env after substituting ${workspaceFolder} and applying any
    windows/osx/linux override. This emulates that resolution, so a task that
    points at a missing script, a stale subcommand, or an unknown backend fails
    here instead of in the Tasks UI. With --run it also executes the tasks that
    do not launch an editor.
    """
    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        if not condition:
            failures += 1
        suffix = f" - {detail}" if detail else ""
        print(f"[{'ok' if condition else 'FAIL':4}] {label}{suffix}", flush=True)

    tasks_path = PROJECT_ROOT / ".vscode" / "tasks.json"
    if not tasks_path.exists():
        check(".vscode/tasks.json", False, "missing")
        return 1
    try:
        tasks = json.loads(tasks_path.read_text(encoding="utf-8")).get("tasks", [])
    except (OSError, json.JSONDecodeError) as error:
        check(".vscode/tasks.json", False, str(error))
        return 1

    platform_key = _task_platform_key()
    known = {"build", "status", "doctor", "selftest", "run", "screenshot", "stop", "close"}
    runnable = {"build", "doctor", "stop", "close"}
    for task in tasks:
        label = task.get("label", "<unlabeled>")
        command = _task_substitute(_task_field(task, "command", platform_key), PROJECT_ROOT)
        arguments = _task_substitute(_task_field(task, "args", platform_key, []), PROJECT_ROOT)
        options = _task_substitute(_task_field(task, "options", platform_key, {}), PROJECT_ROOT)
        resolved = shutil.which(command) if command else None
        script = arguments[0] if arguments else ""
        subcommand = arguments[1] if len(arguments) > 1 else ""
        backend = (options.get("env") or {}).get("GSDEV_BACKEND")
        cwd = options.get("cwd") or str(PROJECT_ROOT)
        reasons = []
        if not resolved:
            reasons.append(f"command {command!r} not on PATH")
        if not script or not Path(script).exists():
            reasons.append(f"script {script!r} missing")
        if subcommand not in known:
            reasons.append(f"unknown subcommand {subcommand!r}")
        if backend is not None and backend not in ("scratch", "turbowarp"):
            reasons.append(f"bad GSDEV_BACKEND {backend!r}")
        if not Path(cwd).is_dir():
            reasons.append(f"cwd {cwd!r} missing")
        check(label, not reasons, "; ".join(reasons) or f"{subcommand} via {command}")

        if args.run and subcommand in runnable and resolved:
            env = dict(os.environ)
            env.update(options.get("env") or {})
            result = subprocess.run(
                [resolved, *arguments], cwd=cwd, env=env, capture_output=True, text=True
            )
            check(f"{label} (executed)", result.returncode == 0, f"exit {result.returncode}")

    if failures:
        print(f"[FAIL] tasks - {failures} check(s) failed", flush=True)
    else:
        suffix = " and ran" if args.run else ""
        print(f"[ok  ] tasks - all .vscode tasks resolve{suffix}", flush=True)
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

    def add_software(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--software",
            action="store_true",
            help="force software GL (SwiftShader) on headless boxes without a GPU",
        )

    def add_json(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--json", action="store_true", help="emit a JSON report")

    add_port(subparsers.add_parser("build", help="compile the project"))
    add_port(subparsers.add_parser("status", help="show sprites and run state"))

    doctor = subparsers.add_parser("doctor", help="check that required tools are available")
    add_port(doctor)
    doctor.set_defaults(func=cmd_doctor)

    selftest = subparsers.add_parser(
        "selftest", help="check per-platform path/flag logic (incl. macOS)"
    )
    selftest.set_defaults(func=cmd_selftest)

    tasks = subparsers.add_parser("tasks", help="check .vscode tasks resolve on this platform")
    tasks.add_argument("--run", action="store_true", help="also execute the non-launching tasks")
    tasks.set_defaults(func=cmd_tasks)

    run = subparsers.add_parser("run", help="build, start, and stream logs")
    add_port(run)
    add_cpu(run)
    run.add_argument("--no-build", action="store_true", help="skip goboscript build")
    run.add_argument(
        "--no-reload",
        action="store_true",
        help="reuse the running project on the page (no build, no reload)",
    )
    run.add_argument("--duration", type=float, default=0.0, help="stop after N seconds")
    run.add_argument(
        "--leave-running",
        action="store_true",
        help="keep the project running after exit (for get/set/watch)",
    )
    run.add_argument("--headless", action="store_true", help="no visible browser window")
    add_software(run)
    run.set_defaults(func=cmd_run)

    shot = subparsers.add_parser("screenshot", help="capture the stage to a PNG")
    add_port(shot)
    add_cpu(shot)
    shot.add_argument("--no-build", action="store_true", help="skip goboscript build")
    shot.add_argument("--out", default="", help="output path (default debug/stage.png)")
    shot.add_argument("--delay", type=int, default=1200, help="wait before capture in ms")
    shot.add_argument("--headless", action="store_true", help="no visible browser window")
    add_software(shot)
    shot.set_defaults(func=cmd_screenshot)

    stop = subparsers.add_parser("stop", help="stop the running project")
    add_port(stop)
    stop.set_defaults(func=cmd_stop)

    get = subparsers.add_parser("get", help="read variables from the running project")
    add_port(get)
    get.add_argument("selectors", nargs="+", help="sprite.variable (bare name = stage global)")
    get.set_defaults(func=cmd_get)

    set_ = subparsers.add_parser("set", help="write a variable in the running project")
    add_port(set_)
    set_.add_argument("selector", help="sprite.variable (bare name = stage global)")
    set_.add_argument("value", help="JSON value, or a string when not valid JSON")
    set_.set_defaults(func=cmd_set)

    set_batch = subparsers.add_parser(
        "set_batch", help="set several variables/lists in one atomic call (no race)"
    )
    add_port(set_batch)
    set_batch.add_argument(
        "assignments",
        nargs="+",
        help="selector=value pairs, e.g. main.dotx=0 main.doty=0",
    )
    set_batch.set_defaults(func=cmd_set_batch)

    watch = subparsers.add_parser("watch", help="sample variables every frame while running")
    add_port(watch)
    watch.add_argument("selectors", nargs="+", help="sprite.variable (bare name = stage global)")
    watch.add_argument("--duration", type=float, default=5.0, help="stop after N seconds")
    watch.add_argument("--interval", type=int, default=20, help="sampling interval in ms")
    watch.set_defaults(func=cmd_watch)

    session = subparsers.add_parser(
        "session", help="run a batch of get/set/input/expect lines from stdin"
    )
    add_port(session)
    add_json(session)
    session.set_defaults(func=cmd_session)

    test = subparsers.add_parser(
        "test", help="run session files as tests, with a summary or --json report"
    )
    add_port(test)
    test.add_argument("files", nargs="+", help="session files to run as tests")
    test.add_argument(
        "--artifacts", default="",
        help="directory for failure screenshots (default debug/)",
    )
    test.add_argument("--timeout", type=int, default=300, help="per-file timeout in seconds")
    test.add_argument("--no-build", action="store_true", help="skip goboscript build")
    test.add_argument(
        "--no-reload", action="store_true", help="reuse the running project (no per-file reload)"
    )
    test.add_argument(
        "--no-screenshots", action="store_true", help="do not save a screenshot on failure"
    )
    test.add_argument("--headless", action="store_true", help="no visible browser window")
    add_software(test)
    add_json(test)
    test.set_defaults(func=cmd_test)

    mouse = subparsers.add_parser("mouse", help="move/press the mouse (Scratch coords)")
    add_port(mouse)
    mouse.add_argument("x", type=float)
    mouse.add_argument("y", type=float)
    mouse_group = mouse.add_mutually_exclusive_group()
    mouse_group.add_argument("--down", action="store_true", help="press the mouse button")
    mouse_group.add_argument("--up", action="store_true", help="release the mouse button")
    mouse.set_defaults(func=cmd_mouse)

    click = subparsers.add_parser("click", help="click at Scratch coords")
    add_port(click)
    click.add_argument("x", type=float)
    click.add_argument("y", type=float)
    click.add_argument("--settle", type=int, default=80, help="ms between down and up")
    click.set_defaults(func=cmd_click)

    key = subparsers.add_parser("key", help="press/release a key")
    add_port(key)
    key.add_argument("name", help="space, enter, up/down/left/right, or a character")
    key_group = key.add_mutually_exclusive_group()
    key_group.add_argument("--down", action="store_true", help="hold the key")
    key_group.add_argument("--up", action="store_true", help="release the key")
    key.add_argument("--settle", type=int, default=80, help="ms between down and up")
    key.set_defaults(func=cmd_key)

    pixel = subparsers.add_parser("pixel", help="read the stage pixel colour at Scratch coords")
    add_port(pixel)
    pixel.add_argument("x", type=float)
    pixel.add_argument("y", type=float)
    pixel.set_defaults(func=cmd_pixel)

    frame = subparsers.add_parser("frame", help="print the runtime frame counter")
    add_port(frame)
    frame.set_defaults(func=cmd_frame)

    wait_frame = subparsers.add_parser("wait_frame", help="wait for N runtime frames")
    add_port(wait_frame)
    wait_frame.add_argument("count", type=int)
    wait_frame.add_argument("--timeout", type=int, default=5000, help="timeout in ms")
    wait_frame.set_defaults(func=cmd_wait_frame)

    step = subparsers.add_parser("step", help="pause and advance exactly N frames")
    add_port(step)
    step.add_argument("count", type=int)
    step.set_defaults(func=cmd_step)

    pause = subparsers.add_parser("pause", help="pause the runtime for manual stepping")
    add_port(pause)
    pause.set_defaults(func=cmd_pause)

    resume = subparsers.add_parser("resume", help="resume the runtime after pause/step")
    add_port(resume)
    resume.set_defaults(func=cmd_resume)

    restart = subparsers.add_parser("restart", help="re-run the green-flag scripts")
    add_port(restart)
    restart.set_defaults(func=cmd_restart)

    render = subparsers.add_parser("render", help="frame/render counters and event totals")
    add_port(render)
    render.set_defaults(func=cmd_render)

    record = subparsers.add_parser(
        "record", help="record variables on every frame (event-driven, no polling)"
    )
    add_port(record)
    record.add_argument("selectors", nargs="+")
    record.add_argument("--max", type=int, default=100000, help="stop after N rows")
    record.set_defaults(func=cmd_record)

    trace = subparsers.add_parser("trace", help="dump the recorded per-frame rows")
    add_port(trace)
    trace.add_argument("--clear", action="store_true", help="clear rows after reading")
    trace.set_defaults(func=cmd_trace)

    stop_record = subparsers.add_parser(
        "stop_record", help="stop the frame recorder (keeps the rows)"
    )
    add_port(stop_record)
    stop_record.set_defaults(func=cmd_stop_record)

    until = subparsers.add_parser(
        "until", help="record the exact frame a condition first holds"
    )
    add_port(until)
    until.add_argument("selector")
    until.add_argument("op", choices=["==", "!=", ">", "<", ">=", "<="])
    until.add_argument("value")
    until.add_argument("--pause", action="store_true", help="pause exactly on the hit frame")
    until.add_argument("--max", type=int, default=100000, help="give up after N frames")
    until.set_defaults(func=cmd_until)

    broadcast = subparsers.add_parser(
        "broadcast", help="start the 'when I receive' hats for a message"
    )
    add_port(broadcast)
    broadcast.add_argument("name", help="broadcast message name")
    broadcast.set_defaults(func=cmd_broadcast)

    broadcast_wait = subparsers.add_parser(
        "broadcast_wait", help="broadcast a message, then wait for its handlers"
    )
    add_port(broadcast_wait)
    broadcast_wait.add_argument("name", help="broadcast message name")
    broadcast_wait.add_argument("--timeout", type=int, default=5000, help="timeout in ms")
    broadcast_wait.set_defaults(func=cmd_broadcast_wait)

    inspect = subparsers.add_parser(
        "inspect", help="dump targets, variables, costumes, and extensions"
    )
    add_port(inspect)
    inspect.add_argument(
        "target", nargs="?", default="",
        help="sprite name or name#N (all targets when omitted)",
    )
    inspect.set_defaults(func=cmd_inspect)

    props = subparsers.add_parser("props", help="print a target's properties")
    add_port(props)
    props.add_argument("target", help="sprite name or name#N")
    props.set_defaults(func=cmd_props)

    prop = subparsers.add_parser("prop", help="read or write one target property")
    add_port(prop)
    prop.add_argument("target", help="sprite name or name#N")
    prop.add_argument("name", help="x, y, direction, size, visible, costume, ...")
    prop.add_argument("value", nargs="?", default=None, help="JSON value; omit to read")
    prop.set_defaults(func=cmd_prop)

    clones = subparsers.add_parser("clones", help="print the clone count per sprite")
    add_port(clones)
    clones.set_defaults(func=cmd_clones)

    errors = subparsers.add_parser("errors", help="dump captured VM/page errors")
    add_port(errors)
    errors.set_defaults(func=cmd_errors)

    expect_no_errors = subparsers.add_parser(
        "expect_no_errors", help="assert no VM/page errors and no error-level logs"
    )
    add_port(expect_no_errors)
    expect_no_errors.set_defaults(func=cmd_expect_no_errors)

    wait_until = subparsers.add_parser(
        "wait_until", help="wait (event-driven) until a variable satisfies a condition"
    )
    add_port(wait_until)
    wait_until.add_argument("selector")
    wait_until.add_argument("op", choices=["==", "!=", ">", "<", ">=", "<="])
    wait_until.add_argument("value")
    wait_until.add_argument("--timeout", type=int, default=5000, help="timeout in ms")
    wait_until.set_defaults(func=cmd_wait_until)

    wait_pixel = subparsers.add_parser(
        "wait_pixel", help="wait (event-driven) until a stage pixel is a colour"
    )
    add_port(wait_pixel)
    wait_pixel.add_argument("x", type=float)
    wait_pixel.add_argument("y", type=float)
    wait_pixel.add_argument("hex", help="colour to wait for, e.g. #ff0000")
    wait_pixel.add_argument("--timeout", type=int, default=5000, help="timeout in ms")
    wait_pixel.set_defaults(func=cmd_wait_pixel)

    close = subparsers.add_parser("close", help="close the host browser and server")
    add_port(close)
    close.set_defaults(func=cmd_close)

    subparsers.choices["build"].set_defaults(func=cmd_build)
    subparsers.choices["status"].set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    global JSON_MODE
    configure_stdio()
    args = build_parser().parse_args(argv)
    if getattr(args, "json", False):
        JSON_MODE = True
    if args.command != "build" and hasattr(args, "port"):
        args.port = resolve_port(args.port)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
