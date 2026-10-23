"""Host/browser lifecycle: ports, profiles, launching, CDP attach, state files.

Settings come from gsdev.py once at import (`configure`), so no module here reads
the project config directly. Everything else is host-local.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from cdp import CDP, CDPError, connect, list_targets


DEFAULT_PORT = 9223
PORT_FILE = Path(".gsdev-port")
STATE_ROOT = Path(".")
HOST_DIR = Path(".")
HOST_PROFILE_ROOT = Path(".")
HOST_SERVER_PORT = 8077
BUNDLES_DIR = Path(".")
TOOLS_DIR = Path(".")
PROJECT_ROOT = Path(".")
HARNESS_FINGERPRINT = ""
HOST_ATTACH_TIMEOUT = 5.0
LAUNCH_ATTEMPTS = 2
HOST_READY_TIMEOUT = 10.0
CDP_ATTACH_TIMEOUT = 2.0
READ_TIMEOUT = 5.0
HOST_SERVER_TIMEOUT = 2.0


def _setup_hint() -> str:
    return "run: python tools/gsdev.py setup"


def host_bundles_present() -> bool:
    return True


def log(message: str) -> None:
    print(f"[gsdev] {message}", flush=True)


def configure(**settings) -> None:
    """Inject the config-derived settings once, from gsdev.py."""
    globals().update(settings)


def host_profile_dir(port: int) -> Path:
    """Browser profile directory for a CDP port.

    One profile per port, so two hosts (A/B) can run at once: a shared
    ``--user-data-dir`` makes the second Chromium launch hand off to the first
    and exit, and then no second debug port ever appears.
    """
    return HOST_PROFILE_ROOT / str(port)


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


def _host_state_root(app: str) -> Path:
    """Stable state root shared across harness processes (temp is per DSH call)."""
    override = os.environ.get("GSDEV_STATE_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        base = Path(__file__).resolve().parents[2] / ".gsbridge" / "host"
    try:
        (base / app).mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(os.environ.get("TEMP", tempfile.gettempdir())) / "gobo-agent"
        (base / app).mkdir(parents=True, exist_ok=True)
    return base / app


def _host_state_path(app: str) -> Path:
    return _host_state_root(app) / f"gsdev_host_{HOST_SERVER_PORT}.json"


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


def host_state_records() -> list[tuple[Path, dict]]:
    records: list[tuple[Path, dict]] = []
    for app in _HOST_STATE_APPS:
        for path in _host_state_root(app).glob("gsdev_host_*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if isinstance(record, dict):
                records.append((path, record))
    return records


def reap_host_orphans(kill_live: bool = False) -> list[int]:
    """Drop records whose browser and server are gone; optionally kill live browsers.

    Only processes whose executable name matches the launch record are touched.
    """
    killed: list[int] = []
    for path, record in host_state_records():
        browser_pid = record.get("hostPid")
        browser_name = record.get("hostBrowser") or ""
        live_browser = (isinstance(browser_pid, int) and browser_name
                        and _name_matches(_pid_name(browser_pid), browser_name))
        server_pid = record.get("pid")
        live_server = isinstance(server_pid, int) and bool(_pid_name(server_pid))
        if live_browser and kill_live:
            terminate_pids([browser_pid])
            killed.append(browser_pid)
            live_browser = False
        if not live_browser and not live_server:
            try:
                path.unlink()
            except OSError:
                pass
    return killed


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
        # The port is open but not answering as ours yet: a concurrent command may be
        # starting a server this very moment. Fall through and try to start ours — a
        # bind failure is immediate (seen as the child exiting), after which we
        # re-probe and reuse theirs, or report a genuinely foreign holder.

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
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", **_detached_kwargs(),
    )
    # hostserver.py prints one ready line and then detaches stdout, so waiting for
    # that line is the readiness event (no port-probing loop, no sleep).
    ready = threading.Event()
    settled = threading.Event()

    def _await_ready() -> None:
        try:
            for line in proc.stdout or ():
                if line.startswith("hostserver ready "):
                    ready.set()
                    break
        except (OSError, ValueError):
            pass
        finally:
            settled.set()

    threading.Thread(target=_await_ready, daemon=True).start()
    # Wait for either outcome (the ready line, or stdout closing when the child
    # exits): a bind failure is then detected immediately instead of after a budget.
    settled.wait(HOST_SERVER_TIMEOUT)
    if not ready.is_set():
        # Fast paths, no long wait: our child either died (usually a bind error
        # because a concurrent command won the port), or another gsdev server is
        # already serving it — in which case reuse that one.
        info = probe_host_server()
        if info is not None and info.get("fingerprint") == HARNESS_FINGERPRINT:
            log(f"host server already running (pid {info.get('pid')}); reusing")
            # Only record it if nobody has: the command that started it owns the
            # state entry (and therefore `close`); do not clobber that.
            if read_host_state().get("pid") != info.get("pid"):
                write_host_state(token=info.get("token"), pid=info.get("pid"),
                                 port=HOST_SERVER_PORT, project=str(PROJECT_ROOT),
                                 fingerprint=HARNESS_FINGERPRINT)
            return
        if port_open(HOST_SERVER_PORT):
            raise SystemExit(
                f"port {HOST_SERVER_PORT} is in use by another application (not a "
                "gsdev host server). Set GSDEV_HOST_PORT to a free port, or stop that "
                "process."
            )
        died = proc.poll() is not None
        raise SystemExit(
            f"host server did not start within {HOST_SERVER_TIMEOUT:g}s"
            + (" (the child exited)" if died else "")
        )
    # One-shot verification: never treat a port owner we did not start as ours.
    info = probe_host_server(token)
    if info is None:
        raise SystemExit(
            f"port {HOST_SERVER_PORT} is held by something that is not our host server"
        )
    write_host_state(token=token, pid=info.get("pid"), port=HOST_SERVER_PORT,
                     project=str(PROJECT_ROOT), fingerprint=HARNESS_FINGERPRINT)
    log(f"host server on http://127.0.0.1:{HOST_SERVER_PORT}")


def host_target(port: int) -> dict | None:
    try:
        targets = list_targets(port=port, timeout=2.0)
    except Exception:
        return None
    for target in targets:
        if target.get("type") == "page" and "host.html" in str(target.get("url", "")):
            return target
    return None


def any_page_target(port: int) -> dict | None:
    """Any page target on the port.

    ``host_target`` only accepts a page already showing ``host.html``; at launch the
    browser may still be on ``about:blank`` because we navigate over CDP ourselves.
    """
    try:
        targets = list_targets(port=port, timeout=2.0)
    except Exception:
        return None
    for target in targets:
        if target.get("type") == "page":
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


def _pid_name(pid: int) -> str:
    if os.name != "nt":
        try:
            return subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().lower()
        except Exception:
            return ""
    for candidate, _parent, name in _win32_process_table():
        if candidate == pid:
            return name.lower()
    return ""


def _name_matches(actual: str, expected: str) -> bool:
    actual, expected = (actual or "").lower(), (expected or "").lower()
    return bool(actual and expected and (
        actual == expected or actual in expected or expected in actual))


def close_host(port: int, keep_server: bool = False) -> None:
    """Close the browser on `port`.

    `keep_server=True` leaves the host server running: a launch *retry* needs it
    (restarting it immediately usually fails on Windows, where hostserver binds
    exclusively, so the just-closed port is still in TIME_WAIT).
    """
    state = read_host_state()
    process_name = state.get("hostBrowser") or Path(find_browser() or "chrome.exe").name.lower()
    pids = find_editor_pids(port, process_name, host_profile_dir(port))
    if not pids:
        # The CDP port may already be closed (failed launch / crashed renderer),
        # but the launch record still identifies the process by PID + profile.
        pid = state.get("hostPid")
        profile = state.get("hostProfile") or ""
        if (isinstance(pid, int) and profile and Path(profile).is_dir()
                and _name_matches(_pid_name(pid), process_name)):
            pids = [pid]
    if pids:
        terminate_pids(pids)
        log(f"killed host browser (pid {', '.join(str(p) for p in pids)})")
    else:
        log("no host browser to close")
    if keep_server:
        return
    # Only stop the host server if we started it (recorded token) and it is still
    # the same process -- never a foreign application that happens to hold the port.
    token = state.get("token")
    if not token:
        clear_host_state()
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


def abort_host(port: int, keep_server: bool = False) -> None:
    """Tear down a failed launch so nothing detached is left behind.

    A failed `run` used to leave the host server (started with the portable
    Python) alive; that then held `.tools/python`, so a rebuild could not delete
    it until the interpreter was killed by hand. `keep_server=True` is for a launch
    *retry*, which needs the server to survive the teardown.
    """
    try:
        close_host(port, keep_server=keep_server)
    except (CDPError, OSError, urllib.error.URLError):
        pass


def browser_flags(browser: str, port: int, profile) -> list[str]:
    """Chromium flags shared by gsdev and the bridge.

    Both launchers must use the same settings or their runs are not comparable
    (perf numbers, dialog suppression, GPU/occlusion behavior). Callers append
    their own extras and the target URL.

    ``GSDEV_MAX_OLD_SPACE_MB`` caps the V8 heap (``--js-flags=--max-old-space-size``)
    to emulate a low-memory device; ``GSDEV_DPR`` sets ``--force-device-scale-factor``
    (2-3 for HiDPI phones). Like the other flags they take effect at launch, so
    ``close`` a warm host before changing them.
    """
    flags = [
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
    mem_mb = os.environ.get("GSDEV_MAX_OLD_SPACE_MB", "").strip()
    if mem_mb:
        try:
            flags.append(f"--js-flags=--max-old-space-size={int(float(mem_mb))}")
        except ValueError:
            pass
    dpr = os.environ.get("GSDEV_DPR", "").strip()
    if dpr:
        try:
            flags.append(f"--force-device-scale-factor={float(dpr):g}")
        except ValueError:
            pass
    if os.environ.get("GSDEV_LOW_END", "").strip().lower() not in ("", "0", "false"):
        flags.append("--enable-low-end-device-mode")
    extra = os.environ.get("GSDEV_BROWSER_ARGS", "").strip()
    if extra:
        flags.extend(extra.split())
    return flags


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


def spawn_browser(flags: list[str], timeout: float | None = None) -> tuple[subprocess.Popen, dict]:
    """Launch a browser and wait for its DevTools readiness line (or its exit).

    Chromium announces its endpoint on stderr; that line is the "a debuggable page
    exists" event (``marks["ws"]``). The pipe is drained for the browser's whole
    lifetime: Chromium is chatty on a headed run, and a pipe we stop reading fills up
    and blocks the renderer, after which the page never loads. ``marks["settled"]`` is
    set on the line or when stderr closes, so a browser that dies at once fails fast
    instead of consuming the attach budget (``marks["exit_code"]`` is then set).

    Shared with gsbridge so both hosts launch and detect readiness the same way.
    """
    if timeout is None:
        timeout = HOST_ATTACH_TIMEOUT
    proc = subprocess.Popen(
        flags, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **_detached_kwargs(),
    )
    marks: dict = {
        "devtools": threading.Event(),
        "settled": threading.Event(),
        "ws": None,
        "exit_code": None,
    }

    def _drain() -> None:
        try:
            for line in proc.stderr or ():
                if not marks["devtools"].is_set() and "DevTools listening on ws://" in line:
                    marks["ws"] = line.split("DevTools listening on ", 1)[1].strip()
                    marks["devtools"].set()
                    marks["settled"].set()
        except (OSError, ValueError):
            pass
        finally:
            marks["settled"].set()

    threading.Thread(target=_drain, daemon=True).start()
    marks["settled"].wait(timeout)
    if not marks["devtools"].is_set():
        try:
            marks["exit_code"] = proc.wait(timeout=0.5)  # a process wait, not a sleep
        except subprocess.TimeoutExpired:
            pass
    return proc, marks


def _page_error(conn: CDP) -> str:
    """The page's URL if the browser is showing its own navigation-error page, else ''."""
    try:
        href = conn.evaluate("location.href", timeout=CDP_ATTACH_TIMEOUT) or ""
    except (CDPError, TimeoutError, OSError):
        return ""
    return str(href) if str(href).startswith("chrome-error://") else ""


