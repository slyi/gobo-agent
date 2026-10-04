#!/usr/bin/env python3
"""gobo-agent: GoboScript dev loop on the real Scratch runtime.

Runs goboscript builds against upstream @scratch/scratch-vm in a plain headless
browser (tools/scratchhost) — no desktop app, no Electron. The real GPU is used
by default, so performance numbers are
representative; --software forces SwiftShader on boxes without a GPU.

Requires (run `gsdev.py doctor` to check them):
    python 3.10+            the only Python runtime; no pip packages
    goboscript              the compiler; on PATH, else bundled by `setup`
    host bundles            @scratch/* browser builds; bundled by `setup` into
                            tools/scratchhost/vendor, else node+npm (`npm install`)
    Chrome or Edge          the host browser (GSDEV_BROWSER overrides)

`gsdev.py setup` downloads the prebuilt tools into user-writable paths, so a
clean Windows machine with no admin rights needs no Rust, MSVC, MSYS2, or Node.

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
    perf        Print fps / rendertime / steptime snapshot
    errors      Dump captured VM/page errors and error-log count
    expect_no_errors  Assert there are no VM/page or error-log errors
    wait_until  Wait (event-driven) until a variable satisfies a condition
    wait_pixel  Wait (event-driven) until a stage pixel is a colour
    session     Batch get/set/watch lines from stdin over one connection
    test        Run session files as tests, with a summary or --json report
    doctor      Check that the required tools are installed
    setup       Download prebuilt goboscript + host bundles (no admin/pip)
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
--port 0 (GSDEV_CDP_PORT=0) to auto-pick a free CDP port recorded in the state
root's .gsdev-port for parallel A/B runs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import (  # noqa: E402
    CDP,
    CDPError,
    connect,
    list_targets,
)
import bootstrap  # noqa: E402

TOOLS_DIR = Path(__file__).resolve().parent
HARNESS_ROOT = TOOLS_DIR.parent

# --- Project config (gobo-agent.json) -----------------------------------------
#
# gobo-agent can be vendored into another project (e.g. gobo-agent/runtime/)
# and configured with a ``gobo-agent.json``. Resolution contract:
#
#   * Precedence per value: ``GSDEV_*`` env -> config field -> built-in default.
#
#   * Config *path* values resolve relative to the directory containing the
#     config file; env path values resolve relative to the current working
#     directory. The exception is ``modeInclude``, which is project-root
#     relative because it mirrors a ``%include`` line in project source.
#
#   * Discovery order: ``GSDEV_CONFIG``; else ``<GSDEV_PROJECT>/gobo-agent.json``
#     when ``GSDEV_PROJECT`` is set; else ``<harness>/../gobo-agent.json`` (the
#     vendored folder/submodule layout).
#
#   * With no config file the historical harness-relative defaults apply, so a
#     plain gobo-agent checkout (this repo) is unchanged.
CONFIG_SCHEMA = 1


def _load_config() -> tuple[dict, Path | None]:
    override = os.environ.get("GSDEV_CONFIG")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    else:
        project_env = os.environ.get("GSDEV_PROJECT")
        if project_env:
            candidates.append(Path(project_env).expanduser() / "gobo-agent.json")
        candidates.append(HARNESS_ROOT.parent / "gobo-agent.json")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SystemExit(f"invalid gobo-agent config {candidate}: {error}")
        if not isinstance(data, dict):
            raise SystemExit(
                f"invalid gobo-agent config {candidate}: top level must be an object"
            )
        return data, candidate.resolve().parent
    if override:
        raise SystemExit(f"GSDEV_CONFIG={override} is not a file")
    return {}, None


CONFIG, CONFIG_DIR = _load_config()


def _config_path(key: str, base: Path) -> Path | None:
    value = CONFIG.get(key)
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path.cwd() / path).resolve()


def _resolve_project_root() -> Path:
    env = _env_path("GSDEV_PROJECT")
    if env is not None:
        return env
    configured = _config_path("project", CONFIG_DIR or Path.cwd())
    if configured is not None:
        return configured
    return TOOLS_DIR.parent


PROJECT_ROOT = _resolve_project_root()


def _resolve_state_root() -> Path:
    """Where per-project run state lives (port file, browser profiles).

    Defaults to ``<config dir>/state`` when a config is present (the vendored
    layout's ``gobo-agent/state/``), else the historical harness-local ``tools/``
    so an unconfigured checkout is unchanged.
    """
    env = _env_path("GSDEV_STATE")
    if env is not None:
        return env
    configured = _config_path("state", CONFIG_DIR or PROJECT_ROOT)
    if configured is not None:
        return configured
    if CONFIG_DIR is not None:
        return (CONFIG_DIR / "state").resolve()
    return TOOLS_DIR


STATE_ROOT = _resolve_state_root()


def mode_include_path(root: Path) -> Path:
    """Path to the debug/release include rewritten for a build.

    ``GSDEV_MODE_INCLUDE`` then config ``modeInclude`` override the default
    ``tools/gsdev_mode.gs``; both are relative to the project root (the
    ``%include`` path), not the config directory.
    """
    spec = os.environ.get("GSDEV_MODE_INCLUDE") or CONFIG.get("modeInclude") or "tools/gsdev_mode.gs"
    path = Path(str(spec))
    return (path if path.is_absolute() else root / path).resolve()


def _int_setting(env_name: str, key: str, default: int) -> int:
    """env -> config -> default, preserving an explicit 0 (auto port)."""
    raw = os.environ.get(env_name)
    if raw is not None and raw != "":
        try:
            return int(raw)
        except ValueError:
            raise SystemExit(f"{env_name}={raw!r} is not an integer")
    value = CONFIG.get(key)
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise SystemExit(f"config {key}={value!r} is not an integer")
    return default


def _prebuild_argv() -> list[str] | None:
    """The configured pre-build command as an argv list, or None.

    ``GSDEV_PREBUILD`` (shell-split) then config ``prebuild`` (a string or a
    list of strings).
    """
    raw = os.environ.get("GSDEV_PREBUILD")
    if raw is None:
        raw = CONFIG.get("prebuild")
    if not raw:
        return None
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    if isinstance(raw, str):
        import shlex
        return shlex.split(raw)
    raise SystemExit("config prebuild must be a string or a list of strings")


def _config_env() -> dict:
    env = CONFIG.get("env")
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise SystemExit("config env must be an object of string values")
    return {str(key): str(value) for key, value in env.items()}


def run_prebuild(root: Path, mode: str = "debug") -> None:
    """Run the project's pre-build command, if configured.

    Runs with cwd = the project root, the resolved interpreter's directory
    prepended to PATH (so a bare ``python``/``python3`` finds the same Python
    that runs gsdev), and ``{python}``/``{project}``/``{mode}`` tokens
    substituted. A non-zero exit aborts the build.
    """
    argv = _prebuild_argv()
    if not argv:
        return
    project = str(root)
    tokens = {"{python}": sys.executable, "{project}": project, "{mode}": mode}
    command = []
    for arg in argv:
        for token, value in tokens.items():
            arg = arg.replace(token, value)
        command.append(arg)
    env = dict(os.environ)
    env.update(_config_env())
    interpreter_dir = str(Path(sys.executable).parent)
    env["PATH"] = interpreter_dir + os.pathsep + env.get("PATH", "")
    env["GSDEV_PROJECT"] = project
    env["GSDEV_MODE"] = mode
    log("prebuild: " + " ".join(command))
    try:
        result = subprocess.run(command, cwd=project, env=env)
    except OSError as error:
        raise SystemExit(f"prebuild failed to start: {error}")
    if result.returncode != 0:
        raise SystemExit(f"prebuild failed (exit {result.returncode}): {' '.join(command)}")


_INCLUDE_RE = re.compile(r"^\s*%include\s+(\S+)")


def _missing_includes(root: Path) -> list[str]:
    """Project ``%include`` paths that do not resolve.

    Scans the project's own source set (top-level ``*.gs`` plus ``lib/``), so
    ``doctor`` can flag a tree that is not build-ready (typically because
    generated code has not been produced) without reading nested fixtures or
    build copies.
    """
    missing: list[str] = []
    # Only the real source set: top-level sprites/stage plus the include dir.
    # Avoids nested fixtures/build copies (examples/, debug/, .gsdev/) whose
    # relative includes would otherwise read as unresolved.
    sources = sorted(root.glob("*.gs"))
    lib = root / "lib"
    if lib.is_dir():
        sources += sorted(lib.rglob("*.gs"))
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _INCLUDE_RE.match(line)
            if not match:
                continue
            target = (root / match.group(1)).resolve()
            candidates = [target, target.with_suffix(".gs")] if target.suffix != ".gs" else [target]
            if not any(candidate.exists() for candidate in candidates):
                try:
                    shown = path.relative_to(root)
                except ValueError:
                    shown = path
                missing.append(f"{shown}: {match.group(1)}")
    return missing


def _default_ports() -> tuple[int, int]:
    """Default CDP/host-server ports.

    A plain gobo-agent checkout keeps 9230/8077 (docs, CI, existing hosts). An
    adopted project (a config is present) gets a stable project-scoped pair so
    two adopted projects do not collide by default.
    """
    if CONFIG_DIR is None:
        return 9230, 8077
    digest = hashlib.sha1(str(PROJECT_ROOT).encode("utf-8")).digest()
    cdp = 9300 + int.from_bytes(digest[:2], "big") % 300
    host = 8100 + int.from_bytes(digest[2:4], "big") % 300
    return cdp, host


_DEFAULT_CDP_PORT, _DEFAULT_HOST_PORT = _default_ports()
SB3_PATH = PROJECT_ROOT / (PROJECT_ROOT.name + ".sb3")
DEBUG_DIR = PROJECT_ROOT / "debug"
DEFAULT_PORT = _int_setting("GSDEV_CDP_PORT", "cdpPort", _DEFAULT_CDP_PORT)
PORT_FILE = STATE_ROOT / ".gsdev-port"
HOST_DIR = TOOLS_DIR / "scratchhost"
HOST_PROFILE_ROOT = STATE_ROOT / "scratchvm-profiles"


def _harness_fingerprint() -> str:
    """Short hash of the served host page + injected scripts.

    Identifies the harness code a warm host/server is running, so a host or
    server from a different build is never silently reused across an update.
    """
    digest = hashlib.sha1()
    for name in ("host.html", "host.js", "profiler.js"):
        try:
            digest.update(name.encode("utf-8"))
            digest.update((HOST_DIR / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:12]


HARNESS_FINGERPRINT = _harness_fingerprint()


def host_profile_dir(port: int) -> Path:
    """Browser profile directory for a CDP port.

    One profile per port, so two hosts (A/B) can run at once: a shared
    ``--user-data-dir`` makes the second Chromium launch hand off to the first
    and exit, and then no second debug port ever appears.
    """
    return HOST_PROFILE_ROOT / str(port)
HOST_SERVER_PORT = _int_setting("GSDEV_HOST_PORT", "hostPort", _DEFAULT_HOST_PORT)
# Shared across projects; the host server mounts it at /vendor/ (see hostserver.py).
BUNDLES_DIR = bootstrap.bundles_dir()

# Phase 1: how long a freshly launched browser has to expose a debuggable page.
# Deliberately short (5s) so a blocked/broken browser fails fast instead of
# hanging; raise GSDEV_HOST_TIMEOUT for a slow cold start.
HOST_ATTACH_TIMEOUT = float(os.environ.get("GSDEV_HOST_TIMEOUT", "5"))
# Phase 2: once the page exists, how long it may take to expose window.__host
# (parsing the ~5.8 MB VM on a cold start). Raise GSDEV_READY_TIMEOUT if needed.
HOST_READY_TIMEOUT = float(os.environ.get("GSDEV_READY_TIMEOUT", "20"))
# Per-attempt CDP timeout inside those windows, so one unresponsive target cannot
# consume the whole budget.
CDP_ATTACH_TIMEOUT = float(os.environ.get("GSDEV_CDP_TIMEOUT", "2"))


def resolve_goboscript() -> str | None:
    """The compiler: the user's own install (PATH) first, then the bundled one.

    A goboscript the user already has wins; `.tools/goboscript/` is only the
    fallback that `setup` installs when none is found.
    """
    return bootstrap.resolve_goboscript()


def host_bundles_present() -> bool:
    """True when host.html has a loadable set of @scratch/* bundles."""
    return bootstrap.host_bundles_present()


def _setup_hint() -> str:
    """A setup command that works even where no `python` is on PATH.

    On Windows the PowerShell script bootstraps Python itself, so it is the only
    correct hint for a machine without an interpreter (Store App Execution Alias
    aside, `python tools/gsdev.py setup` cannot run there).
    """
    setup_script = TOOLS_DIR.parent / "setup.ps1"
    if os.name == "nt" and setup_script.exists():
        return (
            'run: powershell -NoProfile -ExecutionPolicy Bypass -File '
            f'"{setup_script}"'
        )
    return "run: python tools/gsdev.py setup"


def ensure_dependencies(need_bundles: bool = True) -> None:
    """Auto-install the prebuilt tools when a command needs something missing.

    `setup.ps1`/`gsdev.ps1` obtain Python; this obtains goboscript and the host
    bundles, so a fresh extract can go straight to `run`. Set
    ``GSDEV_NO_AUTO_SETUP=1`` to only surface the error/hint instead of
    downloading.
    """
    missing_tools = resolve_goboscript() is None
    missing_bundles = need_bundles and not host_bundles_present()
    # sb2gs needs Python 3.14+; on an older interpreter it can never be present, so
    # do not let that keep triggering setup. A user-installed sb2gs counts too.
    missing_sb2gs = (
        sys.version_info[:2] >= (3, 14)
        and bootstrap.system_sb2gs() is None
        and not bootstrap.sb2gs_present()
    )
    if not (missing_tools or missing_bundles or missing_sb2gs):
        return
    if os.environ.get("GSDEV_NO_AUTO_SETUP", "").strip().lower() in ("1", "true", "yes"):
        return  # the caller's error path explains what is missing
    log("dependencies missing; downloading the prebuilt tools (no admin)")
    only = None
    if missing_tools and not missing_bundles and not missing_sb2gs:
        only = "goboscript"
    elif missing_sb2gs and not missing_tools and not missing_bundles:
        only = "sb2gs"
    bootstrap.run_setup(only=only)



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
    the state root's .gsdev-port so later commands (status/stop/close) find the
    same editor. The file is per state root, so parallel runs in separate
    projects/worktrees stay independent.
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
            encoding="utf-8",
            errors="replace",
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
                [exe, *extra], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=20
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
                # Only processes that really are the browser: the debug-port owner
                # may be a foreign process (e.g. a gsdev host server) if the caller
                # passed the host-server port instead of the CDP port, and we must
                # never kill or mislabel that as ours.
                if names.get(pid) == wanted:
                    pids.add(pid)
        pids.discard(os.getpid())
        return sorted(pids)
    needle = str(profile_dir)
    pids: set[int] = set()
    result = subprocess.run(
        ["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True,
        encoding="utf-8", errors="replace"
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
    # Edge is preinstalled on Windows (Chrome usually is not), so prefer it there
    # to keep the zero-admin path dependency-free. Both are Chromium, so CDP and
    # --headless=new behave the same; Chrome remains the fallback.
    "win32": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
}


def _windows_app_path_browser() -> str | None:
    """Edge/Chrome from the App Paths registry (robust to install dir/arch)."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for exe in ("msedge.exe", "chrome.exe"):
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, base + "\\" + exe) as key:
                    value, _ = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            if value and Path(value).exists():
                return value
    return None


def find_browser() -> str | None:
    override = os.environ.get("GSDEV_BROWSER") or os.environ.get("CHROME_EXE")
    if override and Path(override).exists():
        return override
    for candidate in BROWSER_CANDIDATES.get(sys.platform, []):
        if Path(candidate).exists():
            return candidate
    registered = _windows_app_path_browser()
    if registered:
        return registered
    names = (
        ("msedge", "microsoft-edge", "google-chrome", "chrome", "chromium", "chromium-browser")
        if sys.platform == "win32"
        else ("google-chrome", "chrome", "chromium", "chromium-browser", "microsoft-edge")
    )
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def host_url() -> str:
    # Identity travels in the URL so the page can expose which project + harness
    # build it is; ensure_host reads it back before attaching (never reuse a host
    # belonging to another project or a different build).
    query = urllib.parse.urlencode({
        "project": str(PROJECT_ROOT),
        "fp": HARNESS_FINGERPRINT,
    })
    return f"http://127.0.0.1:{HOST_SERVER_PORT}/host.html?{query}"


def _detached_kwargs() -> dict:
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        # Opt-in: launch the browser at HIGH_PRIORITY_CLASS. Windows can otherwise
        # schedule a background-launched Chromium at reduced priority/frequency
        # (Efficiency Mode), which skews perf runs.
        if os.environ.get("GSDEV_HIGH_PRIORITY"):
            flags |= subprocess.HIGH_PRIORITY_CLASS
        return {"creationflags": flags}
    return {"start_new_session": True}


# Temp-dir namespace for cross-process host state. `kilo` is the historical
# location; read/clear still check it so a host started by an older build is
# found, but all new state is written under the tool's own name.
_HOST_STATE_APPS = ("gobo-agent", "kilo")


def _host_state_path(app: str) -> Path:
    return Path(os.environ.get("TEMP", tempfile.gettempdir())) / app / (
        f"gsdev_host_{HOST_SERVER_PORT}.json"
    )


def _host_state_write_path() -> Path:
    path = _host_state_path(_HOST_STATE_APPS[0])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def read_host_state() -> dict:
    for app in _HOST_STATE_APPS:
        try:
            return json.loads(_host_state_path(app).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def write_host_state(**fields) -> None:
    try:
        _host_state_write_path().write_text(json.dumps(fields), encoding="utf-8")
    except OSError:
        pass


def update_host_state(**fields) -> None:
    """Merge fields into the host state, preserving the server token/pid.

    ``write_host_state`` overwrites the whole file, so the browser's CDP port is
    added here rather than with a second full write that would drop the server's
    ownership token.
    """
    state = read_host_state()
    state.update(fields)
    write_host_state(**state)


def clear_host_state() -> None:
    for app in _HOST_STATE_APPS:
        try:
            _host_state_path(app).unlink()
        except OSError:
            pass


def probe_host_server(token: str | None = None, timeout: float = 1.0) -> dict | None:
    """Return {token, pid, dir} if a gsdev host server holds the port, else None.

    `token` filters to a specific server; None accepts any gsdev host server.
    A foreign listener (no /__gsdev) returns None so callers never treat it as ours.
    """
    url = f"http://127.0.0.1:{HOST_SERVER_PORT}/__gsdev"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            info = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if not isinstance(info, dict) or "token" not in info:
        return None
    if token is not None and info.get("token") != token:
        return None
    return info


def ensure_host_server() -> None:
    state = read_host_state()
    if port_open(HOST_SERVER_PORT):
        info = probe_host_server()
        if info is not None:
            if info.get("fingerprint") != HARNESS_FINGERPRINT:
                raise SystemExit(
                    f"host server on port {HOST_SERVER_PORT} was started by a different "
                    f"gobo-agent build (fingerprint {info.get('fingerprint')!r}, expected "
                    f"{HARNESS_FINGERPRINT}); set GSDEV_HOST_PORT to a free port or stop it."
                )
            # A gsdev host server already holds the port (ours, or another
            # checkout's running the same harness build). Reuse it; we only *close*
            # a server we recorded.
            if not (state.get("token") and info.get("token") == state["token"]):
                log(f"host server already on http://127.0.0.1:{HOST_SERVER_PORT} "
                    f"(pid {info.get('pid')}); reusing")
            return
        raise SystemExit(
            f"port {HOST_SERVER_PORT} is in use by another application (not a gsdev "
            f"host server). Set GSDEV_HOST_PORT to a free port, or stop that process."
        )
    if not (HOST_DIR / "host.html").exists():
        raise SystemExit(f"missing {HOST_DIR / 'host.html'}")
    if not host_bundles_present():
        raise SystemExit(
            "host bundles are not installed; "
            f"{_setup_hint()}\n"
            f'  (or the advanced npm path: cd "{HOST_DIR}" and run npm install)'
        )
    # hostserver.py (not `python -m http.server`) so responses are no-store: the
    # stdlib server allows conditional 304s, which served an edited host.js stale.
    # It is started with a per-session ownership token so a foreign process on the
    # port is never mistaken for it (and never killed).
    token = uuid.uuid4().hex
    args = [
        sys.executable, str(TOOLS_DIR / "hostserver.py"),
        str(HOST_DIR), str(HOST_SERVER_PORT), token, HARNESS_FINGERPRINT,
        str(BUNDLES_DIR),
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached_kwargs())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        info = probe_host_server(token)
        if info is not None:
            write_host_state(token=token, pid=info.get("pid"), port=HOST_SERVER_PORT,
                             project=str(PROJECT_ROOT), fingerprint=HARNESS_FINGERPRINT)
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


def prepare_host_profile(profile_dir: Path) -> None:
    """Mark the reused host profile as cleanly exited.

    We kill the host browser instead of closing it, so Chromium records an
    unclean exit and the *next visible* launch shows a "restore pages?" bubble
    over the stage (which also steals input). Edge ignores
    ``--hide-crash-restore-bubble``, so rewrite the exit flag before launching.
    """
    prefs = profile_dir / "Default" / "Preferences"
    if not prefs.exists():
        return
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    profile = data.get("profile")
    if not isinstance(profile, dict):
        return
    if profile.get("exit_type") == "Normal" and profile.get("crashed") is False:
        return
    profile["exit_type"] = "Normal"
    profile["crashed"] = False
    try:
        prefs.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def is_browser_dialog(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme in ("edge", "chrome") and parsed.hostname in {
        "sync-confirmation-dialog", "sync-confirmation",
        "session-restore", "session-restore-bubble",
    }


def dismiss_browser_dialogs(port: int) -> None:
    """Close Edge/Chrome internal pages that cover the stage.

    On managed Windows, Edge force-signs in and shows
    ``edge://sync-confirmation-dialog/`` ("We are now syncing your browsing
    data…") or a restore bubble over the host page, ignoring ``--disable-sync``
    and the feature flags. Closing the DevTools target dismisses it.
    """
    try:
        targets = list_targets(port=port, timeout=CDP_ATTACH_TIMEOUT)
    except (CDPError, OSError, urllib.error.URLError):
        return
    for target in targets:
        url = str(target.get("url", ""))
        # Internal targets also include Chrome's omnibox UI, which is recreated
        # when closed. Only dismiss known blocking dialogs.
        if not is_browser_dialog(url):
            continue
        target_id = target.get("id")
        if not target_id:
            continue
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/close/{target_id}"
        )
        try:
            urllib.request.urlopen(request, timeout=CDP_ATTACH_TIMEOUT).close()
        except (OSError, urllib.error.URLError):
            continue
        log(f"closed browser dialog: {url}")


def abort_host(port: int) -> None:
    """Tear down a failed launch so nothing detached is left behind.

    A failed `run` used to leave the host server (started with the portable
    Python) alive; that then held `.tools/python`, so a rebuild could not delete
    it until the interpreter was killed by hand.
    """
    try:
        close_host(port)
    except (CDPError, OSError, urllib.error.URLError):
        pass


def browser_flags(browser: str, port: int, profile) -> list[str]:
    """Chromium flags shared by gsdev and the bridge.

    Both launchers must use the same settings or their runs are not comparable
    (perf numbers, dialog suppression, GPU/occlusion behavior). Callers append
    their own extras and the target URL.
    """
    return [
        browser, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows", "--disable-background-mode",
        # The host profile is reused and often killed rather than closed cleanly,
        # so suppress Chrome's "did not shut down correctly / restore pages" bubble
        # and other dialogs that would sit over the stage and steal input. Edge
        # ignores the bubble flags, which prepare_host_profile() covers instead.
        "--noerrdialogs", "--disable-infobars",
        "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
        # Edge (unlike Chrome) opens first-run/sync/restore dialogs as extra
        # pages that sit over the stage and can stall the host page; disable them.
        "--disable-sync", "--no-service-autorun", "--disable-component-update",
        "--disable-features=InfiniteSessionRestore,SessionRestoreBubble,"
        "msEdgeSyncConfirmation,msEdgeFirstRunExperience,msEdgeIdentityIntegration,"
        # Windows native window-occlusion detection throttles a headed window that
        # is not foreground; keep the host stepping at full rate either way.
        "CalculateNativeWinOcclusion,",
        # Hardware GL is the production path (scratch.mit.edu runs on the GPU);
        # don't silently fall back to a software/blocklisted renderer.
        "--ignore-gpu-blocklist",
        # Disable Chromium's field-trial testing config where supported. This
        # does not disable all Finch experiments in branded Chrome/Edge.
        "--disable-field-trial-config",
    ]


def _assert_host_identity(conn: CDP, port: int) -> None:
    """Refuse to use a host page belonging to another project or build.

    ``host_url()`` carries the project root and harness fingerprint as query
    params; host.html exposes them as ``window.__gsdevIdentity``. Missing
    identity means a host page from a different build (or a foreign page), which
    is never adopted silently.
    """
    identity = conn.evaluate(
        "(() => { const i = window.__gsdevIdentity ||"
        " (window.__host && window.__host.identity);"
        " return i ? {project: String(i.project || ''), fp: String(i.fingerprint || '')}"
        " : null; })()",
        timeout=CDP_ATTACH_TIMEOUT,
    )
    if not isinstance(identity, dict) or not identity.get("project"):
        raise SystemExit(
            f"host on port {port} has no project identity; it may be from a different "
            f"gobo-agent build. Run `close --port {port}` and start it again."
        )
    host_project = Path(identity["project"]).resolve()
    if host_project != PROJECT_ROOT:
        raise SystemExit(
            f"host on port {port} belongs to project {host_project}, not {PROJECT_ROOT}; "
            f"use another --port or `close --port {port}`."
        )
    host_fp = identity.get("fp")
    if host_fp and host_fp != HARNESS_FINGERPRINT:
        raise SystemExit(
            f"host on port {port} runs a different gobo-agent build (fingerprint "
            f"{host_fp}, expected {HARNESS_FINGERPRINT}); run `close --port {port}` and "
            "start it again."
        )


def ensure_host(port: int, headless: bool, software: bool) -> tuple[CDP, bool]:
    # GSDEV_SOFTWARE=1 forces software GL without threading --software through
    # every command (CI sets it once so the test suites can run on GPU-less boxes).
    software = software or os.environ.get("GSDEV_SOFTWARE", "").strip().lower() not in (
        "", "0", "false",
    )
    target = host_target(port) if port_open(port) else None
    if target is not None:
        conn = connect(target, timeout=CDP_ATTACH_TIMEOUT)
        try:
            version = conn.call("Browser.getVersion", timeout=CDP_ATTACH_TIMEOUT)
            actual_headless = "HeadlessChrome/" in version.get("userAgent", "")
            if actual_headless != headless:
                actual = "headless" if actual_headless else "headed"
                wanted = "headless" if headless else "headed"
                raise SystemExit(
                    f"host on port {port} is {actual}, but this command requests {wanted}; "
                    f"run `close --port {port}` first, or choose another --port"
                )
            _assert_host_identity(conn, port)
            dismiss_browser_dialogs(port)
            return conn, False
        except BaseException:
            conn.close()
            raise
    browser = find_browser()
    if browser is None:
        raise SystemExit("no Chrome/Edge found; set GSDEV_BROWSER to the executable")
    ensure_host_server()
    profile = host_profile_dir(port)
    profile.mkdir(parents=True, exist_ok=True)
    prepare_host_profile(profile)
    flags = browser_flags(browser, port, profile)
    if headless:
        flags.append("--headless=new")
        if software:
            flags += ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    flags.append(host_url())
    log(f"launching {Path(browser).name}{' headless' if headless else ''}")
    subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached_kwargs())
    # Record the CDP port so a later `close` (with no --port) can find this
    # browser instead of guessing the default.
    update_host_state(cdpPort=port)
    # Two phases so a browser that never attaches fails fast, while a slow cold
    # page load still gets time: HOST_ATTACH_TIMEOUT for a debuggable page to
    # appear, then HOST_READY_TIMEOUT for window.__host to come up.
    attach_deadline = time.monotonic() + HOST_ATTACH_TIMEOUT
    ready_deadline: float | None = None
    conn: CDP | None = None
    while True:
        now = time.monotonic()
        if ready_deadline is None:
            if now >= attach_deadline:
                abort_host(port)
                raise SystemExit(
                    f"no debuggable {Path(browser).name} page appeared within "
                    f"{HOST_ATTACH_TIMEOUT:g}s; is the browser blocked by policy? "
                    "(raise GSDEV_HOST_TIMEOUT for a slow cold start)"
                )
        elif now >= ready_deadline:
            break
        dismiss_browser_dialogs(port)
        target = host_target(port)
        if target is not None:
            if ready_deadline is None:
                ready_deadline = now + HOST_READY_TIMEOUT
            try:
                if conn is None:
                    conn = connect(target, timeout=CDP_ATTACH_TIMEOUT)
                if conn.evaluate(
                    "!!(window.__host && window.__marks && window.__marks.ready)",
                    timeout=CDP_ATTACH_TIMEOUT,
                ):
                    log("host ready")
                    try:
                        info = conn.evaluate(
                            "window.__host.gpu ? window.__host.gpu() : null",
                            timeout=CDP_ATTACH_TIMEOUT,
                        )
                        if info and info.get("renderer"):
                            log(f"gl {info['renderer']}")
                            if info.get("software"):
                                log(
                                    "WARNING: GL looks like software (SwiftShader); "
                                    "perf is not GPU-representative"
                                )
                    except (CDPError, TimeoutError, OSError):
                        pass
                    return conn, True
            except (CDPError, TimeoutError, OSError):
                pass
        time.sleep(0.2)
    detail = ""
    try:
        if conn is not None:
            error = conn.evaluate("window.__hostBundlesError || ''", timeout=CDP_ATTACH_TIMEOUT)
            if error:
                detail = f" (host page: {error})"
    except (CDPError, TimeoutError, OSError):
        pass
    abort_host(port)
    raise SystemExit(
        f"scratch-vm host page did not finish loading within {HOST_READY_TIMEOUT:g}s"
        f"{detail}; {_setup_hint()}"
    )


def open_host(port: int) -> CDP | None:
    if not port_open(port):
        log(f"no scratch-vm host on port {port}; start the project with run first")
        return None
    target = host_target(port)
    if target is None:
        log(f"no scratch-vm host on port {port}; start the project with run first")
        return None
    conn = connect(target, timeout=CDP_ATTACH_TIMEOUT)
    try:
        _assert_host_identity(conn, port)
    except BaseException:
        conn.close()
        raise
    return conn


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
    pids = find_editor_pids(port, process_name, host_profile_dir(port))
    if pids:
        terminate_pids(pids)
        log(f"killed host browser (pid {', '.join(str(p) for p in pids)})")
    else:
        log("no host browser to close")
    # Only stop the host server if we started it (recorded token) and it is still
    # the same process -- never a foreign application that happens to hold the port.
    state = read_host_state()
    token = state.get("token")
    if not token:
        return
    if not port_open(HOST_SERVER_PORT):
        clear_host_state()
        return
    info = probe_host_server(token)
    if info is None:
        log("host server on this port is not ours; leaving it running")
        return
    server_pids = sorted({p for p in (info.get("pid"), state.get("pid")) if isinstance(p, int)})
    if server_pids:
        terminate_pids(server_pids)
        log(f"stopped host server (pid {', '.join(str(p) for p in server_pids)})")
    clear_host_state()


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


# --- debug / release builds ---------------------------------------------------
# A harness-generated include (`lib/gsdev_mode.gs`) provides diagnostic statement
# macros. Debug expands them to real statements; release erases the statement *and
# its arguments* at compile time (verified empty macro expansion). A project opts in
# with `%include lib/gsdev_mode` and calls the macros as statements without a
# trailing `;` (the macro owns the terminator).
MODE_MACROS = {
    "debug": (
        "%define DBG_LOG(msg) log msg;\n"
        "%define DBG_SAY(msg) say msg;\n"
        "%define DBG_ADD(target, n) target += n;\n"
        "%define DBG_SET(target, v) target = v;\n"
    ),
    "release": (
        "%define DBG_LOG(msg)\n"
        "%define DBG_SAY(msg)\n"
        "%define DBG_ADD(target, n)\n"
        "%define DBG_SET(target, v)\n"
    ),
}
def resolve_mode(args=None) -> str:
    mode = getattr(args, "mode", None) or os.environ.get("GSDEV_MODE") or "debug"
    return mode if mode in MODE_MACROS else "debug"


def inject_monitors(sb3_path: Path, root: Path) -> None:
    """Re-inject the source project's monitors into a built sb3.

    goboscript emits an empty `monitors` array, so monitor positions, slider
    modes and visibility are lost. sb2gs records them in `monitors.json` (with the
    compiled variable/list name); this maps each back to the compiled id and
    writes the array into the sb3. Best-effort: a missing/invalid sidecar is a
    no-op.
    """
    sidecar = root / "monitors.json"
    if not sidecar.is_file():
        return
    try:
        records = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(records, list) or not records:
        return
    try:
        with zipfile.ZipFile(sb3_path) as archive:
            project = json.loads(archive.read("project.json"))
            members = [
                (name, archive.read(name))
                for name in archive.namelist()
                if name != "project.json"
            ]
    except (OSError, ValueError, zipfile.BadZipFile):
        return
    targets: dict[str | None, dict] = {
        None: next((t for t in project["targets"] if t.get("isStage")), None)
    }
    for target in project["targets"]:
        if not target.get("isStage"):
            targets[target["name"]] = target
    monitors = []
    for record in records:
        target = targets.get(record.get("target"))
        if target is None:
            continue
        is_list = record.get("kind") == "list"
        name = record.get("name")
        table = target.get("lists" if is_list else "variables", {})
        var_id = next((vid for vid, entry in table.items() if entry[0] == name), None)
        if var_id is None:
            continue
        monitors.append({
            "id": var_id,
            "mode": record.get("mode", "default"),
            "opcode": "data_listcontents" if is_list else "data_variable",
            "params": {"LIST" if is_list else "VARIABLE": name},
            "spriteName": record.get("target"),
            "value": 0,
            "width": 0,
            "height": 0,
            "x": record.get("x", 0),
            "y": record.get("y", 0),
            "visible": bool(record.get("visible", False)),
            "sliderMin": record.get("sliderMin", 0),
            "sliderMax": record.get("sliderMax", 100),
            "isDiscrete": record.get("isDiscrete", True),
        })
    if not monitors:
        return
    project["monitors"] = monitors
    tmp = sb3_path.with_name(sb3_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("project.json", json.dumps(project, ensure_ascii=False))
            for name, data in members:
                archive.writestr(name, data)
        os.replace(tmp, sb3_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def inject_costumes(sb3_path: Path, root: Path) -> None:
    """Set the source rotation centers on a built sb3's costumes.

    sb2gs preserves costumes byte-for-byte, so the compiled costumes have no
    `rotationCenterX`/`rotationCenterY`/`bitmapResolution`; this restores them from
    the `costumes.json` sidecar (matched by target + costume name, else order).
    Best-effort.
    """
    sidecar = root / "costumes.json"
    if not sidecar.is_file():
        return
    try:
        records = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(records, list) or not records:
        return
    try:
        with zipfile.ZipFile(sb3_path) as archive:
            project = json.loads(archive.read("project.json"))
            members = [
                (name, archive.read(name))
                for name in archive.namelist()
                if name != "project.json"
            ]
    except (OSError, ValueError, zipfile.BadZipFile):
        return
    targets: dict[str | None, dict] = {
        None: next((t for t in project["targets"] if t.get("isStage")), None)
    }
    for target in project["targets"]:
        if not target.get("isStage"):
            targets[target["name"]] = target
    applied = 0
    for record in records:
        target = targets.get(record.get("target"))
        rcx = record.get("rotationCenterX")
        rcy = record.get("rotationCenterY")
        if target is None or rcx is None or rcy is None:
            continue
        costumes = target.get("costumes", [])
        name = record.get("name")
        matches = [c for c in costumes if c.get("name") == name]
        if len(matches) == 1:
            costume = matches[0]
        else:
            index = record.get("index")
            costume = (
                costumes[index]
                if isinstance(index, int) and 0 <= index < len(costumes)
                else None
            )
        if costume is None:
            continue
        costume["rotationCenterX"] = rcx
        costume["rotationCenterY"] = rcy
        costume["bitmapResolution"] = record.get("bitmapResolution", 1)
        applied += 1
    if not applied:
        return
    tmp = sb3_path.with_name(sb3_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("project.json", json.dumps(project, ensure_ascii=False))
            for name, data in members:
                archive.writestr(name, data)
        os.replace(tmp, sb3_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# --- Preflight: Scratch compatibility + memory (offline, no browser) ----------

PREFLIGHT_SCHEMA = 1
_PREFLIGHT_DEFAULTS = {
    "jsonBudget": 5 * 1024 * 1024,      # Scratch's "5 MB" = 5 MiB
    "assetBudget": 10 * 1024 * 1024,    # Scratch's "10 MB" = 10 MiB
    "listLimit": 200_000,
    "memoryBudget": 2 * 1024 ** 3,      # observed ~2 GiB browser ceiling (empirical)
    "svgMemoryFactor": 2,               # SVG pays texture + per-skin canvas (~2x bitmap)
    "svgMaxWidth": 2400,
    "svgMaxHeight": 1800,
}


def _memory_suggestions(report: dict, budgets: dict) -> list[str]:
    memory = report['memory']
    suggestions = []
    if memory['svgPx'] > 0:
        suggestions.append(
            f"convert SVG costumes to bitmap (~{budgets['svgMemoryFactor']}x saving on those)")
    if memory['bitmapPx'] + memory['svgPx'] > 0:
        suggestions.append('downscale costumes/frames (memory scales with area)')
    suggestions.append('reduce stored frames or pack into a spritesheet with fewer, smaller frames')
    if report['sounds']['decodedBytes'] > 0:
        suggestions.append('shorten or drop large sounds (decoded to whole-file PCM in memory)')
    suggestions.append('remove unused costumes and sounds')
    return suggestions


def preflight_budgets() -> dict:
    """Thresholds: ``GSDEV_PREFLIGHT_*`` env -> gobo-agent.json -> conservative default."""
    values = {}
    for key, default in _PREFLIGHT_DEFAULTS.items():
        camel = "preflight" + key[0].upper() + key[1:]
        env = "GSDEV_PREFLIGHT_" + re.sub(r"(?<!^)(?=[A-Z])", "_", key).upper()
        values[key] = _int_setting(env, camel, default)
    return values


def _png_dims(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def _jpeg_dims(data: bytes):
    i, end = 2, len(data)
    while i + 9 < end:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > end:
            break
        size = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (w, h)
        if size < 2:
            break
        i += 2 + size
    return None


def _bmp_dims(data: bytes):
    if data[:2] != b"BM" or len(data) < 26:
        return None
    w, h = struct.unpack("<ii", data[18:26])
    return (abs(w), abs(h))


def _gif_dims(data: bytes):
    if data[:3] != b"GIF" or len(data) < 10:
        return None
    return struct.unpack("<HH", data[6:10])


def _svg_dims(data: bytes) -> dict:
    head = data[:4000]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        return {"status": "unresolved", "reason": "doctype"}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {"status": "unresolved", "reason": "parse"}

    def scalar(value):
        if not value:
            return None
        match = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*(px)?\s*$", value)
        return float(match.group(1)) if match else None

    width, height = scalar(root.attrib.get("width")), scalar(root.attrib.get("height"))
    if width and height:
        return {"status": "px", "width": width, "height": height}
    view_box = root.attrib.get("viewBox")
    if view_box:
        parts = view_box.replace(",", " ").split()
        if len(parts) == 4:
            try:
                return {"status": "viewbox", "width": abs(float(parts[2])), "height": abs(float(parts[3]))}
            except ValueError:
                pass
    return {"status": "unresolved", "reason": "dim"}


def _pf(level: str, code: str, message: str, detail=None) -> dict:
    finding = {'level': level, 'code': code, 'message': message}
    if detail is not None:
        finding['detail'] = detail
    return finding


def preflight_scan(sb3_path: Path) -> dict:
    """Offline Scratch compatibility/size/memory check on an sb3 (no browser)."""
    budgets = preflight_budgets()
    findings: list[dict] = []
    report = {
        'schema': PREFLIGHT_SCHEMA,
        'artifact': str(sb3_path),
        'sha256': _sha256_file(sb3_path),
        'budgets': budgets,
        'json': {'bytes': 0, 'budget': budgets['jsonBudget'], 'over': False},
        'assets': {'count': 0, 'totalBytes': 0, 'over': [], 'largest': []},
        'lists': {'max': 0, 'over': [], 'atLimit': []},
        'memory': {
            'bitmapPx': 0, 'svgPx': 0, 'svgUnresolved': 0,
            'svgMemoryFactor': budgets['svgMemoryFactor'], 'effectivePx': 0,
            'bytes': 0, 'budget': budgets['memoryBudget'], 'percent': 0.0,
            'overDimension': [], 'largest': [],
        },
        'sounds': {'count': 0, 'bytes': 0, 'decodedBytes': 0, 'unresolved': 0},
        'coverage': {'unresolved': 0},
        'findings': findings,
    }
    try:
        with zipfile.ZipFile(sb3_path) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names):
                findings.append(_pf('error', 'duplicate-zip-entries', 'archive has duplicate entry names'))
            if 'project.json' not in names:
                findings.append(_pf('error', 'missing-project-json', 'no project.json in the archive'))
                return report
            project_bytes = archive.read('project.json')
            sizes = {n: archive.getinfo(n).file_size for n in names if n != 'project.json'}
            total_uncompressed = sum(sizes.values()) + len(project_bytes)
            if len(names) > _PREFLIGHT_MAX_ENTRIES or total_uncompressed > _PREFLIGHT_MAX_UNCOMPRESSED:
                findings.append(_pf('error', 'scan-incomplete',
                                    f"archive too large to scan safely "
                                    f"({len(names)} entries, {total_uncompressed:,} bytes uncompressed)"))
                return report
            data = json.loads(project_bytes)
            targets = data.get('targets', [])

            report['assets']['count'] = len(sizes)
            report['assets']['totalBytes'] = sum(sizes.values())
            over_assets = [{'name': n, 'bytes': s} for n, s in sizes.items() if s > budgets['assetBudget']]
            report['assets']['over'] = sorted(over_assets, key=lambda x: -x['bytes'])[:10]
            report['assets']['largest'] = sorted(
                ({'name': n, 'bytes': s} for n, s in sizes.items()), key=lambda x: -x['bytes']
            )[:10]
            if over_assets:
                findings.append(_pf('error', 'asset-over-budget',
                                    f"{len(over_assets)} asset(s) exceed {budgets['assetBudget']} bytes",
                                    {'assets': report['assets']['over'],
                                     'suggestions': ['compress or downscale the asset below 10 MiB',
                                                     'split a long sound into shorter pieces']}))

            cache: dict[str, dict] = {}
            costume_px: list[tuple] = []
            for target in targets:
                for costume in target.get('costumes', []):
                    fmt = costume.get('dataFormat')
                    md5ext = costume.get('md5ext') or f"{costume.get('assetId')}.{fmt}"
                    info = cache.get(md5ext)
                    if info is None:
                        info = {'kind': fmt, 'dims': None, 'unresolved': False}
                        try:
                            blob = archive.read(md5ext)
                        except KeyError:
                            info['unresolved'] = True
                        else:
                            if fmt == 'png':
                                dims = _png_dims(blob)
                            elif fmt in ('jpg', 'jpeg'):
                                dims = _jpeg_dims(blob)
                            elif fmt == 'bmp':
                                dims = _bmp_dims(blob)
                            elif fmt == 'gif':
                                dims = _gif_dims(blob)
                            elif fmt == 'svg':
                                dims = _svg_dims(blob)
                            else:
                                dims = None
                            if isinstance(dims, dict):
                                if dims.get('status') == 'unresolved':
                                    info['unresolved'] = True
                                else:
                                    info['dims'] = (dims['width'], dims['height'])
                            elif dims:
                                info['dims'] = dims
                            else:
                                info['unresolved'] = True
                        cache[md5ext] = info
                    if info['unresolved'] or not info['dims']:
                        report['coverage']['unresolved'] += 1
                        if info['kind'] == 'svg':
                            report['memory']['svgUnresolved'] += 1
                        continue
                    width, height = info['dims']
                    pixels = int(round(width * height))
                    if info['kind'] == 'svg':
                        report['memory']['svgPx'] += pixels
                        if width > budgets['svgMaxWidth'] or height > budgets['svgMaxHeight']:
                            report['memory']['overDimension'].append(
                                {'sprite': target.get('name'), 'costume': costume.get('name'),
                                 'width': width, 'height': height})
                    else:
                        report['memory']['bitmapPx'] += pixels
                    costume_px.append((pixels, target.get('name'), costume.get('name'), info['kind']))

            for target in targets:
                for entry in target.get('lists', {}).values():
                    count = len(entry[1]) if isinstance(entry[1], list) else 0
                    report['lists']['max'] = max(report['lists']['max'], count)
                    if count > budgets['listLimit']:
                        report['lists']['over'].append(
                            {'sprite': target.get('name'), 'list': entry[0], 'items': count})
                    elif count == budgets['listLimit']:
                        report['lists']['atLimit'].append(
                            {'sprite': target.get('name'), 'list': entry[0], 'items': count})
            if report['lists']['over']:
                findings.append(_pf('error', 'list-over-capacity',
                                    f"list(s) exceed {budgets['listLimit']} items", report['lists']['over']))
            if report['lists']['atLimit']:
                findings.append(_pf('warning', 'list-at-capacity',
                                    f"list(s) at the {budgets['listLimit']}-item limit", report['lists']['atLimit']))

            for target in targets:
                for sound in target.get('sounds', []):
                    fmt = sound.get('dataFormat')
                    md5ext = sound.get('md5ext') or f"{sound.get('assetId')}.{fmt}"
                    report['sounds']['count'] += 1
                    report['sounds']['bytes'] += sizes.get(md5ext, 0)
                    try:
                        blob = archive.read(md5ext)
                    except KeyError:
                        report['sounds']['unresolved'] += 1
                        continue
                    pcm = _sound_pcm_bytes(blob, fmt, len(blob))
                    if pcm is None:
                        report['sounds']['unresolved'] += 1
                    else:
                        report['sounds']['decodedBytes'] += pcm

        report['json']['bytes'] = len(project_bytes)
        report['json']['over'] = len(project_bytes) > budgets['jsonBudget']
        if budgets['jsonBudget']:
            report['json']['percent'] = round(len(project_bytes) / budgets['jsonBudget'] * 100, 1)
        if report['json']['over']:
            findings.append(_pf('error', 'json-over-budget',
                                f"project.json is {len(project_bytes):,} bytes > {budgets['jsonBudget']:,}",
                                {'suggestions': [
                                    'clean up lists: drop duplicate/unused list data and list-monitor entries',
                                    'remove redundant shadow blocks (unused menu/shadow inputs) from project.json',
                                    'shorten large list literals / long strings',
                                    'remove unused blocks, variables and lists',
                                    'move big data into assets instead of project.json',
                                ]}))
        elif len(project_bytes) >= budgets['jsonBudget'] * 9 // 10:
            findings.append(_pf('warning', 'json-near-budget',
                                f"project.json is {len(project_bytes):,} bytes "
                                f"(~{report['json']['percent']}% of {budgets['jsonBudget']:,})"))
        report['memory']['largest'] = [
            {'pixels': p, 'sprite': s, 'costume': c, 'kind': k}
            for p, s, c, k in sorted(costume_px, reverse=True)[:10]
        ]
        memory = report['memory']
        memory['effectivePx'] = int(memory['bitmapPx'] + budgets['svgMemoryFactor'] * memory['svgPx'])
        memory['bytes'] = memory['effectivePx'] * 4 + report['sounds']['decodedBytes']
        memory['percent'] = round(memory['bytes'] / budgets['memoryBudget'] * 100, 1) if budgets['memoryBudget'] else 0.0
        if memory['bytes'] > budgets['memoryBudget']:
            findings.append(_pf('error', 'memory-over-budget',
                f"estimated memory ~{memory['bytes'] / 2 ** 30:.2f} GiB "
                f"({memory['percent']}% of {budgets['memoryBudget'] / 2 ** 30:.0f} GiB)",
                {'bitmapPx': memory['bitmapPx'], 'svgPx': int(memory['svgPx']),
                 'svgFactor': budgets['svgMemoryFactor'],
                 'soundPcmBytes': report['sounds']['decodedBytes'],
                 'suggestions': _memory_suggestions(report, budgets)}))
        if memory['overDimension']:
            findings.append(_pf('warning', 'svg-over-dimension',
                                f"{len(memory['overDimension'])} SVG(s) exceed "
                                f"{budgets['svgMaxWidth']}x{budgets['svgMaxHeight']}",
                                memory['overDimension'][:10]))
        if report['coverage']['unresolved']:
            findings.append(_pf('warning', 'unresolved-assets',
                                f"{report['coverage']['unresolved']} asset(s) had no measurable "
                                'dimensions (estimate is partial)'))
        if report['sounds']['decodedBytes'] > 100_000_000:
            findings.append(_pf('warning', 'sounds-decoded-large',
                                f"decoded sound PCM ~{report['sounds']['decodedBytes']:,} bytes "
                                f"({report['sounds']['unresolved']} unmeasured)"))
    except (OSError, zipfile.BadZipFile, ValueError) as error:
        findings.append(_pf('error', 'invalid-archive', f'cannot read archive: {error}'))
    return report


def print_preflight(report: dict) -> None:
    j, m, a, l = report['json'], report['memory'], report['assets'], report['lists']
    print(f"project.json: {j['bytes']:,} / {j['budget']:,} bytes"
          f" ({j.get('percent', 0.0)}%)" + ('  OVER' if j['over'] else ''))
    print(f"assets: {a['count']} unique, {a['totalBytes']:,} bytes total, "
          f"{len(a['over'])} over {report['budgets']['assetBudget']:,}")
    print(f"memory: ~{m['bytes'] / 2 ** 30:.2f} GiB ({m['percent']}% of {m['budget'] / 2 ** 30:.0f} GiB)"
          f"  = (bitmap {m['bitmapPx']:,} px + svg {int(m['svgPx']):,} px x{m['svgMemoryFactor']}) x4"
          f" + {report['sounds']['decodedBytes']:,} B sound PCM")
    print(f"svg unresolved: {m['svgUnresolved']}")
    print(f"largest list: {l['max']:,} / {report['budgets']['listLimit']:,} items")
    s = report['sounds']
    print(f"sounds: {s['count']} ({s['bytes']:,} bytes, ~{s['decodedBytes']:,} PCM, "
          f"{s['unresolved']} unmeasured)")
    print(f"measurement gaps: {report['coverage']['unresolved']}")
    print(f"sha256: {report.get('sha256', '')[:16]}...")
    for item in report['memory']['largest'][:5]:
        print(f"  largest: {item['sprite']}::{item['costume']} {item['pixels']:,} px ({item['kind']})")
    for finding in report['findings']:
        print(f"[{finding['level']:7}] {finding['code']}: {finding['message']}")
        detail = finding.get('detail')
        if isinstance(detail, dict):
            for suggestion in detail.get('suggestions', []):
                print(f"            -> {suggestion}")


def cmd_preflight(args: argparse.Namespace) -> int:
    ensure_dependencies(need_bundles=False)
    if getattr(args, 'sb3', ''):
        path = Path(args.sb3).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            raise SystemExit(f'--sb3 not found: {path}')
    else:
        path = build_project(resolve_mode(args))
    report = preflight_scan(path)
    errors = [f for f in report['findings'] if f['level'] == 'error']
    warnings = [f for f in report['findings'] if f['level'] == 'warning']
    if getattr(args, 'json', False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_preflight(report)
    if errors or (getattr(args, 'strict', False) and warnings):
        return 1
    return 0


_PREFLIGHT_MAX_ENTRIES = 200_000
_PREFLIGHT_MAX_UNCOMPRESSED = 2 * 1024 ** 3
_PREFLIGHT_MAX_SVG_READ = 32 * 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


_MP3_BITRATES = {
    1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MP3_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _wav_pcm_bytes(data: bytes):
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return None
    i = 12
    while i + 8 <= len(data):
        chunk = data[i:i + 4]
        size = struct.unpack('<I', data[i + 4:i + 8])[0]
        body = i + 8
        if chunk == b'data':
            return size
        i = body + size + (size & 1)
    return None


def _mp3_pcm_bytes(data: bytes, file_bytes: int):
    for i in range(min(len(data) - 4, 8192)):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            version_bits = (data[i + 1] >> 3) & 0x03
            bitrate_index = (data[i + 2] >> 4) & 0x0F
            rate_index = (data[i + 2] >> 2) & 0x03
            if version_bits == 1 or bitrate_index in (0, 15) or rate_index == 3:
                continue
            table = _MP3_BITRATES[1 if version_bits == 3 else 2]
            rates = _MP3_RATES.get(version_bits)
            if not rates:
                continue
            bitrate = table[bitrate_index] * 1000
            rate = rates[rate_index]
            channels = 1 if ((data[i + 3] >> 6) & 0x03) == 3 else 2
            if bitrate <= 0:
                continue
            seconds = file_bytes * 8 / bitrate
            return int(seconds * rate * channels * 2)
    return None


def _sound_pcm_bytes(data: bytes, fmt: str, file_bytes: int):
    if fmt == 'wav':
        return _wav_pcm_bytes(data)
    if fmt == 'mp3':
        return _mp3_pcm_bytes(data, file_bytes)
    return None


def build_project_at(root: Path, mode: str = "debug") -> Path:
    goboscript = resolve_goboscript()
    if goboscript is None:
        raise SystemExit(
            "goboscript is not on PATH. "
            f"{_setup_hint()} (or install the GoboScript compiler)."
        )
    mode = mode if mode in MODE_MACROS else "debug"
    run_prebuild(root, mode)
    # The mode include is a checked-in project file (default `tools/gsdev_mode.gs`,
    # overridable via config `modeInclude`). Rewrite it for this build and restore
    # it afterwards, so building never changes the working tree. A project that does
    # not opt in (no such file) builds unchanged.
    mode_file = mode_include_path(root)
    original = mode_file.read_text(encoding="utf-8") if mode_file.is_file() else None
    if original is not None:
        mode_file.write_text(MODE_MACROS[mode], encoding="utf-8")
        log(f"building {root} ({mode})")
    else:
        log(f"building {root}")
    sb3_path = root / (root.name + ".sb3")
    # Rely on cwd for the input (3.2.1 uses -i/--input, older builds a positional).
    command = [goboscript, "build", "-o", str(sb3_path)]
    try:
        if JSON_MODE:
            # Keep stdout clean for the JSON report; decode diagnostics as UTF-8 so a
            # CP932/Japanese Windows locale cannot corrupt them.
            result = subprocess.run(
                command, cwd=str(root), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            print((result.stdout or "") + (result.stderr or ""), end="",
                  file=sys.stderr, flush=True)
        else:
            result = subprocess.run(command, cwd=str(root))
    finally:
        if original is not None:
            mode_file.write_text(original, encoding="utf-8")
    if result.returncode != 0:
        raise SystemExit(f"goboscript build failed (exit {result.returncode}).")
    inject_monitors(sb3_path, root)
    inject_costumes(sb3_path, root)
    return sb3_path


def build_project(mode: str = "debug") -> Path:
    return build_project_at(PROJECT_ROOT, mode)


def poll_host(cdp: CDP, since: int = 0) -> dict:
    """One evaluate per run loop: drained logs, stopped flag, and new errors."""
    return cdp.evaluate(f"window.__host.poll({int(since)})") or {}


def print_event(event: dict) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{event['level'].upper()} {stamp} {event['sprite']}] {event['value']}", flush=True)


def cmd_build(args: argparse.Namespace) -> int:
    ensure_dependencies(need_bundles=False)
    build_project(resolve_mode(args))
    log(f"built {SB3_PATH.name}")
    return 0


def artifact_path(args: argparse.Namespace) -> Path:
    """The sb3 to load: ``--sb3 PATH`` when given, else the project's build output."""
    given = getattr(args, "sb3", "")
    if given:
        path = Path(given).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            raise SystemExit(f"--sb3 not found: {path}")
        return path
    return SB3_PATH


def cmd_run(args: argparse.Namespace) -> int:
    ensure_dependencies(need_bundles=True)
    if not getattr(args, "sb3", "") and not args.no_build and not args.no_reload:
        build_project(resolve_mode(args))
    artifact = artifact_path(args)
    t0 = time.monotonic()
    cdp, launched = ensure_host(args.port, args.headless, args.software)
    dismiss_browser_dialogs(args.port)
    ready = time.monotonic() - t0
    apply_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
    try:
        if args.no_reload:
            log(
                f"host {'launched' if launched else 'warm'}: browser {ready:.2f}s, "
                "reusing the running project (--no-reload)"
            )
        else:
            info = load_host_project(cdp, artifact)
            log(
                f"host {'launched' if launched else 'warm'}: browser {ready:.2f}s, "
                f"load {info['load']:.0f} ms"
            )
        log("running; press Ctrl+C to stop")
        deadline = time.monotonic() + args.duration if args.duration else None
        errors_seen = 0
        next_dialog_check = time.monotonic()
        next_perf = time.monotonic() + 1.0
        try:
            while True:
                if time.monotonic() >= next_dialog_check:
                    dismiss_browser_dialogs(args.port)
                    next_dialog_check = time.monotonic() + 2.0
                if time.monotonic() >= next_perf:
                    p = cdp.evaluate("window.__host.perf()") or {}
                    log(f"[perf] fps={float(p.get('stepFps') or 0):.1f} "
                        f"step_ms={float(p.get('steptimeAvg') or 0):.2f} "
                        f"render_ms={float(p.get('rendertimeAvg') or 0):.2f}")
                    next_perf = time.monotonic() + 1.0
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
    ensure_dependencies(need_bundles=True)
    if not getattr(args, "sb3", "") and not args.no_build:
        build_project(resolve_mode(args))
    artifact = artifact_path(args)
    out_path = Path(args.out).expanduser() if args.out else DEBUG_DIR / "stage.png"
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    cdp, _ = ensure_host(args.port, args.headless, args.software)
    dismiss_browser_dialogs(args.port)
    apply_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
    try:
        load_host_project(cdp, artifact)
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
    port = args.port
    if port is None:
        # Prefer the CDP port the running host recorded; fall back to the
        # remembered auto port (or a fresh one) when there is no recorded host.
        recorded = read_host_state().get("cdpPort")
        port = recorded if isinstance(recorded, int) and recorded > 0 else resolve_port(0)
    else:
        port = resolve_port(port)
    close_host(port)
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
    if selector.startswith("@"):
        return "@perf", selector[1:], None
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
    if target == "@perf":
        if value is not _UNSET:
            return "({error: 'read-only: @' + " + json.dumps(name) + "})"
        return (
            "(() => {"
            " const _host = window.__host;"
            " if (!_host || !_host.perfValue) return {error: 'no host (open the project with run first)'};"
            " const _want = " + json.dumps(name) + ";"
            " const _out = _host.perfValue(_want);"
            " if (typeof _out === 'undefined') return {error: 'unknown perf metric: ' + _want};"
            " return {value: _out, target: '@perf', name: _want, index: null};"
            "})()"
        )
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
        "   if (_s.target === '@perf') { _refs.push({label: _s.label, ref: {__perf: _s.name}, index: null}); continue; }"
        "   const _t = _host.findTarget(_s.target);"
        "   if (!_t) return {error: 'target not found: ' + _s.target};"
        "   let _v = null;"
        "   for (const _k in _t.variables) { if (_t.variables[_k].name === _s.name) { _v = _t.variables[_k]; break; } }"
        "   if (!_v) return {error: 'variable not found: ' + _s.name};"
        "   _refs.push({label: _s.label, ref: _v, index: _s.index});"
        " }"
        " const _read = (_r) => (_r.ref.__perf)"
        "   ? window.__host.perfValue(_r.ref.__perf)"
        "   : ((_r.index === null)"
        "     ? _r.ref.value"
        "     : (Array.isArray(_r.ref.value) ? _r.ref.value[_r.index - 1] : undefined));"
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
        result = cdp.evaluate(_var_js(target, name, index, value)) or {}
        log(json.dumps({"selector": args.selector, **result}))
        return 1 if result.get("error") else 0
    finally:
        cdp.close()


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


SESSION_SCHEMA = 1


def run_session_lines(cdp: CDP, lines, quiet: bool = False,
                      event_sink=None) -> dict:
    """Run session lines over one open CDP connection.

    Prints the same ``[ASSERT ...]`` / ``[gsdev] ...`` output as the ``session``
    command and returns ``{"asserts": [{"ok", "msg"}], "failures": N}`` where
    ``failures`` counts every failed assertion and every timeout/error verb.

    ``event_sink`` (a callable) receives one versioned structured record per
    assertion plus a final ``session_end``, written as the run proceeds, so a parent
    can assemble a failure bundle without scraping stdout or re-reading the page.

    A ``capture SELECTOR...`` line declares extra state to read together with the
    following ``expect``, in the same synchronous in-page evaluation, so a bundle
    shows assertion-time state rather than a later (drifting) re-read.
    """
    failures = 0
    asserts = []
    watches = []
    captures = []
    started = time.time()
    current_line = 0

    def emit(rec: dict) -> None:
        if event_sink is None:
            return
        rec.setdefault("schema", SESSION_SCHEMA)
        try:
            event_sink(rec)
        except (OSError, TypeError, ValueError):
            pass

    def record(ok: bool, message: str, **extra) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        if not quiet:
            print(f"[ASSERT {'ok' if ok else 'FAIL'}] {message}", flush=True)
        asserts.append({"ok": ok, "msg": message})
        rec = {"kind": "assert", "ok": ok, "msg": message, "ts": time.time(),
               "seq": len(asserts)}
        rec.update(extra)
        rec.setdefault("line", current_line)
        emit(rec)

    try:
        for lineno, raw in enumerate(lines, 1):
            current_line = lineno
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            op = parts[0].lower()
            if op == "capture" and len(parts) >= 2:
                for selector in parts[1:]:
                    if selector not in captures:
                        captures.append(selector)
                log(json.dumps({"capture": captures}))
            elif op == "set" and len(parts) >= 3:
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
                extra_selectors = [s for s in captures if s != selector]
                if extra_selectors:
                    # One synchronous evaluation: assertion + declared state together.
                    res = cdp.evaluate(
                        _assert_capture_js(selector, extra_selectors)) or {}
                    actual = (res.get("actual") or {}).get("value")
                    capture = {"source": res.get("actual")}
                    capture.update(res.get("capture") or {})
                else:
                    actual = cdp.evaluate(select_js(selector)).get("value")
                    capture = None
                record(
                    compare(actual, operator, expected),
                    f"{selector} {operator} {expected!r} (actual {actual!r})",
                    selector=selector, operator=operator, expected=expected,
                    actual=actual, capture=capture,
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
                ok = actual.lower() == expected
                capture = None
                atomic = None
                if not ok and captures:
                    # A pixel read is a promise, so pixel-failure captures are read
                    # separately; label that they are not simultaneous.
                    capture = cdp.evaluate(_capture_js(captures)) or {}
                    atomic = False
                record(
                    ok,
                    f"pixel {parts[1]} {parts[2]} == {expected} (actual {actual})",
                    selector=f"pixel {parts[1]} {parts[2]}", expected=expected,
                    actual=actual, capture=capture, capture_atomic=atomic,
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
            elif op == "perf":
                log(json.dumps(cdp.evaluate("window.__host.perf()")))
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
        emit({"kind": "session_end", "failures": failures, "asserts": len(asserts),
              "elapsed": round(time.time() - started, 3)})
    finally:
        try:
            cdp.evaluate(STOP_WATCH_JS)
        except (CDPError, TimeoutError, OSError):
            pass
    return {"asserts": asserts, "failures": failures, "watches": watches}


def _capture_js(selectors) -> str:
    entries = ", ".join(f"{json.dumps(s)}: {select_js(s)}" for s in selectors)
    return f"({{ {entries} }})"


def _assert_capture_js(selector: str, extra_selectors) -> str:
    caps = ", ".join(f"{json.dumps(s)}: {select_js(s)}" for s in extra_selectors)
    return f"(() => ({{ actual: {select_js(selector)}, capture: {{ {caps} }} }}))()"


def _event_writer(path: str):
    """Open a JSONL sink that flushes each record, for crash/timeout survival."""
    handle = open(path, "w", encoding="utf-8")

    def write(record: dict) -> None:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()

    write.close = handle.close
    return write


def _read_events(path: str) -> list[dict]:
    records = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            # An interrupted final write is expected after a timeout or crash.
            continue
    return records


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


BUNDLE_SCHEMA = 1


def assemble_bundle(*, artifacts: Path, label: str, events, session_path: str,
                    project_root: Path, sb3_path: Path | None,
                    screenshot: bytes | None = None, errors: int = 0,
                    partial: bool = False) -> Path:
    """Write a failure bundle from the session's structured records.

    Built only from records captured at assertion time; never re-reads the page. An
    incomplete trailing record (timeout/crash) is tolerated, and a truncated event
    stream marks the bundle ``partial``.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bundle = artifacts / f"{label}-failure-{stamp}"
    suffix = 1
    while bundle.exists():
        bundle = artifacts / f"{label}-failure-{stamp}-{suffix}"
        suffix += 1
    bundle.mkdir(parents=True)
    asserts = [e for e in events if isinstance(e, dict) and e.get("kind") == "assert"]
    failed = [e for e in asserts if not e.get("ok")]
    ends = [e for e in events if isinstance(e, dict) and e.get("kind") == "session_end"]
    if ends and int(ends[-1].get("failures") or 0) != len(failed):
        partial = True
    state = {}
    for e in failed:
        capture = e.get("capture")
        if isinstance(capture, dict):
            state.update(capture)
    build = {}
    if sb3_path and sb3_path.exists():
        try:
            build = {"sb3": sb3_path.name, "bytes": sb3_path.stat().st_size,
                     "sha256": hashlib.sha256(sb3_path.read_bytes()).hexdigest()[:16]}
        except OSError:
            build = {"sb3": sb3_path.name}
    manifest = {
        "schema": BUNDLE_SCHEMA, "kind": "failure-bundle",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "partial": bool(partial),
        "category": "assertion" if failed else ("error" if errors else "unknown"),
        "session": session_path, "project": str(project_root), "build": build,
        "failures": len(failed), "errors": int(errors), "asserts": len(asserts),
        "artifacts": {"manifest": "manifest.json", "failures": "failures.json",
                      "state": "state.json", "events": "events.jsonl",
                      "reproduction": "reproduction.txt"},
    }
    _atomic_write(bundle / "failures.json",
                  (json.dumps(failed, indent=2) + "\n").encode("utf-8"))
    _atomic_write(bundle / "state.json",
                  (json.dumps(state, indent=2) + "\n").encode("utf-8"))
    _atomic_write(bundle / "events.jsonl",
                  "".join(json.dumps(e, separators=(",", ":")) + "\n"
                          for e in events).encode("utf-8"))
    _atomic_write(bundle / "reproduction.txt", (
        f"# reproduction\nproject: {project_root}\nsession: {session_path}\n"
        f"build:   {build.get('sb3', 'n/a')}\n\n"
        "# warm host:\n"
        "python tools/gsdev.py run --headless --leave-running\n"
        f"python tools/gsdev.py session --file \"{session_path}\"\n"
    ).encode("utf-8"))
    if screenshot:
        try:
            _atomic_write(bundle / "stage.png", screenshot)
            manifest["artifacts"]["stage"] = "stage.png"
        except OSError:
            manifest["stage_capture_error"] = True
    _atomic_write(bundle / "manifest.json",
                  (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return bundle


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
    artifacts = Path(args.artifacts).expanduser() if getattr(args, "artifacts", "") else DEBUG_DIR
    if not artifacts.is_absolute():
        artifacts = PROJECT_ROOT / artifacts
    want_bundle = bool(getattr(args, "bundle", False))
    events_path = getattr(args, "events", "") or ""
    if want_bundle and not events_path:
        artifacts.mkdir(parents=True, exist_ok=True)
        events_path = str(artifacts / f"session-{os.getpid()}.events.jsonl")
    sink = _event_writer(events_path) if events_path else None
    source = getattr(args, "file", None)
    cdp = open_live_cdp(args.port)
    if cdp is None:
        if sink:
            sink.close()
        return 1
    bundle_path = None
    try:
        if source:
            # UTF-8 file input: PowerShell 5.1 pipes native-command input as the
            # legacy code page and destroys CJK before Python can read it.
            stream = io.StringIO(Path(source).read_text(encoding="utf-8"))
        else:
            stream = sys.stdin
            if os.name == "nt":
                log("[hint] PowerShell 5.1 pipes are not UTF-8; use `session --file PATH` "
                    "for non-ASCII (CJK) selectors/values")
        result = run_session_lines(cdp, stream, quiet=JSON_MODE, event_sink=sink)
        if sink:
            sink.close()
        if want_bundle and result["failures"]:
            src = Path(source) if source else None
            root = project_for_test(src) if src else PROJECT_ROOT
            try:
                screenshot = capture_stage_png(cdp)
            except Exception:  # noqa: BLE001
                screenshot = None
            try:
                bundle_path = assemble_bundle(
                    artifacts=artifacts, label=(src.stem if src else "session"),
                    events=_read_events(events_path),
                    session_path=str(src) if src else "<stdin>",
                    project_root=root, sb3_path=root / (root.name + ".sb3"),
                    screenshot=screenshot)
                log(f"bundle: {bundle_path}")
            except OSError as error:
                log(f"bundle: capture failed ({error}); original failure stands")
    finally:
        if sink is not None:
            try:
                sink.close()
            except OSError:
                pass
        cdp.close()
    if JSON_MODE:
        payload = {"session": result}
        if bundle_path:
            payload["bundle"] = str(bundle_path)
        print(json.dumps(payload, separators=(",", ":")), flush=True)
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


MANIFEST_SCHEMA = 1


def discover_tests(manifest_path: Path) -> tuple[list[dict], list[str]]:
    """Read a versioned test manifest; validate without running anything.

    Returns ``(tests, errors)``. Each test is ``{id, label, path, root, timeout,
    buildMode}`` with resolved paths. Test ``root`` values resolve against the
    configured project root (so a manifest vendored inside the project targets the
    parent project with ``root: "."``), unless the manifest declares its own
    ``base`` relative to itself. Validation covers the schema, required fields,
    duplicate ids, roots/projects existing, and sessions staying under their
    declared root (no path escape). Read-only: no builds, no browser.
    """
    errors: list[str] = []
    tests: list[dict] = []
    if not manifest_path.is_file():
        return [], [f"{manifest_path}: not found"]
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [], [f"{manifest_path}: {error}"]
    if not isinstance(data, dict):
        return [], [f"{manifest_path}: top level must be an object"]
    if data.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"{manifest_path}: schema must be {MANIFEST_SCHEMA}")
    manifest_dir = manifest_path.resolve().parent
    # Roots are relative to the configured project root by default, so a manifest
    # vendored inside the project (e.g. gobo-agent/gobo-tests.json) targets the
    # parent project with root ".". A manifest may override the base relative to
    # itself with "base" (e.g. "." for a manifest-local layout).
    base_field = data.get("base")
    if isinstance(base_field, str) and base_field:
        base = (manifest_dir / base_field).resolve()
    else:
        base = PROJECT_ROOT
    roots = data.get("roots") or {}
    default_root = data.get("defaultRoot")
    raw = data.get("tests")
    if not isinstance(raw, list) or not raw:
        errors.append(f"{manifest_path}: 'tests' must be a non-empty list")
        return [], errors
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"tests[{index}]: must be an object")
            continue
        test_id = item.get("id")
        session = item.get("session")
        if not isinstance(test_id, str) or not test_id:
            errors.append(f"tests[{index}]: 'id' is required")
            test_id = f"tests[{index}]"
        elif test_id in seen:
            errors.append(f"{test_id}: duplicate id")
        else:
            seen.add(test_id)
        if not isinstance(session, str) or not session:
            errors.append(f"{test_id}: 'session' is required")
            continue
        root_spec = item.get("root") or default_root or "."
        root_dir = (base / roots.get(root_spec, root_spec)).resolve()
        try:
            root_dir.relative_to(base)
        except ValueError:
            errors.append(f"{test_id}: root {root_spec!r} escapes the manifest base")
            continue
        if not (root_dir / "goboscript.toml").is_file():
            errors.append(f"{test_id}: root {root_spec!r} has no goboscript.toml")
            continue
        session_path = (root_dir / session).resolve()
        try:
            session_path.relative_to(root_dir)
        except ValueError:
            errors.append(f"{test_id}: session {session!r} escapes its root")
            continue
        if not session_path.is_file():
            errors.append(f"{test_id}: session {session!r} not found under {root_dir}")
            continue
        tests.append({"id": test_id, "label": item.get("label") or test_id,
                      "path": session_path, "root": root_dir,
                      "timeout": item.get("timeout"), "buildMode": item.get("buildMode")})
    return tests, errors


def cmd_test(args: argparse.Namespace) -> int:
    manifest_data = None
    if args.manifest:
        manifest_path = Path(args.manifest)
        items, errors = discover_tests(manifest_path)
        if errors:
            for error in errors:
                log(f"manifest: {error}")
            return 1
        if args.list:
            print(json.dumps({
                "schema": MANIFEST_SCHEMA,
                "tests": [{"id": t["id"], "label": t["label"],
                           "session": str(t["path"]), "root": str(t["root"]),
                           "build_mode": t.get("buildMode")} for t in items],
            }, separators=(",", ":")), flush=True)
            return 0
        if not items:
            log("manifest: no tests")
            return 1
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest_data = None
    else:
        if args.list:
            log("--list requires --manifest")
            return 1
        if not args.files:
            log("test: pass FILE... or --manifest PATH")
            return 1
        items = [{"id": Path(f).stem, "label": Path(f).stem, "path": Path(f),
                  "root": project_for_test(Path(f)), "timeout": None,
                  "buildMode": None} for f in args.files]

    if args.artifacts:
        artifacts = Path(args.artifacts).expanduser()
    else:
        spec = (manifest_data or {}).get("artifacts")
        artifacts = Path(spec).expanduser() if spec else DEBUG_DIR
    if not artifacts.is_absolute():
        artifacts = PROJECT_ROOT / artifacts
    report = {"files": [], "totals": {"files": 0, "asserts": 0, "failures": 0}}
    any_failed = False
    # Like build/run/screenshot/profile, install the prebuilt tools on first use:
    # the test path builds a project and launches a host.
    ensure_dependencies(need_bundles=True)
    for item in items:
        path = item["path"]
        root = item["root"]
        timeout = item.get("timeout") or args.timeout
        sb3_path = root / (root.name + ".sb3")
        entry = {"id": item["id"], "label": item["label"], "path": str(path),
                 "root": str(root), "build_mode": item.get("buildMode"),
                 "asserts": [], "failures": 0, "errors": 0}
        if not args.no_build:
            build_project_at(root, resolve_mode(args))
        cdp, _ = ensure_host(args.port, args.headless, args.software)
        try:
            if not args.no_reload:
                load_host_project(cdp, sb3_path)
        finally:
            cdp.close()
        env = dict(os.environ, GSDEV_PROJECT=str(root))
        artifacts.mkdir(parents=True, exist_ok=True)
        events_path = artifacts / (
            f"{path.stem}-{hashlib.sha1(str(path.resolve()).encode('utf-8')).hexdigest()[:8]}"
            ".events.jsonl")
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "session",
             "--port", str(args.port), "--events", str(events_path)],
            input=path.read_text(encoding="utf-8"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if not JSON_MODE:
            print(output, end="", flush=True)
        events = _read_events(str(events_path))
        records = [e for e in events if e.get("kind") == "assert"]
        if records:
            entry["asserts"] = [{"ok": bool(e.get("ok")), "msg": e.get("msg", "")}
                                for e in records]
        else:
            # No event stream (older session, or none written): fall back to text.
            entry["asserts"] = parse_asserts(output)
        entry["failures"] = sum(1 for item in entry["asserts"] if not item["ok"])
        cdp = open_host(args.port)
        if cdp is None:
            failed = True
        else:
            try:
                info = get_errors(cdp)
                entry["errors"] = int(info.get("count", 0)) + int(info.get("logErrors", 0))
                perf = cdp.evaluate("window.__host.perf()") or {}
                entry["perf"] = {
                    "fps": round(float(perf.get("stepFps") or 0.0), 1),
                    "step_ms": round(float(perf.get("steptimeAvg") or 0.0), 2),
                }
                failed = entry["failures"] > 0 or entry["errors"] > 0 or proc.returncode != 0
                if failed:
                    screenshot = None
                    if not args.no_screenshots:
                        try:
                            screenshot = capture_stage_png(cdp)
                        except Exception:  # noqa: BLE001
                            screenshot = None
                    try:
                        bundle = assemble_bundle(
                            artifacts=artifacts, label=path.stem, events=events,
                            session_path=str(path), project_root=root,
                            sb3_path=sb3_path, screenshot=screenshot,
                            errors=entry["errors"])
                        entry["bundle"] = str(bundle)
                        if screenshot:
                            entry["screenshot"] = str(bundle / "stage.png")
                    except OSError as error:
                        # The original failure must stay the headline.
                        entry["bundle_error"] = str(error)
            finally:
                cdp.close()
        any_failed = any_failed or failed
        report["files"].append(entry)
        report["totals"]["files"] += 1
        report["totals"]["asserts"] += len(entry["asserts"])
        report["totals"]["failures"] += entry["failures"]
        if not JSON_MODE:
            perf = entry.get("perf") or {}
            log(
                f"[{'ok' if not failed else 'FAIL'}] {path}: {len(entry['asserts'])} assert(s), "
                f"{entry['failures']} failure(s), {entry['errors']} error(s)"
                f" | fps={perf.get('fps', 0)} step_ms={perf.get('step_ms', 0)}"
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


def cmd_perf(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        log(json.dumps(cdp.evaluate("window.__host.perf()")))
    finally:
        cdp.close()
    return 0


def cmd_gpu(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(
            "window.__host.gpu ? window.__host.gpu() : {error: 'host has no gpu()'}"
        )
        log(json.dumps(result))
    finally:
        cdp.close()
    return 0


def cmd_setprofiling(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        enabled = args.mode == "on"
        result = cdp.evaluate(f"window.__host.setProfiling({str(enabled).lower()})")
        log(json.dumps({"profiling": bool(result)}))
    finally:
        cdp.close()
    return 0


def cmd_profilereset(args: argparse.Namespace) -> int:
    """Start a fresh profiling window while instrumentation stays on."""
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        ok = bool(cdp.evaluate("window.__host.profileReset()"))
        log(json.dumps({"profileReset": ok}))
        return 0 if ok else 1
    finally:
        cdp.close()


def cmd_profiling(args: argparse.Namespace) -> int:
    """Print the current profiling report without starting a new capture."""
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        report = cdp.evaluate("window.__host.profile()")
        if not isinstance(report, dict) or not report.get("enabled"):
            log("profiling is off: enable with `setprofiling on`, or run `profile`")
            return 1
        state = read_host_state()
        _attach_sources(report, state.get("project"))
        _print_profile(report, args.top, getattr(args, 'sort', 'self'),
                       getattr(args, 'hats_min', 2.0))
        _print_profile_steps(report, args.steps)
        if args.children:
            _print_children(report, args.children, args.top)
        if args.json:
            out = Path(args.json).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
            log(f"wrote {out}")
    finally:
        cdp.close()
    return 0


def _short_proc(name: str) -> str:
    """Elide typed-procedure argument signatures for the table (JSON keeps the key)."""
    idx = name.find(": %s")
    if idx < 0:
        return name
    head = name[:idx]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head) + "..."


def _print_children(report: dict, name: str, top: int) -> None:
    """Print the callees of `name` from the call-graph edges."""
    steps = int(report.get("steps") or 0)
    edges = [e for e in (report.get("edges") or [])
             if name.lower() in str(e.get("caller", "")).lower()]
    if not edges:
        log(f"no callees recorded for {name!r} (is the name in the procedure table?)")
        return
    by_key = {str(p.get("key")): p for p in (report.get("procedures") or [])}
    parent_incl = 0
    parent_self = 0
    for p in (report.get("procedures") or []):
        if name.lower() in str(p.get("key", "")).lower():
            parent_incl = int(p.get("inclusive") or 0)
            parent_self = int(p.get("self") or 0)
            break
    if not parent_incl:
        parent_incl = sum(int(e.get("inclusive") or 0) for e in edges)
    log(f"callees of {name}: (share% is of the caller's inclusive; blocks/step is "
        f"edge-inclusive, i.e. the callee plus everything below it)")
    log(f"{'callee':40} {'share%':>7} {'blocks/step':>12} {'calls':>7} {'calls/step':>10}")
    for edge in sorted(edges, key=lambda e: -(e.get("inclusive") or 0))[: max(0, top)]:
        child = by_key.get(str(edge.get("callee"))) or {}
        calls = int(edge.get("calls") or 0)
        incl_ops = int(edge.get("inclusive") or 0)
        incl = (incl_ops / steps) if steps else 0.0
        share = (100.0 * incl_ops / parent_incl) if parent_incl else 0.0
        label = _short_proc(str(edge.get("callee", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"{label:40} {share:6.1f}% {incl:12.2f} {calls:7d} "
            f"{(calls / steps if steps else 0):10.2f}")
    if parent_self and steps:
        log(f"(caller self: {parent_self / steps:.2f}/step of "
            f"{(parent_incl / steps) if steps else 0:.2f} inclusive)")


def _proc_name_parts(name: str) -> tuple[str, list[str]]:
    """('emit_dashed_row', ['world_y', 'clipped_sx_start']) from a Scratch proccode."""
    proc = ""
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", name or "")
    if match:
        proc = match.group(1)
    params = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*%[sbn]", name or "")
    return proc, params


def _attach_sources(report: dict, project_root) -> None:
    """Map each procedure row to its goboscript source, and record a file manifest.

    A goboscript sprite is one `.gs` file named after the sprite, and procedures are
    declared as `proc <name> ...`, so file and line come from scanning the project
    (the VM keeps no source information).
    """
    rows = report.get("procedures") or []
    for row in rows:
        proc, params = _proc_name_parts(str(row.get("name", "")))
        row["proc"] = proc
        row["params"] = params
        row["kind"] = ("hat" if str(row.get("name", "")).startswith("event_")
                       else ("procedure" if proc else "internal"))
        row["resolved"] = False
    root = Path(str(project_root)) if project_root else None
    if not root or not root.is_dir():
        return
    files: list[dict] = []
    index: dict[str, tuple[str, int]] = {}
    skip = {"vendor", "node_modules", ".tools", ".git", "__pycache__", "debug"}
    for path in sorted(root.rglob("*.gs")):
        rel = path.relative_to(root).as_posix()
        if any(part in skip for part in Path(rel).parts):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        files.append({"file": rel, "lines": len(lines),
                      "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]})
        for number, line in enumerate(lines, 1):
            match = re.match(r"\s*proc\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if match:
                index.setdefault(match.group(1), (rel, number))
        if len(files) >= 200:
            break
    report["sources"] = {"project_dir": str(root), "files": files}
    for row in rows:
        hit = index.get(str(row.get("proc") or ""))
        if hit:
            row["file"], row["line"] = hit
            row["search"] = f'grep -n "proc {row["proc"]}" {hit[0]}'
            row["resolved"] = True
            row["kind"] = "procedure"


def _print_per_frame(report: dict, order: str, top: int) -> None:
    """Per-frame share spread plus the slowest frames and their main contributors."""
    frames = report.get("frames") or []
    ids = report.get("ids") or {}
    if not frames:
        return
    n = len(frames)
    totals = [max(1, int(f.get("blocks") or 0)) for f in frames]
    by_id: dict[int, list[int]] = {}
    for index, frame in enumerate(frames):
        for entry in (frame.get("ops") or []):
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                counts = by_id.setdefault(int(entry[0]), [0] * n)
                counts[index] += int(entry[1])

    def key_for(rid) -> str:
        return str(ids.get(str(rid)) or ids.get(rid) or f"id{rid}")

    def stats(rid: int):
        counts = by_id.get(rid)
        if not counts:
            return None
        shares = [c / t for c, t in zip(counts, totals)]
        if n > 9:
            deciles = statistics.quantiles(shares, n=10)
            lo, hi = deciles[0], deciles[-1]
        else:
            lo, hi = min(shares), max(shares)
        return statistics.median(shares), lo, hi, 100.0 * sum(1 for c in counts if c) / n

    log(f"per-frame share ({n} frames; share of each frame's blocks, active% = frames "
        f"the row appears in):")
    log(f"{'procedure':40} {'median':>8} {'p10-p90':>15} {'active%':>8}")
    ranked = report.get("procedures") or []
    if order in ("total", "incl", "kids"):
        ranked = sorted(ranked, key=lambda r: -(int(r.get("inclusive") or 0)))
    shown = 0
    for row in ranked:
        outcome = stats(int(row.get("id", -1)))
        if not outcome:
            continue
        median, lo, hi, active = outcome
        label = _short_proc(str(row.get("key", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"{label:40} {100 * median:7.1f}% {100 * lo:6.1f}-{100 * hi:5.1f}% "
            f"{active:7.0f}%")
        shown += 1
        if shown >= min(max(top, 5), 10):
            break
    if len(ranked) > shown:
        log(f"... {len(ranked) - shown} more in the JSON (frames matrix + ids table)")

    slowest = sorted(range(n), key=lambda i: -float(frames[i].get("step_ms") or 0))[:3]
    log("slowest frames (instrumented step ms; contributors by share of that frame):")
    for index in slowest:
        frame = frames[index]
        total = max(1, int(frame.get("blocks") or 0))
        parts = []
        for entry in sorted((frame.get("ops") or []), key=lambda e: -int(e[1]))[:3]:
            parts.append(f"{_short_proc(key_for(entry[0])).split(' :: ')[-1][:24]} "
                         f"{100 * int(entry[1]) / total:.0f}%")
        log(f"  frame {frame.get('frame')}: {float(frame.get('step_ms') or 0):7.1f} ms, "
            f"{int(frame.get('blocks') or 0):6d} blocks, "
            f"{int(frame.get('draws') or 0):4d} draws -> " + ", ".join(parts))


def _print_time_share(report: dict, top: int) -> None:
    """Sampled per-procedure time as a share of the profiled total (never ms).

    The clock is read once per adaptive window and credited to whichever identity ran
    during it, split into ui-thread (screen-refresh) and all-at-once (without screen
    refresh). Shares survive the profiler's inflation; the ms totals do not.
    """
    total = float(report.get("time_ms_total") or 0.0)
    samples = int(report.get("time_samples") or 0)
    if not total or not samples:
        return
    ranked = sorted(report.get("procedures") or [],
                    key=lambda r: -float(r.get("time_share") or 0.0))
    log(f"profiled time share ({samples} samples, {total:.0f} ms attributed, "
        f"~{int(report.get('sample_every') or 0)} blocks/sample; instrumented — "
        f"compare shares, not ms):")
    log(f"{'procedure':40} {'time%':>7} {'ui%':>6} {'all%':>6} {'blocks%':>8}")
    shown = 0
    for row in ranked:
        share = float(row.get("time_share") or 0.0)
        if share <= 0:
            continue
        ui = 100.0 * float(row.get("ui_time_ms") or 0.0) / total
        all_at_once = 100.0 * float(row.get("warp_time_ms") or 0.0) / total
        label = _short_proc(str(row.get("key", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"{label:40} {100 * share:6.1f}% {ui:5.1f}% {all_at_once:5.1f}% "
            f"{100.0 * float(row.get('share') or 0.0):7.1f}%")
        shown += 1
        if shown >= min(max(top, 5), 10):
            break
    unattributed = float(report.get("unattributed_time_ms") or 0.0)
    if unattributed:
        log(f"  (unattributed {100.0 * unattributed / total:.1f}% of sampled time)")


def _print_profile(report: dict, top: int, order: str = "self",
                   hats_min: float = 2.0) -> None:
    """Print the project summary and ranked procedure table.

    `order` picks the ranking: `self` (own body work, the default), `total` (whole
    subtree; `incl` is accepted as an alias), or `kids` (subtree below it). Top-level
    scripts (`event_*` hats) are containers, not optimisable code, so they are
    summarised in a footer and only listed when they hold at least `hats_min` percent
    of the counted work.
    """
    steps = int(report.get("steps") or 0)
    all_rows = report.get("procedures") or []
    total_ops = int(report.get("total_ops") or 0)

    def ops_pct(ops: int) -> float:
        return (100.0 * ops / total_ops) if total_ops else 0.0

    def is_hat(row: dict) -> bool:
        return str(row.get("name", "")).startswith("event_")

    def ctx(row: dict) -> str:
        return {"ui-thread": "ui", "all-at-once": "all", "mixed": "mix"}.get(
            str(row.get("context") or ""), "?")

    # Top-level scripts are containers rather than optimisable code, but they are also
    # where a screen-refresh (non-warp) loop lives, so the ones that carry real work are
    # ranked with everything else and only the small ones are summarised in the footer.
    hats = [r for r in all_rows if is_hat(r)]
    big_hats = [h for h in hats if ops_pct(int(h.get("inclusive") or 0)) >= hats_min]
    rows = [r for r in all_rows if not is_hat(r)] + big_hats

    def rank(row: dict) -> int:
        self_ops = int(row.get("self") or 0)
        incl_ops = int(row.get("inclusive") or 0)
        if order in ("total", "incl"):
            return -incl_ops
        if order == "kids":
            return -max(0, incl_ops - self_ops)
        return -self_ops

    rows = sorted(rows, key=rank)
    # self = ops executed with this procedure innermost (its own body only);
    # kids = its descendants; total = self + kids (the grouped subtree cost).
    # All three percentages share one denominator (all counted ops), so they add up
    # and are comparable across rows; blocks/step is the inclusive subtree cost.
    # ctx: ui = screen-refresh (non-warp, cut at the step's work budget),
    #      all = without-screen-refresh (warp), mix = both.
    log(f"steps={steps} | Step ms={float(report.get('step_ms_mean') or 0.0):.2f} | "
        f"Draws/step={float(report.get('draws_per_step') or 0.0):.1f} | "
        f"Blocks/step={float(report.get('ops_per_step_mean') or 0.0):.1f} | "
        f"total={total_ops} self={int(report.get('self_total') or 0)} "
        f"unattributed={int(report.get('unattributed') or 0)}")
    log(f"{'procedure':40} {'total%':>7} {'self%':>7} {'kids%':>7} {'ctx':>4} "
        f"{'blocks/step':>12} {'calls':>7} {'calls/step':>10}")
    shown_rows = sorted(rows[: max(0, top)],
                        key=lambda r: -int(r.get("inclusive") or 0))
    for row in shown_rows:
        calls = int(row.get("calls") or 0)
        calls_per_step = (calls / steps) if steps else 0.0
        self_ops = int(row.get("self") or 0)
        incl_ops = int(row.get("inclusive") or 0)
        kids_ops = max(0, incl_ops - self_ops)
        incl = (incl_ops / steps) if steps else 0.0
        if total_ops:
            self_pct = 100.0 * self_ops / total_ops
            kids_pct = 100.0 * kids_ops / total_ops
            incl_pct = 100.0 * incl_ops / total_ops
        else:
            self_pct = kids_pct = incl_pct = 0.0
        label = _short_proc(str(row.get("key", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"{label:40} {incl_pct:6.1f}% {self_pct:6.1f}% {kids_pct:6.1f}% "
            f"{ctx(row):>4} {incl:12.2f} {calls:7d} {calls_per_step:10.2f}")
    if len(rows) > len(shown_rows):
        log(f"... {len(rows) - len(shown_rows)} more procedure(s) in the JSON report")
    # A top-level script that carries a large share must never be invisible just
    # because its own body is small (hats sort last when ranking by self).
    buried = [h for h in sorted(big_hats, key=lambda r: -int(r.get("inclusive") or 0))
              if h not in rows[: max(0, top)]]
    for hat in buried[:4]:
        label = _short_proc(str(hat.get("key", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"top-level: {label:36} {ops_pct(int(hat.get('self') or 0)):5.1f}% self "
            f"{ops_pct(int(hat.get('inclusive') or 0)):5.1f}% total  {ctx(hat)}")

    def ops_pct(ops: int) -> float:
        return (100.0 * ops / total_ops) if total_ops else 0.0

    small_hats = [h for h in hats if h not in big_hats]
    if small_hats:
        ranked = sorted(small_hats, key=lambda h: -int(h.get("inclusive") or 0))
        log(f"({len(ranked)} top-level script(s) below {hats_min:g}% total omitted, "
            f"largest: {_short_proc(str(ranked[0].get('key', '?')))})")

    # Screen-refresh (non-warp) work that is still running when the step's work budget
    # runs out is what starves a frame; blocks/step cannot show it because the loop is
    # cut at the budget, so call it out explicitly.
    budget_steps = int(report.get("budget_steps") or 0)
    work_time = float(report.get("work_time_ms") or 0.0)
    if budget_steps and steps:
        log(f"screen-refresh budget: {budget_steps}/{steps} steps used >=90% of the "
            f"{work_time:.1f} ms work budget")
        flagged = [r for r in all_rows
                   if float(r.get("budget_pct") or 0) >= 50.0
                   and str(r.get("context")) in ("ui-thread", "mixed")
                   and ops_pct(int(r.get("self") or 0)) >= 5.0]
        for row in sorted(flagged, key=lambda r: -float(r.get("budget_pct") or 0))[:6]:
            label = _short_proc(str(row.get("key", "?")))
            if len(label) > 40:
                label = label[:37] + "..."
            log(f"  ! {label:38} held {float(row.get('budget_pct') or 0):5.0f}% of "
                f"budget-bound steps  {(int(row.get('self') or 0) / steps):9.0f} "
                f"blocks/step  {ctx(row)}")
        if not flagged:
            log("  (no single screen-refresh script dominated those steps)")

    _print_per_frame(report, order, top)
    _print_time_share(report, top)


def _print_profile_steps(report: dict, count: int) -> None:
    """Print the last N per-step lines from the in-page series."""
    series = report.get("step_series") or []
    if not series or count <= 0:
        return
    tail = series[-count:]
    base = len(series) - len(tail)
    prev_t = float(series[base - 1].get("t") or 0.0) if base > 0 else None
    for row in tail:
        t = float(row.get("t") or 0.0)
        rate = (1000.0 / (t - prev_t)) if (prev_t and t > prev_t) else 0.0
        if t:
            prev_t = t
        log(f"frame {int(row.get('frame') or 0)} | VM steps/s={rate:.1f} | "
            f"Step ms={float(row.get('step_ms') or 0.0):.3f} | "
            f"Draws/step={float(row.get('draws') or 0.0):.0f} | "
            f"Blocks/step={int(row.get('blocks') or 0)}")


def _follow_profile(cdp: CDP, args: argparse.Namespace) -> None:
    """Stream one line per completed VM step while the capture window runs."""
    deadline = time.monotonic() + max(0.0, args.seconds)
    seen = 0
    prev_t: float | None = None
    while time.monotonic() < deadline:
        try:
            batch = cdp.evaluate(f"window.__host.profileSteps({seen})") or {}
        except (CDPError, TimeoutError, OSError):
            break
        base = int(batch.get("series_start") or 0)
        for i, row in enumerate(batch.get("steps") or []):
            seen = base + i + 1
            t = float(row.get("t") or 0.0)
            rate = (1000.0 / (t - prev_t)) if (prev_t and t > prev_t) else 0.0
            if t:
                prev_t = t
            log(f"frame {int(row.get('frame') or 0)} | VM steps/s={rate:.1f} | "
                f"Step ms={float(row.get('step_ms') or 0.0):.3f} | "
                f"Draws/step={float(row.get('draws') or 0.0):.0f} | "
                f"Blocks/step={int(row.get('blocks') or 0)}")
        time.sleep(max(0.02, args.follow_interval))


def cmd_profile(args: argparse.Namespace) -> int:
    """Capture a bounded profiling window and print the procedure hotspot report.

    Counts first, attribution second (plan-procedure-hotspots.md): this prints the
    project summary and ranked procedure table; it does not measure per-opcode cost.
    """
    ensure_dependencies(need_bundles=True)
    if not args.no_build and not args.no_reload:
        build_project(resolve_mode(args))
    cdp, launched = ensure_host(args.port, args.headless, args.software)
    dismiss_browser_dialogs(args.port)
    apply_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
    try:
        if args.no_reload:
            log("reusing the running project (--no-reload)")
        else:
            info = load_host_project(cdp, SB3_PATH)
            log(f"loaded in {info['load']:.0f} ms")
        cdp.evaluate("window.__host.setProfiling(true)")
        if args.warmup > 0:
            time.sleep(args.warmup)
        if args.wait_until:
            # Event-gated capture: run the scenario, wait for the start event, then
            # open the window, so loading/setup work is excluded.
            if not args.no_restart:
                cdp.evaluate("window.__host.restart()")
            spec = _spec(args.wait_until)
            value = parse_value(args.wait_value)
            script = (f"window.__host.waitUntil({json.dumps(spec)}, "
                      f"{json.dumps(args.wait_op)}, {json.dumps(value)}, "
                      f"{int(args.wait_timeout)})")
            result = cdp.evaluate(script, await_promise=True,
                                  timeout=args.wait_timeout / 1000.0 + 10) or {}
            if not result.get("ok"):
                log(f"wait-until {args.wait_until} {args.wait_op} {args.wait_value} "
                    f"failed: {result.get('error') or 'timed out'}")
                return 1
            log(f"event: {args.wait_until} {args.wait_op} {args.wait_value} "
                f"at frame {result.get('frame')}")
            cdp.evaluate("window.__host.profileReset()")
        else:
            cdp.evaluate("window.__host.profileReset()")
            if not args.no_restart:
                # Start the scenario inside the capture window so one-shot setup
                # work is attributed too (instrument before scenario start).
                cdp.evaluate("window.__host.restart()")
        if args.follow:
            _follow_profile(cdp, args)
        else:
            time.sleep(max(0.0, args.seconds))
        report = cdp.evaluate("window.__host.profile()")
        if not isinstance(report, dict):
            log("no profiling report (is the host page up to date?)")
            return 1
        _attach_sources(report, PROJECT_ROOT)
        _print_profile(report, args.top, getattr(args, 'sort', 'self'),
                       getattr(args, 'hats_min', 2.0))
        if args.children:
            _print_children(report, args.children, args.top)
        if args.json:
            out = Path(args.json).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
            log(f"wrote {out}")
        if not args.leave_running:
            cdp.evaluate("window.__host.setProfiling(false)")
    finally:
        if not args.leave_running:
            try:
                stop_host(cdp)
            except (CDPError, TimeoutError, OSError):
                pass
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
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15)
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

    goboscript = resolve_goboscript()
    if goboscript:
        detail = _tool_version([goboscript, "--version"])
        bundled = bootstrap.bundled_goboscript()
        is_bundled = bundled is not None and Path(goboscript).resolve() == bundled.resolve()
        if detail:
            ok("goboscript", f"{detail} ({'bundled' if is_bundled else 'system'})")
        else:
            # A resolved path that cannot run is a broken install; don't report ok.
            fail("goboscript", f"{goboscript} is not runnable; {_setup_hint()}")
    else:
        fail("goboscript", f"not found; {_setup_hint()} (or install from github.com/aspizu/goboscript)")

    if host_bundles_present():
        where = BUNDLES_DIR if bootstrap.vendor_present() else (HOST_DIR / "node_modules")
        ok("host bundles", str(where))
    else:
        fail("host bundles", f"not installed; {_setup_hint()}")

    # node/npm are only the advanced way to fetch the host bundles; once the
    # bundles are present (vendored by setup, or via npm) they are optional.
    if host_bundles_present():
        if shutil.which("node") or shutil.which("npm"):
            ok("node/npm", "present (optional)")
    else:
        node = shutil.which("node")
        if node:
            ok("node", _tool_version([node, "--version"]) or node)
        else:
            warn("node", "not on PATH")
        npm = shutil.which("npm")
        if npm:
            ok("npm", _tool_version([npm, "--version"]) or npm)
        else:
            warn("npm", "not on PATH")

    browser = find_browser()
    if browser:
        ok("browser", browser)
    else:
        fail("browser", "no Chrome/Edge found; set GSDEV_BROWSER to the executable")

    if port_open(HOST_SERVER_PORT):
        info = probe_host_server()
        if info is not None:
            ok("host server", f"http://127.0.0.1:{HOST_SERVER_PORT} (pid {info.get('pid')})")
        else:
            warn("host port", f"port {HOST_SERVER_PORT} is used by another application; "
                              f"set GSDEV_HOST_PORT to a free port")
    else:
        ok("host server", "not running (started on demand)")

    missing_includes = _missing_includes(PROJECT_ROOT)
    if missing_includes:
        preview = ", ".join(missing_includes[:3]) + ("..." if len(missing_includes) > 3 else "")
        if _prebuild_argv():
            ok("build readiness", f"{len(missing_includes)} include(s) generated by prebuild")
        else:
            warn("build readiness",
                 f"{len(missing_includes)} unresolved %include path(s), no prebuild: {preview}")
    else:
        ok("build readiness", "all %include paths resolve")

    system_sb2gs = bootstrap.system_sb2gs()
    if system_sb2gs:
        ok("sb2gs", f"{system_sb2gs} (system)")
    elif bootstrap.sb2gs_present():
        ok("sb2gs", str(bootstrap.sb2gs_source_dir()))
    elif sys.version_info[:2] < (3, 14):
        warn("sb2gs", f"needs Python 3.14+ (found {platform.python_version()})")
    else:
        fail("sb2gs", f"not installed; {_setup_hint()}")

    print("", flush=True)
    if failures:
        print(f"[FAIL] doctor - {failures} required dependency(ies) missing", flush=True)
    elif warnings:
        print(f"[ok  ] doctor - no failures ({warnings} warning(s))", flush=True)
    else:
        print("[ok  ] doctor - all dependencies found", flush=True)
    return 1 if failures else 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Download the prebuilt tools (goboscript + host bundles + sb2gs), no admin."""
    return bootstrap.run_setup(only=args.only, force=args.force, offline=args.offline)


def _sb2gs_paths(argv: list[str]) -> tuple[Path | None, Path | None]:
    """Input `.sb3` and output directory from sb2gs's positional args."""
    positionals: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg == "--id":
            skip = True
            continue
        if arg.startswith("-"):
            continue
        positionals.append(arg)
    if not positionals:
        return None, None
    input_path = Path(positionals[0])
    output_path = (
        Path(positionals[1]) if len(positionals) > 1
        else input_path.parent / input_path.stem
    )
    return input_path, output_path


def _clean_sb2gs_failure(
    input_path: Path | None, output_path: Path | None,
    input_existed: bool, output_existed: bool, verify: bool = False,
) -> None:
    """Remove artifacts this failed run created, so a retry starts clean.

    A partial `.sb3` would make sb2gs skip the download and then fail on a
    corrupt archive; a partial output dir makes it refuse ("use --overwrite").
    Only artifacts that did not exist before this run are removed, and a valid
    `.sb3` is kept so a retry skips the download.

    With ``--verify`` a *complete* decompile (its config was written) is kept for
    inspection even though goboscript rejected it; a partial decompile is removed.
    """
    import zipfile

    if (input_path is not None and not input_existed and input_path.exists()
            and not zipfile.is_zipfile(input_path)):
        try:
            input_path.unlink()
            log(f"removed incomplete download: {input_path}")
        except OSError:
            pass
    config = output_path / "goboscript.toml" if output_path is not None else None
    decompiled = bool(config is not None and config.is_file() and config.stat().st_size > 0)
    if (output_path is not None and not output_existed
            and not (verify and decompiled) and output_path.is_dir()):
        try:
            shutil.rmtree(output_path)
            log(f"removed incomplete output: {output_path}")
        except OSError:
            pass


def _sb2gs_project_id(argv: list[str]) -> str | None:
    """The `--id` project id, if passed (`--id N` or `--id=N`)."""
    for index, arg in enumerate(argv):
        if arg == "--id" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--id="):
            return arg[len("--id="):]
    return None


def _write_provenance(project_id: str, output_dir: Path) -> None:
    """Write provenance.json for a Scratch project imported with `--id`.

    Best-effort: a metadata fetch failure warns but never fails the import, and
    the file is only meaningful for projects hosting their source on Scratch.
    """
    request = urllib.request.Request(
        f"https://api.scratch.mit.edu/projects/{project_id}",
        headers={"User-Agent": "gobo-agent"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as error:
        log(f"provenance: could not fetch metadata for {project_id}: {error}")
        return
    history = data.get("history") or {}
    author = data.get("author") or {}
    username = author.get("username")
    canonical_id = data.get("id", project_id)
    record = {
        "title": data.get("title"),
        "username": username,
        "projectid": canonical_id,
        "releasedate": history.get("shared"),
        "instructions": data.get("instructions"),
        "notes_and_credits": data.get("description"),
        "url": f"https://scratch.mit.edu/projects/{canonical_id}",
        "author_url": f"https://scratch.mit.edu/users/{username}/" if username else None,
        "retrieved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "provenance.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8", newline="\n",
        )
    except OSError as error:
        log(f"provenance: could not write provenance.json: {error}")
        return
    log(f"provenance: wrote {output_dir / 'provenance.json'}")


def _run_sb2gs(argv: list[str]) -> int:
    """Run a user-installed sb2gs if present, else install ours (only if missing)."""
    input_path, output_path = _sb2gs_paths(argv)
    verify = "--verify" in argv
    # An existing but invalid .sb3 (a partial download from a previous failure)
    # would make sb2gs skip the download and fail on decompile; drop it first.
    if "--id" in argv and input_path is not None and input_path.exists():
        import zipfile
        if not zipfile.is_zipfile(input_path):
            try:
                input_path.unlink()
                log(f"removed incomplete download: {input_path}")
            except OSError:
                pass
    input_existed = input_path.exists() if input_path is not None else True
    output_existed = output_path.exists() if output_path is not None else True

    try:
        timeout = float(os.environ.get("GSDEV_SB2GS_TIMEOUT", "1800"))
    except ValueError:
        raise SystemExit("GSDEV_SB2GS_TIMEOUT is not a number")

    system = bootstrap.system_sb2gs()
    try:
        if system:
            code = subprocess.run(
                [system, *argv], timeout=timeout, env=bootstrap.sb2gs_env()
            ).returncode
        else:
            if not bootstrap.sb2gs_present():
                if os.environ.get("GSDEV_NO_AUTO_SETUP", "").strip().lower() in ("1", "true", "yes"):
                    raise SystemExit("sb2gs is not installed; run `gsdev setup --only sb2gs`")
                log("sb2gs not installed; downloading it (no admin)")
                bootstrap.run_setup(only="sb2gs")
            code = bootstrap.run_sb2gs(argv)
    except subprocess.TimeoutExpired:
        _clean_sb2gs_failure(input_path, output_path, input_existed, output_existed, verify=verify)
        raise SystemExit(
            f"sb2gs timed out after {int(timeout)}s (slow/stalled network?); "
            "download the .sb3 in a browser and pass the local file instead"
        )
    except bootstrap.SetupError as error:
        _clean_sb2gs_failure(input_path, output_path, input_existed, output_existed, verify=verify)
        raise SystemExit(str(error))

    if code != 0:
        _clean_sb2gs_failure(input_path, output_path, input_existed, output_existed, verify=verify)
    else:
        project_id = _sb2gs_project_id(argv)
        if project_id and output_path is not None and output_path.is_dir():
            _write_provenance(project_id, output_path)
    return code


def cmd_sb2gs(args: argparse.Namespace) -> int:
    """Run the bundled sb2gs Scratch importer with the given arguments."""
    return _run_sb2gs(list(args.sb2gs_args))


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
        "bundle entry table",
        set(bootstrap.ENTRY_FILES) == set(bootstrap.PACKAGES),
        f"{len(bootstrap.PACKAGES)} pinned packages",
    )
    asset_cases = {
        ("Windows", "AMD64"): "goboscript_Windows_x86_64.zip",
        ("Windows", "ARM64"): "goboscript_Windows_x86_64.zip",
        ("Darwin", "arm64"): "goboscript_Darwin_arm64.tar.gz",
        ("Darwin", "x86_64"): "goboscript_Darwin_x86_64.tar.gz",
        ("Linux", "aarch64"): "goboscript_Linux_arm64.tar.gz",
        ("Linux", "x86_64"): "goboscript_Linux_x86_64.tar.gz",
    }
    check(
        "goboscript asset mapping",
        all(
            bootstrap._goboscript_asset(system, machine)[0] == expected
            for (system, machine), expected in asset_cases.items()
        ),
        "win/mac/linux x64+arm64",
    )
    get_python = TOOLS_DIR / "get-python.ps1"
    python_text = get_python.read_text(encoding="utf-8").lower() if get_python.exists() else ""
    check(
        "portable Python arches",
        "arm64" in python_text and "amd64" in python_text,
        "x64 + arm64 embed zips",
    )
    check(
        "sb2gs pins",
        len(bootstrap.SB2GS_WHEELS) >= 10
        and "win_amd64" in bootstrap.SB2GS_PILLOW
        and bool(bootstrap.SB2GS_ZIP_SHA256),
        f"{len(bootstrap.SB2GS_WHEELS)} wheels + pillow per platform",
    )
    host_html = (HOST_DIR / "host.html")
    html_text = host_html.read_text(encoding="utf-8") if host_html.exists() else ""
    check(
        "host page resolves bundles",
        "./vendor/@scratch" in html_text and "./node_modules/@scratch" in html_text,
        "vendor first, node_modules fallback",
    )
    check(
        "browser candidates",
        bool(BROWSER_CANDIDATES.get("win32")) and bool(BROWSER_CANDIDATES.get("darwin")),
        "chrome/edge paths",
    )
    check("host port", isinstance(HOST_SERVER_PORT, int) and HOST_SERVER_PORT > 0, str(HOST_SERVER_PORT))
    check(
        "per-port host profile",
        host_profile_dir(9240) != host_profile_dir(9241)
        and host_profile_dir(9240).parent == HOST_PROFILE_ROOT,
        str(HOST_PROFILE_ROOT),
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


def _task_entrypoint(command: str, arguments: list) -> tuple[str, str]:
    """(script, subcommand) for a task, across the launcher shapes.

    POSIX tasks go through the shell launchers: ``<repo>/tools/gsdev <subcommand>``
    and ``<repo>/setup.sh``. Windows tasks go through the PowerShell launchers:
    ``powershell -File <script.ps1> <subcommand>``, where ``setup.ps1`` has no
    subcommand (installing is its whole job). Plain Python tasks are
    ``python <script.py> <subcommand>``.
    """
    name = Path(command or "").name.lower()
    if name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        positions = [i for i, arg in enumerate(arguments) if str(arg).lower() == "-file"]
        if positions:
            index = positions[0]
            script = arguments[index + 1] if len(arguments) > index + 1 else ""
            subcommand = arguments[index + 2] if len(arguments) > index + 2 else ""
            if script and Path(script).name.lower() == "setup.ps1":
                subcommand = "setup"
            return script, subcommand
        return "", ""
    if name == "gsdev":
        # POSIX launcher: the shim resolves Python itself, so the first argument
        # is the subcommand (the Windows path is gsdev.ps1 above).
        return command, (arguments[0] if arguments else "")
    if name == "setup.sh":
        return command, "setup"
    script = arguments[0] if arguments else ""
    subcommand = arguments[1] if len(arguments) > 1 else ""
    return script, subcommand


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
    known = {"build", "status", "doctor", "selftest", "setup", "run", "screenshot",
             "test", "stop", "close"}
    runnable = {"build", "doctor", "stop", "close"}
    labels = {task.get("label", "<unlabeled>") for task in tasks}
    for task in tasks:
        label = task.get("label", "<unlabeled>")
        command = _task_substitute(_task_field(task, "command", platform_key), PROJECT_ROOT)
        arguments = _task_substitute(_task_field(task, "args", platform_key, []), PROJECT_ROOT)
        options = _task_substitute(_task_field(task, "options", platform_key, {}), PROJECT_ROOT)
        resolved = shutil.which(command) if command else None
        script, subcommand = _task_entrypoint(command, arguments)
        cwd = options.get("cwd") or str(PROJECT_ROOT)
        depends = task.get("dependsOn") or []
        if isinstance(depends, str):
            depends = [depends]
        reasons = []
        if not resolved:
            reasons.append(f"command {command!r} not on PATH")
        if script and not Path(script).exists():
            reasons.append(f"script {script!r} missing")
        if subcommand not in known:
            reasons.append(f"unknown subcommand {subcommand!r}")
        missing = [name for name in depends if name not in labels]
        if missing:
            reasons.append(f"unknown dependsOn {missing!r}")
        if not Path(cwd).is_dir():
            reasons.append(f"cwd {cwd!r} missing")
        check(label, not reasons, "; ".join(reasons) or f"{subcommand} via {command}")

        if args.run and subcommand in runnable and resolved:
            env = dict(os.environ)
            env.update(options.get("env") or {})
            result = subprocess.run(
                [resolved, *arguments], cwd=cwd, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            check(f"{label} (executed)", result.returncode == 0, f"exit {result.returncode}")

    if failures:
        print(f"[FAIL] tasks - {failures} check(s) failed", flush=True)
    else:
        suffix = " and ran" if args.run else ""
        print(f"[ok  ] tasks - all .vscode tasks resolve{suffix}", flush=True)
    return 1 if failures else 0


# --- gsdev init (adopt) -------------------------------------------------------
#
# Adopts an existing project into a single namespaced `gobo-agent/` folder,
# non-destructively and idempotently. Project-owned files (config, manifest,
# smoke, mode include) are created once and never overwritten without
# --force-owned; generated files (the runtime copy, VERSION, .install.json) may
# be refreshed with --force. An existing gobo-agent/ that is not a recognized
# install is refused, never merged into.

INIT_SCHEMA = 1
INSTALL_DIRNAME = "gobo-agent"
_GITATTRIBUTES_RULES = (
    "# gobo-agent: keep the POSIX launchers LF so they run after a Windows checkout",
    "gobo-agent/runtime/tools/gsdev text eol=lf",
    "gobo-agent/runtime/setup.sh text eol=lf",
)
_GITIGNORE_RULES = (
    "# gobo-agent",
    "/gobo-agent/state/",
    "/gobo-agent/runtime/.tools/",
    "/gobo-agent/runtime/tools/scratchhost/vendor/",
    "/gobo-agent/runtime/tools/scratchhost/node_modules/",
    "/gobo-agent/runtime/tools/scratchvm-profiles/",
    "*.sb3",
    "debug/",
    "__pycache__/",
)
_SMOKE_TEMPLATE = (
    "# gobo-agent smoke: loads, runs, renders, and reports no errors.\n"
    "waitframe 6\n"
    "expect @stepfps > 0\n"
    "expect @rendered > 0\n"
    "expect_no_errors\n"
)

# `init` merges this into the project's root AGENTS.md (creating the file if
# needed) so an agentic coder in the adopted project knows the harness is there
# and how to drive it. The markers let re-runs refresh only our section.
_AGENTS_BEGIN = "<!-- gobo-agent:begin (managed by `gsdev init`) -->"
_AGENTS_END = "<!-- gobo-agent:end -->"
_AGENTS_REPO = "https://github.com/slyi/gobo-agent"
_AGENTS_SECTION = f"""\
## gobo-agent

This project is built, run, and tested with **gobo-agent**, a warm-browser GoboScript
harness under `gobo-agent/`. The launcher is `gobo-agent/runtime/tools/gsdev` (POSIX)
or `gobo-agent/runtime/tools/gsdev.ps1` (Windows); below, `gsdev` means that. Run
commands from the project root.

Full harness workflow (the gobo-agent `AGENTS.md`):
{_AGENTS_REPO}/blob/main/AGENTS.md

The first command installs the pinned tools (goboscript, Scratch host bundles, sb2gs)
with no admin rights and no Rust; they are shared per user, so later projects download
nothing.

Core loop:

- `gsdev doctor` — check python, goboscript, host bundles, sb2gs, build readiness.
- `gsdev build` — compile to `<project>.sb3` at the project root.
- `gsdev run --duration 2 --leave-running` — build and run in a warm browser with live
  logs; then `get <sprite.variable>` / `inspect` / `screenshot`, and end with `close`.
- `gsdev test --manifest gobo-agent/gobo-tests.json --headless` — run the manifest's
  sessions (`--list` validates read-only).
- `gsdev set <sprite.var>=<value>` / `set_batch ...` — live edits, no rebuild.
- `--mode debug|release`; the mode include is `gobo-agent/gsdev_mode.gs`.

Keep the host warm between edits: rebuild/reload after `.gs` changes, but prefer
`set`/`set_batch` for data. `gobo-agent/gobo-agent.json` can set `prebuild`, `env`,
`modeInclude`, `hostPort`/`cdpPort`. Prefer a local `.sb3` over `sb2gs --id` for large
projects.

VS Code: **Ctrl+Shift+B** runs the `gobo-agent: Run` task; the optional Test Explorer
extension lists the manifest. The upstream `aspizu.goboscript` extension adds syntax
and a `.sb3` preview but has no debugger (don't press F5 — build with gobo-agent, then
open the built `.sb3`; set `goboscript.compilerPath` to
`<tools-root>/goboscript/goboscript.exe`).

Scratch import (`sb2gs`) is best-effort: unsupported opcodes and sprite names that
aren't valid filenames can fail. See the gobo-agent repository's integration guide
({_AGENTS_REPO}/blob/main/docs/integration.md) → "Known upstream limitations".
"""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments (outside strings) and trailing commas."""
    out = []
    i = 0
    n = len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_jsonc(path: Path):
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(_strip_jsonc(raw))


def _runtime_manifest() -> list[str]:
    lines = []
    for line in (TOOLS_DIR / "runtime-files.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _git_capture(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _config_template() -> bytes:
    return (json.dumps({
        "schema": 1,
        "project": "..",
        "harness": "runtime",
        "modeInclude": "gobo-agent/gsdev_mode.gs",
    }, indent=2) + "\n").encode("utf-8")


def _manifest_template() -> bytes:
    return (json.dumps({
        "schema": 1,
        "tests": [{"id": "smoke", "label": "smoke",
                   "session": "gobo-agent/smoke.txt", "root": "."}],
    }, indent=2) + "\n").encode("utf-8")


def _adopted_tasks() -> list[dict]:
    rt = "${workspaceFolder}/gobo-agent/runtime"
    manifest = "${workspaceFolder}/gobo-agent/gobo-tests.json"
    setup_label = "gobo-agent: Setup (download prebuilt tools, no admin)"

    def task(label, args, *, depends=False, group=None, extra=None):
        entry = {
            "label": label,
            "type": "process",
            "command": f"{rt}/tools/gsdev",
            "args": args,
            "windows": {
                "command": "powershell",
                "args": ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                         f"{rt}/tools/gsdev.ps1", *args],
            },
            "options": {"cwd": "${workspaceFolder}"},
            "problemMatcher": [],
        }
        if depends:
            entry["dependsOn"] = [setup_label]
            entry["dependsOrder"] = "sequence"
        if group is not None:
            entry["group"] = group
        if extra:
            entry.update(extra)
        return entry

    setup = {
        "label": setup_label,
        "type": "process",
        "command": f"{rt}/setup.sh",
        "args": [],
        "windows": {
            "command": "powershell",
            "args": ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", f"{rt}/setup.ps1"],
        },
        "options": {"cwd": "${workspaceFolder}"},
        "problemMatcher": [],
        "presentation": {"reveal": "always", "panel": "dedicated", "clear": True},
    }
    shown = {"reveal": "always", "panel": "dedicated", "clear": True}
    return [
        setup,
        task("gobo-agent: Run (web, build + live logs)", ["run"], depends=True,
             group={"kind": "build", "isDefault": True},
             extra={"presentation": shown, "runOptions": {"instanceLimit": 1}}),
        task("gobo-agent: Build", ["build"], depends=True, group="build"),
        task("gobo-agent: Screenshot",
             ["screenshot", "--out", "${workspaceFolder}/debug/check.png"], depends=True,
             extra={"presentation": shown, "runOptions": {"instanceLimit": 1}}),
        task("gobo-agent: Run Tests", ["test", "--manifest", manifest], depends=True,
             group={"kind": "test", "isDefault": True},
             extra={"presentation": shown, "runOptions": {"instanceLimit": 1}}),
        task("gobo-agent: Stop project", ["stop"]),
        task("gobo-agent: Close host", ["close"]),
        task("gobo-agent: Check dependencies", ["doctor"],
             extra={"presentation": shown}),
    ]


def cmd_init(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise SystemExit(f"--project {project} is not a directory")
    harness = Path(args.harness).expanduser().resolve() if args.harness else HARNESS_ROOT
    if not (harness / "tools" / "gsdev.py").is_file():
        raise SystemExit(f"--harness {harness} is not a gobo-agent checkout")
    if (project / "tools" / "gsdev.py").is_file():
        raise SystemExit(
            f"{project} already looks like a gobo-agent checkout (tools/gsdev.py); "
            "nothing to adopt."
        )

    install = project / INSTALL_DIRNAME
    marker_path = install / ".install.json"
    version_path = install / "VERSION"
    marker = None
    if marker_path.is_file():
        try:
            marker = _load_jsonc(marker_path)
        except (OSError, ValueError):
            marker = None
    recognized = (isinstance(marker, dict) and marker.get("schema") == INIT_SCHEMA
                  and version_path.is_file())
    if install.exists() and not recognized:
        raise SystemExit(
            f"{install} exists but is not a recognized gobo-agent install "
            "(missing/invalid VERSION + .install.json); refusing to modify it. "
            "Move it aside, then re-run init."
        )
    created: dict = marker.get("created", {}) if recognized else {}
    if not isinstance(created, dict):
        created = {}

    commit = _git_capture(["rev-parse", "HEAD"], harness) or "unknown"
    version_text = (
        "gobo-agent integration\n"
        f"schema: {INIT_SCHEMA}\n"
        f"layout: {args.layout}\n"
        f"harness: {harness}\n"
        f"commit: {commit}\n"
        f"fingerprint: {HARNESS_FINGERPRINT}\n"
    ).encode("utf-8")

    planned: list[tuple[str, Path, bytes, str, bool]] = []
    conflicts: list[str] = []

    def consider(rel: str, data: bytes, *, owned: bool) -> None:
        path = project / rel
        if not path.exists():
            planned.append(("create", path, data, "", owned))
            return
        current = path.read_bytes()
        if current == data:
            planned.append(("unchanged", path, data, "", owned))
            return
        recorded = created.get(rel)
        if recorded is not None and _sha256_bytes(current) == recorded:
            if (not owned) or args.force_owned:
                planned.append(("refresh", path, data, "", owned))
            else:
                planned.append(("kept", path, data, "", owned))
            return
        if owned and args.force_owned:
            planned.append(("overwrite --force-owned", path, data, "", owned))
        elif (not owned) and args.force:
            planned.append(("overwrite --force", path, data, "", owned))
        else:
            conflicts.append(rel)
            planned.append(("kept (modified)", path, b"", "", owned))

    owned_files = {
        "gobo-agent/gobo-agent.json": _config_template(),
        "gobo-agent/gobo-tests.json": _manifest_template(),
        "gobo-agent/smoke.txt": _SMOKE_TEMPLATE.encode("utf-8"),
        "gobo-agent/gsdev_mode.gs": (TOOLS_DIR / "gsdev_mode.gs").read_bytes(),
    }
    for rel in sorted(owned_files):
        consider(rel, owned_files[rel], owned=True)

    generated_files: dict[str, bytes] = {"gobo-agent/VERSION": version_text}
    if args.layout == "copy":
        for rel in _runtime_manifest():
            src = harness / rel
            if src.is_file():
                generated_files[f"gobo-agent/runtime/{rel}"] = src.read_bytes()
            else:
                conflicts.append(f"missing in harness: {rel}")
    for rel in sorted(generated_files):
        consider(rel, generated_files[rel], owned=False)

    # .gitignore additions
    gi_path = project / ".gitignore"
    gi_existing = gi_path.read_text(encoding="utf-8").splitlines() if gi_path.is_file() else []
    gi_missing = [line for line in _GITIGNORE_RULES if line not in gi_existing]

    # .gitattributes additions (copy layout: the runtime ships with the project, so
    # its POSIX launchers must stay LF; a submodule carries its own .gitattributes).
    ga_path = project / ".gitattributes"
    ga_existing = ga_path.read_text(encoding="utf-8").splitlines() if ga_path.is_file() else []
    ga_missing = ([line for line in _GITATTRIBUTES_RULES if line not in ga_existing]
                  if args.layout == "copy" else [])

    # .vscode/tasks.json merge
    tasks_path = project / ".vscode" / "tasks.json"
    tasks_existing = _load_jsonc(tasks_path) if tasks_path.is_file() else None
    if tasks_existing is not None and not isinstance(tasks_existing, dict):
        raise SystemExit(f"{tasks_path}: top level must be an object")
    generated_tasks = _adopted_tasks()
    generated_labels = {t["label"] for t in generated_tasks}
    base_tasks = []
    if isinstance(tasks_existing, dict) and isinstance(tasks_existing.get("tasks"), list):
        base_tasks = [t for t in tasks_existing["tasks"]
                      if not (isinstance(t, dict) and t.get("label") in generated_labels)]
    merged_tasks = {
        "version": (tasks_existing or {}).get("version", "2.0.0") if isinstance(tasks_existing, dict) else "2.0.0",
        "tasks": base_tasks + generated_tasks,
    }
    tasks_changed = not (
        isinstance(tasks_existing, dict)
        and tasks_existing.get("tasks") == merged_tasks["tasks"]
        and tasks_existing.get("version") == merged_tasks["version"]
    )

    # AGENTS.md: hand an agentic coder in the adopted project the gobo-agent
    # workflow. Created if absent; if the file exists only our marked section is
    # (re)written, so the project's own instructions are preserved.
    agents_path = project / "AGENTS.md"
    agents_block = f"{_AGENTS_BEGIN}\n{_AGENTS_SECTION}{_AGENTS_END}\n"
    agents_marker = re.compile(
        re.escape(_AGENTS_BEGIN) + r".*?" + re.escape(_AGENTS_END) + r"\n?", re.DOTALL
    )
    agents_existing = ""
    agents_skip = False
    if agents_path.is_file():
        try:
            agents_existing = agents_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Free-form user file in another encoding: leave it untouched instead
            # of crashing or mangling it.
            agents_skip = True
    if agents_skip:
        agents_merged = ""
        agents_changed = False
    else:
        if agents_marker.search(agents_existing):
            agents_merged = agents_marker.sub(lambda _m: agents_block, agents_existing, count=1)
        elif agents_existing.strip():
            agents_merged = agents_existing.rstrip("\n") + "\n\n" + agents_block
        else:
            agents_merged = "# AGENTS.md\n\n" + agents_block
        agents_changed = agents_merged != agents_existing

    writes = [p for p in planned if p[0] in ("create", "refresh", "overwrite --force",
                                             "overwrite --force-owned")]

    if args.dry_run:
        print(f"[init] project: {project}")
        print(f"[init] harness: {harness} (commit {commit})")
        print(f"[init] layout:  {args.layout}")
        for op, path, data, _, owned in sorted(planned, key=lambda p: str(p[1])):
            rel = path.relative_to(project)
            kind = "owned" if owned else "generated"
            print(f"  {op:>20}  {rel}  ({kind}, {len(data)} bytes)")
        if gi_missing:
            print(f"  {'append':>20}  .gitignore  (+{len(gi_missing)} rules)")
        if ga_missing:
            print(f"  {'append':>20}  .gitattributes  (+{len(ga_missing)} rules)")
        if tasks_changed:
            print(f"  {'merge':>20}  .vscode/tasks.json  (+{len(generated_tasks)} tasks)")
        if agents_changed:
            print(f"  {'merge':>20}  AGENTS.md  (gobo-agent section)")
        if agents_skip:
            print(f"  {'skip':>20}  AGENTS.md  (not UTF-8; left untouched)")
        if conflicts:
            print(f"[init] conflicts (left untouched): {', '.join(sorted(set(conflicts)))}")
        print("[init] dry run: nothing written")
        return 0

    for op, path, data, _, _owned in planned:
        if not data:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if gi_missing:
        prefix = "" if (not gi_path.is_file() or gi_path.read_text(encoding="utf-8").endswith("\n")) else "\n"
        with gi_path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + "\n".join(gi_missing) + "\n")
    if ga_missing:
        prefix = "" if (not ga_path.is_file() or ga_path.read_text(encoding="utf-8").endswith("\n")) else "\n"
        with ga_path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + "\n".join(ga_missing) + "\n")
    if tasks_changed:
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        tasks_path.write_text(json.dumps(merged_tasks, indent=4) + "\n", encoding="utf-8")
    if agents_changed:
        agents_path.write_text(agents_merged, encoding="utf-8")

    # Make the copied runtime launchers executable on POSIX (preserve other modes).
    if args.layout == "copy" and os.name != "nt":
        exec_launchers = {"tools/gsdev", "setup.sh"}
        for rel in _runtime_manifest():
            src = harness / rel
            dest = project / "gobo-agent" / "runtime" / rel
            if not (src.is_file() and dest.is_file()):
                continue
            mode = 0o755 if rel in exec_launchers else src.stat().st_mode & 0o777
            try:
                os.chmod(dest, mode)
            except OSError:
                pass

    # Record ownership of every managed file whose on-disk content matches what we
    # intended (generated + project-owned); a user-modified file is left as unknown
    # so a later run reports it again instead of silently clobbering it.
    recorded_out: dict[str, str] = {}
    for op, path, data, _, _owned in planned:
        if not data:
            continue
        rel = str(path.relative_to(project)).replace("\\", "/")
        recorded_out[rel] = _sha256_bytes(data)
    install.mkdir(parents=True, exist_ok=True)
    marker_payload = {
        "schema": INIT_SCHEMA,
        "layout": args.layout,
        "harness": {"path": str(harness), "commit": commit,
                    "fingerprint": HARNESS_FINGERPRINT},
        "created": recorded_out,
    }
    marker_path.write_text(json.dumps(marker_payload, indent=2) + "\n", encoding="utf-8")

    log(f"adopted {project} into {install.relative_to(project)}/ ({args.layout})")
    for op, path, _data, _, _owned in sorted(planned, key=lambda p: str(p[1])):
        log(f"  {op}: {path.relative_to(project)}")
    if gi_missing:
        log(f"  .gitignore: +{len(gi_missing)} rules")
    if ga_missing:
        log(f"  .gitattributes: +{len(ga_missing)} rules")
    if tasks_changed:
        log(f"  .vscode/tasks.json: {len(generated_tasks)} gobo-agent tasks")
    if agents_changed:
        log("  AGENTS.md: gobo-agent section")
    if agents_skip:
        log("  AGENTS.md: not UTF-8; left untouched")
    if conflicts:
        log(f"conflicts (left untouched): {', '.join(sorted(set(conflicts)))}")

    if args.layout == "submodule":
        runtime = install / "runtime"
        if not runtime.is_dir() or not any(runtime.iterdir()):
            url = args.harness_url or _git_capture(["remote", "get-url", "origin"], harness)
            url = url or "<gobo-agent-repo-url>"
            log("runtime submodule is not initialized; add it, then re-run init:")
            log(f'  git -C "{project}" submodule add {url} gobo-agent/runtime')
            log(f'  git -C "{project}" submodule update --init --recursive gobo-agent/runtime')
            return 1
    log("next: gobo-agent/runtime/setup.ps1  then  gobo-agent/runtime/tools/gsdev.ps1 run")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_port(
        sub: argparse.ArgumentParser,
        default: int | None = DEFAULT_PORT,
        help_text: str = "CDP port; 0 picks a free port and remembers it (default %(default)s)",
    ) -> None:
        sub.add_argument("--port", type=int, default=default, help=help_text)

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

    def add_mode(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--mode", choices=["debug", "release"], default=None,
            help="build mode (default debug; GSDEV_MODE overrides)",
        )

    build = subparsers.add_parser("build", help="compile the project")
    add_port(build)
    add_mode(build)
    build.set_defaults(func=cmd_build)
    add_port(subparsers.add_parser("status", help="show sprites and run state"))

    doctor = subparsers.add_parser("doctor", help="check that required tools are available")
    add_port(doctor)
    doctor.set_defaults(func=cmd_doctor)

    setup = subparsers.add_parser(
        "setup", help="download prebuilt goboscript + host bundles + sb2gs (no admin/pip)"
    )
    setup.add_argument(
        "--only", choices=["goboscript", "vendor", "sb2gs"], help="install just one piece"
    )
    setup.add_argument("--force", action="store_true", help="re-download even if present")
    setup.add_argument("--offline", action="store_true", help="never touch the network")
    setup.set_defaults(func=cmd_setup)

    sb2gs = subparsers.add_parser(
        "sb2gs", help="import a Scratch project with the bundled sb2gs (e.g. --id 123 name.sb3)"
    )
    sb2gs.add_argument(
        "sb2gs_args", nargs=argparse.REMAINDER, help="arguments forwarded to sb2gs"
    )
    sb2gs.set_defaults(func=cmd_sb2gs)

    selftest = subparsers.add_parser(
        "selftest", help="check per-platform path/flag logic (incl. macOS)"
    )
    selftest.set_defaults(func=cmd_selftest)

    tasks = subparsers.add_parser("tasks", help="check .vscode tasks resolve on this platform")
    tasks.add_argument("--run", action="store_true", help="also execute the non-launching tasks")
    tasks.set_defaults(func=cmd_tasks)

    init = subparsers.add_parser(
        "init", help="adopt an existing project into a gobo-agent/ folder"
    )
    init.add_argument("--project", default=".", help="project directory (default: cwd)")
    init.add_argument("--harness", help="gobo-agent source dir (default: this checkout)")
    init.add_argument("--layout", choices=["copy", "submodule"], default="copy",
                      help="copy the runtime (default) or scaffold a submodule")
    init.add_argument("--harness-url", help="git URL for --layout submodule")
    init.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    init.add_argument("--force", action="store_true",
                      help="re-copy generated files (not project-owned ones)")
    init.add_argument("--force-owned", action="store_true",
                      help="also overwrite project-owned files")
    init.set_defaults(func=cmd_init)

    run = subparsers.add_parser("run", help="build, start, and stream logs")
    add_port(run)
    add_cpu(run)
    add_mode(run)
    run.add_argument("--no-build", action="store_true", help="skip goboscript build")
    run.add_argument("--sb3", default="", help="load this sb3 instead of building")
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
    add_mode(shot)
    shot.add_argument("--no-build", action="store_true", help="skip goboscript build")
    shot.add_argument("--sb3", default="", help="load this sb3 instead of building")
    shot.add_argument("--out", default="", help="output path (default debug/stage.png)")
    shot.add_argument("--delay", type=int, default=1200, help="wait before capture in ms")
    shot.add_argument("--headless", action="store_true", help="no visible browser window")
    add_software(shot)
    shot.set_defaults(func=cmd_screenshot)

    pre = subparsers.add_parser('preflight', help='offline Scratch size/memory/compat check')
    add_mode(pre)
    pre.add_argument('--sb3', default='', help='scan this sb3 instead of building')
    pre.add_argument('--json', action='store_true', help='emit the report as JSON')
    pre.add_argument('--strict', action='store_true', help='exit non-zero on warnings too')
    pre.set_defaults(func=cmd_preflight)

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
        "session", help="run a batch of get/set/input/expect lines (stdin or --file)"
    )
    add_port(session)
    add_json(session)
    session.add_argument(
        "--file",
        help="read the session script from this UTF-8 file instead of stdin "
             "(recommended on PowerShell 5.1, whose pipes are not UTF-8)",
    )
    session.add_argument(
        "--events", default="",
        help="write versioned structured assertion records (JSONL) to this path",
    )
    session.add_argument(
        "--bundle", action="store_true",
        help="assemble a failure bundle under --artifacts when an assertion fails",
    )
    session.add_argument(
        "--artifacts", default="",
        help="artifact root for --bundle (default debug/)",
    )
    session.set_defaults(func=cmd_session)

    test = subparsers.add_parser(
        "test", help="run session files (or a test manifest) as tests"
    )
    add_port(test)
    add_mode(test)
    test.add_argument("files", nargs="*", help="session files to run as tests")
    test.add_argument("--manifest", default="", help="run the tests in this manifest")
    test.add_argument(
        "--list", action="store_true",
        help="validate and list the manifest's tests without running (requires --manifest)",
    )
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

    perf = subparsers.add_parser(
        "perf", help="print fps / rendertime / steptime snapshot"
    )
    add_port(perf)
    perf.set_defaults(func=cmd_perf)

    gpu = subparsers.add_parser(
        "gpu", help="print the host GL renderer (GPU vs software)"
    )
    add_port(gpu)
    gpu.set_defaults(func=cmd_gpu)

    setprof = subparsers.add_parser(
        "setprofiling", help="enable/disable procedure profiling (opt-in, intrusive)"
    )
    add_port(setprof)
    setprof.add_argument("mode", choices=["on", "off"])
    setprof.set_defaults(func=cmd_setprofiling)

    profread = subparsers.add_parser(
        "profiling", help="print the current profiling report (no new capture)"
    )
    add_port(profread)
    profread.add_argument("--top", type=int, default=15, help="procedure rows to print")
    profread.add_argument("--children", default="",
                          help="also print the callees of this procedure")
    profread.add_argument("--sort", choices=["self", "kids", "total", "incl"],
                          default="self",
                          help="rank by own work (default), callee subtree, or the whole "
                               "subtree (total; incl is an alias)")
    profread.add_argument("--hats-min", type=float, default=2.0,
                          help="only list top-level scripts holding at least this %% of "
                               "the work (default 2)")
    profread.add_argument("--steps", type=int, default=20,
                          help="also print the last N per-step lines (0 disables)")
    profread.add_argument("--json", default="", help="write the full report to this path")
    profread.set_defaults(func=cmd_profiling)

    profreset = subparsers.add_parser(
        "profilereset", help="start a fresh profiling window (instrumentation stays on)"
    )
    add_port(profreset)
    profreset.set_defaults(func=cmd_profilereset)

    profile = subparsers.add_parser(
        "profile", help="capture a bounded window and print procedure hotspots"
    )
    add_port(profile)
    add_cpu(profile)
    add_mode(profile)
    profile.add_argument("--seconds", type=float, default=2.0,
                         help="capture window in seconds (default 2)")
    profile.add_argument("--warmup", type=float, default=0.5,
                         help="seconds to run before the capture window is reset")
    profile.add_argument("--top", type=int, default=15, help="procedure rows to print")
    profile.add_argument("--children", default="",
                         help="also print the callees of this procedure (substring match "
                              "on the procedure key)")
    profile.add_argument("--json", default="", help="write the full report to this path")
    profile.add_argument("--sort", choices=["self", "kids", "total", "incl"],
                         default="self",
                         help="rank by own work (default), callee subtree, or the whole "
                              "subtree (total; incl is an alias)")
    profile.add_argument("--hats-min", type=float, default=2.0,
                         help="only list top-level scripts holding at least this %% of "
                              "the work (default 2)")
    profile.add_argument("--no-build", action="store_true", help="skip goboscript build")
    profile.add_argument("--no-reload", action="store_true",
                         help="reuse the running project (no build, no reload)")
    profile.add_argument("--no-restart", action="store_true",
                         help="do not re-run the green-flag scripts inside the capture window")
    profile.add_argument("--follow", dest="follow", action="store_true", default=True,
                         help=argparse.SUPPRESS)
    profile.add_argument("--no-follow", dest="follow", action="store_false",
                         help="do not stream per-step lines during the capture")
    profile.add_argument("--follow-interval", type=float, default=0.2,
                         help="streaming poll interval (seconds, default 0.2)")
    profile.add_argument("--wait-until", default="",
                         help="event-gated start: begin the capture once this variable "
                              "condition is met (excludes load/setup)")
    profile.add_argument("--wait-op", default=">",
                         choices=["==", "!=", ">", "<", ">=", "<="])
    profile.add_argument("--wait-value", default="0")
    profile.add_argument("--wait-timeout", type=float, default=60000.0,
                         help="event wait timeout in ms (default 60000)")
    profile.add_argument("--leave-running", action="store_true",
                         help="keep profiling enabled and the host running")
    profile.add_argument("--headless", action="store_true", help="no visible browser window")
    add_software(profile)
    profile.set_defaults(func=cmd_profile)

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
    add_port(
        close,
        None,
        "CDP port of the host to close (default: the host's recorded CDP port)",
    )
    close.set_defaults(func=cmd_close)

    subparsers.choices["build"].set_defaults(func=cmd_build)
    subparsers.choices["status"].set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    global JSON_MODE
    configure_stdio()
    raw = list(sys.argv[1:] if argv is None else argv)
    # `sb2gs` is a pass-through: forward its own flags untouched (argparse would
    # otherwise try to interpret options like --id itself).
    if raw and raw[0] == "sb2gs":
        return _run_sb2gs(raw[1:])
    args = build_parser().parse_args(raw)
    if getattr(args, "json", False):
        JSON_MODE = True
    # `build` has no port; `close` resolves its own port (the host's recorded CDP
    # port when none is given) inside cmd_close.
    if args.command not in ("build", "close") and hasattr(args, "port"):
        args.port = resolve_port(args.port)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