def _navigate_host(conn: CDP) -> bool:
    """Page.enable + navigate to the host page + await the load event.

    False means the navigation did not land on the host page (typically because the
    host server was not accepting connections, so Chromium showed its error page).
    The caller retries rather than waiting out the whole ready budget.
    """
    try:
        conn.call("Page.enable", timeout=CDP_ATTACH_TIMEOUT)
        conn.call("Page.navigate", {"url": host_url()}, timeout=HOST_READY_TIMEOUT)
        conn.event("Page.loadEventFired", timeout=HOST_READY_TIMEOUT)
    except (CDPError, TimeoutError, OSError):
        return False
    return not _page_error(conn)


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
    # Launch on a blank page and navigate over CDP: that way the navigation happens
    # after we enable Page events, so `Page.loadEventFired` is deterministic.
    # (Launching straight at the host URL races that event and can run an evaluate
    # in the pre-navigation context, which then gets destroyed.)
    base_flags = list(flags)
    proc: subprocess.Popen | None = None
    conn: CDP | None = None
    for attempt in range(1, LAUNCH_ATTEMPTS + 1):
        # abort_host() stops the host server as well as the browser, so every attempt
        # (including a relaunch) must make sure the server is back before the page
        # navigates to it. Reuses an existing server when one is up.
        ensure_host_server()
        flags = base_flags + ["about:blank"]
        log(f"launching {Path(browser).name}{' headless' if headless else ''}"
            f"{'' if attempt == 1 else f' (attempt {attempt}/{LAUNCH_ATTEMPTS})'}")
        proc, marks = spawn_browser(flags)
        devtools = marks["devtools"]
        if devtools.is_set():
            log(f"{Path(browser).name} devtools ready")
        elif marks["exit_code"] not in (None, 0):
            abort_host(port)
            raise SystemExit(
                f"{Path(browser).name} exited immediately (code {marks['exit_code']}); "
                "is it blocked by policy?"
            )
        else:
            # Chromium's launcher stub can hand off to the real browser and exit 0 while
            # the browser keeps running (observed on Windows Edge), and a browser that dies
            # silently just closes stderr — neither proves failure, so probe for the
            # endpoint below instead of declaring it here.
            log(f"{Path(browser).name} printed no DevTools line; probing for the endpoint")
        # Record the CDP port so a later `close` (with no --port) can find this
        # browser instead of guessing the default. pid/profile are the crash record:
        # close can kill it even when the page/port is no longer reachable.
        update_host_state(cdpPort=port, hostPid=proc.pid, hostProfile=str(profile),
                          hostBrowser=Path(browser).name.lower(), hostStarted=time.time())
        # Phase 1: a debuggable page (the DevTools event above, or probing if the
        # browser stayed silent). Phase 2: the page's own ready event below. A launcher
        # stub that handed off (exit 0, no line) leaves the browser coming up, which is a
        # ready-phase wait rather than an attach failure, so the probe gets the longer
        # budget there; the fast path keeps the short one.
        attach_budget = HOST_ATTACH_TIMEOUT if devtools.is_set() else HOST_READY_TIMEOUT
        attach_deadline = time.monotonic() + attach_budget
        conn = None
        silent = not devtools.is_set()
        while time.monotonic() < attach_deadline and conn is None:
            dismiss_browser_dialogs(port)
            target = any_page_target(port)
            if target is not None:
                try:
                    conn = connect(target, timeout=CDP_ATTACH_TIMEOUT)
                    break
                except (CDPError, TimeoutError, OSError):
                    pass
            if silent:
                time.sleep(0.05)  # fallback only: no DevTools line to wait on
        # Navigate here, inside the retry loop: a navigation that never lands on the
        # host page (server not accepting connections -> Chromium's error page) is a
        # launch failure and must be retried, not charged to the ready budget.
        if conn is not None and _navigate_host(conn):
            break
        if conn is not None:
            log(f"{Path(browser).name} landed on {_page_error(conn) or 'a non-host page'}; "
                "relaunching")
            conn.close()
            conn = None
        # No usable page: clear the browser's owner and try once more. Keep the host
        # server alive when another attempt follows (the browser, not the server, is
        # what failed here).
        more_attempts = attempt < LAUNCH_ATTEMPTS
        abort_host(port, keep_server=more_attempts)
        if not more_attempts:
            raise SystemExit(
                f"no debuggable {Path(browser).name} page appeared within "
                f"{attach_budget:g}s after {LAUNCH_ATTEMPTS} attempt(s); is the browser "
                "blocked by policy? (raise GSDEV_HOST_TIMEOUT for a slow cold start, "
                "GSDEV_LAUNCH_ATTEMPTS for more tries)"
            )
        log(f"{Path(browser).name} gave no usable page; relaunching")
    # Phase 2 is the page's own ready event, awaited in-page (no polling). The
    # navigation itself happened inside the retry loop above.
    ready_error = ""
    ready_phase = "gsdev:ready"
    ready: dict | None = None
    try:
        # The listener is installed by us (not by host.js) because host.js loads well
        # after `load`; ready() covers the case where the event already fired.
        ready = conn.evaluate(
            "new Promise(r => {"
            " if (window.__host && window.__host.ready) { window.__host.ready(0).then(r); return; }"
            " window.addEventListener('gsdev:ready',"
            "   () => r({ ok: true, why: 'event', fatal: (window.__marks && window.__marks.fatal) || null }),"
            "   { once: true });"
            f" setTimeout(() => r({{ ok: false, why: 'timeout' }}), {int(HOST_READY_TIMEOUT * 1000)});"
            " })",
            await_promise=True, timeout=HOST_READY_TIMEOUT + 5,
        )
    except (CDPError, TimeoutError, OSError) as exc:
        ready = None
        ready_error = f"{type(exc).__name__} during {ready_phase}: {exc}"
    if ready and ready.get("fatal"):
        # host.js recorded a fatal init error: report it at once rather than as a
        # ready timeout (the event still fires, so ok is true).
        conn.close()
        abort_host(port)
        raise SystemExit(f"host page failed to initialise: {ready['fatal']}")
    if not (ready or {}).get("ok"):
        detail = ""
        try:
            error = conn.evaluate("window.__hostBundlesError || ''", timeout=CDP_ATTACH_TIMEOUT)
            if error:
                detail = f" (host page: {error})"
        except (CDPError, TimeoutError, OSError):
            pass
        # What was the page actually doing? Distinguishes "still fetching resources"
        # from "host.js never ran" from "init threw" without another reproduction.
        try:
            page = conn.evaluate(
                "JSON.stringify({readyState: document.readyState, href: location.href,"
                " hasHost: !!window.__host, hasVm: !!(window.__host && window.__host.vm),"
                " marksReady: !!(window.__marks && window.__marks.ready),"
                " marksFatal: (window.__marks && window.__marks.fatal) || null,"
                " scripts: document.scripts.length,"
                " bundles: window.__gsdevBundles ? window.__gsdevBundles.length : null})",
                timeout=CDP_ATTACH_TIMEOUT,
            )
            detail += f" (page: {page})"
        except (CDPError, TimeoutError, OSError) as exc:
            detail += f" (page diag failed: {type(exc).__name__})"
        # Was the page's *server* reachable from here at the same moment the browser
        # showed its error page? (Distinguishes a dead/slow host server from a
        # browser-side navigation failure.)
        try:
            import urllib.request
            with urllib.request.urlopen(host_url(), timeout=3) as resp:
                detail += f" (server: http {resp.status}, {len(resp.read())} bytes)"
        except Exception as exc:  # noqa: BLE001
            detail += f" (server unreachable: {type(exc).__name__}: {exc})"
        conn.close()
        abort_host(port)
        raise SystemExit(
            f"scratch-vm host page did not finish loading within {HOST_READY_TIMEOUT:g}s"
            f"{detail}{(' (' + ready_error + ')') if ready_error else ''}; {_setup_hint()}"
        )
    log("host ready" + (" (waited on gsdev:ready)" if ready.get("waited") else ""))
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
