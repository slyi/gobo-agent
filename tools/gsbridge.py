#!/usr/bin/env python3
"""Run the same live-game verbs against any Scratch host.

Targets:
  localhost   our gsdev host page (window.__host)
  github      scratchfoundation.github.io/scratch-gui/player.html (clean player)
  editor      scratch.mit.edu/projects/editor (production build)
  production  an existing scratch.mit.edu project page/embed (--url or --project-id)
  url         any page that runs a scratch-gui VM (--url)

A React-fiber walk discovers the VM on every page, then a small bridge is
injected that mirrors the capability surface of tools/scratchhost/host.js:
live variable/list edits, mouse/key input, screenshots, getpixel, and goboscript
log/warn/error capture -- plus a frame counter and a small perf snapshot (fps and
steptime). It also wires the shared procedure profiler (`profile`, `profiling`,
`setprofiling`, `profilereset`) from `scratchhost/profiler.js`, the same module the
local host loads, so per-procedure attribution and Draws/step work here too.
`errors` and `expect_no_errors` count goboscript `error` blocks, not only VM
exceptions.

A `run` refuses a port that already has a host (no silent reuse of a previous
session), and `close` only shuts down the browser the bridge launched -- never the
shared gsdev host server.

This tool is intentionally separate from gsdev so it cannot destabilise the main
loop. It reuses gsdev's CDP/browser helpers.

Examples:
  python tools/gsbridge.py run --target github --sb3 examples/writesProbe/writesProbe.sb3 --duration 5
  python tools/gsbridge.py run --target production --project-id 123456789 --headless --leave-running
  python tools/gsbridge.py get main.a debuglogs
  python tools/gsbridge.py set main.a 7
  python tools/gsbridge.py click 0 0
  python tools/gsbridge.py key space
  python tools/gsbridge.py pixel 0 0
  python tools/gsbridge.py screenshot --out debug/bridge.png
  python tools/gsbridge.py logs
  python tools/gsbridge.py errors
  python tools/gsbridge.py expect_no_errors
  python tools/gsbridge.py session --file smoke.txt
  python tools/gsbridge.py close
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gsdev  # noqa: E402  (also configures gsdevlib.host)
from cdp import CDPError, connect, list_targets  # noqa: E402
from gsdevlib import host as bridge_host  # noqa: E402
from gsdevlib import page as bridge_page  # noqa: E402
from gsdevlib import state as bridge_state  # noqa: E402

DEFAULT_PORT = int(os.environ.get("GSBRIDGE_PORT", "9400"))
DEFAULT_TIMEOUT = 30.0

TARGET_URLS = {
    "github": "https://scratchfoundation.github.io/scratch-gui/player.html",
    "editor": "https://scratch.mit.edu/projects/editor/",
}

# ---------------------------------------------------------------------------
# Injected bridge (mirrors host.js capability surface against a discovered VM)
# ---------------------------------------------------------------------------
BRIDGE_JS = (Path(__file__).resolve().parent / "gsdevlib" / "bridge_shim.js").read_text(
    encoding="utf-8")

BOOT_WAIT_JS = "!!(window.__bridge && window.__bridge.__installed)"
# The profiler module is shared with the local host (scratchhost/profiler.js), so the
# bridge and host run one implementation. Injected before BRIDGE_JS, which wires it up.
PROFILER_JS = (Path(__file__).resolve().parent / "scratchhost" / "profiler.js").read_text(
    encoding="utf-8")
STATE_JS = "(() => ({ origin: location.origin, bridge: !!(window.__bridge && window.__bridge.__installed), frame: window.__bridge ? window.__bridge.frame() : -1 }))()"


def log(obj) -> None:
    print(obj if isinstance(obj, str) else json.dumps(obj), flush=True)


def load_sb3_b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def project_target_names(sb3: Path) -> list[str]:
    """Sprite names in a built project (excluding the stage), for load verification."""
    try:
        with zipfile.ZipFile(sb3) as archive:
            data = json.loads(archive.read("project.json"))
        return [t.get("name") for t in data.get("targets", [])
                if not t.get("isStage") and t.get("name")]
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return []


def load_project_script(encoded: str) -> str:
    """JS that loads and starts a base64 sb3 in the bridge's VM. Returns ms taken."""
    return ("(async () => { const b = atob('" + encoded + "');"
            " const a = new Uint8Array(b.length); for (let i=0;i<b.length;i++) a[i]=b.charCodeAt(i);"
            " const t0 = performance.now(); await window.__bridge.vm.loadProject(a.buffer);"
            " window.__bridge.vm.start(); return Math.round((performance.now()-t0)*10)/10; })()")


def bridge_target_names(cdp) -> list[str]:
    try:
        names = cdp.evaluate("(window.__bridge.rt.targets || []).map(t => t.getName())")
        return [str(n) for n in names] if isinstance(names, list) else []
    except (CDPError, TimeoutError, OSError):
        return []


def inject_bridge(cdp, timeout: float = 30.0) -> bool:
    """Install the bridge once, then wait for the page's project (event-driven).

    Installing is synchronous, so one evaluate suffices; readiness is then the page's
    own per-step predicate via waitReady(), never a CLI polling loop.
    """
    # The page mounts its VM on its own schedule (React/scratch-gui, notably on
    # scratch.mit.edu), so install once and retry until the VM is reachable, bounded by
    # `timeout`. There is no event for "React mounted", so this is a short bounded poll,
    # not a state wait: readiness itself stays event-driven below.
    deadline = time.monotonic() + timeout
    while True:
        try:
            if cdp.evaluate(BOOT_WAIT_JS, timeout=10):
                break
            cdp.evaluate(PROFILER_JS, timeout=10)
            cdp.evaluate(BRIDGE_JS, timeout=10)
            if cdp.evaluate(BOOT_WAIT_JS, timeout=10):
                break
        except (CDPError, TimeoutError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    try:
        result = cdp.evaluate(
            f"window.__bridge.waitReady({int(timeout * 1000)})",
            await_promise=True, timeout=timeout + 5,
        ) or {}
    except (CDPError, TimeoutError, OSError):
        return False
    return bool(result.get("ok"))


def open_target_page(port: int, timeout: float = 15.0, match: str | None = None):
    """Attach to a page target, waiting for one to appear.

    Not a fixed sleep loop: a Chromium launcher stub can exit 0 while the real
    browser is still starting, so the CDP endpoint (and its page target) may not be
    listed yet at launch time. The wait is bounded by `timeout`, which is also the
    per-listing budget.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            targets = list_targets(port=port, timeout=min(5.0, timeout))
        except (CDPError, TimeoutError, OSError):
            targets = []
        for t in targets:
            if t.get("type") != "page":
                continue
            url = t.get("url", "")
            if url.startswith("edge://") or url.startswith("chrome://"):
                continue
            if match and match not in url:
                continue
            try:
                return connect(t)
            except (CDPError, TimeoutError, OSError):
                break
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def resolve_browser(name: str | None) -> str | None:
    if name:
        if os.path.isabs(name) and Path(name).exists():
            return name
        low = name.lower()
        table = {
            "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                       r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
            "edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                     r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
        }
        for path in table.get(low, []):
            if Path(path).exists():
                return path
    return bridge_host.find_browser()


def launch_browser(args, url: str, port: int, profile: Path) -> tuple[subprocess.Popen, dict]:
    browser = resolve_browser(args.browser)
    if not browser:
        raise SystemExit("no Chrome/Edge found; set GSDEV_BROWSER")
    # Shared with gsdev so both hosts run under the same browser settings; the
    # bridge only adds its own extras below.
    flags = bridge_host.browser_flags(browser, port, profile)
    flags += ["--disable-extensions", "--window-size=1200,900"]
    if args.headless:
        flags.append("--headless=new")
    flags.append(url)
    log(f"launching {Path(browser).name}{' headless' if args.headless else ''} -> {url}")
    # Shared launch: the DevTools stderr line is the readiness event and the pipe is
    # drained for the browser's whole life (a headed Chromium blocks on a full pipe).
    proc, marks = bridge_host.spawn_browser(flags, timeout=args.ready_timeout)
    if marks["devtools"].is_set():
        log(f"{Path(browser).name} devtools ready")
    elif marks["exit_code"] not in (None, 0):
        raise SystemExit(
            f"{Path(browser).name} exited immediately (code {marks['exit_code']}); "
            "is it blocked by policy?"
        )
    return proc, marks


def target_url(args) -> str:
    if args.url:
        return args.url
    if args.target in TARGET_URLS:
        return TARGET_URLS[args.target]
    if args.target == "production":
        if not args.project_id:
            raise SystemExit("--target production needs --project-id N (or --url)")
        return f"https://scratch.mit.edu/projects/{args.project_id}/embed"
    if args.target == "localhost":
        return bridge_host.host_url()
    raise SystemExit(f"unknown target {args.target}")


# Temp-dir namespace for bridge profiles/session state. `kilo` is the historical
# location; read/clear still check it so a session launched by an older build is
# found, but new state uses the tool's own name.
_BRIDGE_STATE_APPS = ("gobo-agent", "kilo")


def _bridge_temp_dir(app: str) -> Path:
    return Path(os.environ.get("TEMP", ".")) / app / "gsbridge"


def profile_dir(port: int, tag: str) -> Path:
    base = _bridge_temp_dir(_BRIDGE_STATE_APPS[0])
    return base / f"{tag}_{port}_{int(time.time() * 1000)}"


def match_for(url: str, target: str) -> str:
    """URL substring identifying the page we launched (for CDP target matching)."""
    if target == "localhost":
        return f"127.0.0.1:{bridge_host.HOST_SERVER_PORT}"
    return urlparse(url).netloc or ""


def _state_root(app: str) -> Path:
    """Stable state root, shared across harness processes.

    ``%TEMP%`` is per DSH invocation, so a state file written by ``run`` is not
    visible to the next command. The project-local ``.gsbridge/`` is stable and
    writable under the workspace sandbox; ``GSBRIDGE_STATE_DIR`` overrides it.
    """
    override = os.environ.get("GSBRIDGE_STATE_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        base = Path(__file__).resolve().parent.parent / ".gsbridge"
    try:
        (base / app).mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(os.environ.get("TEMP", ".")) / "gobo-agent"
        (base / app).mkdir(parents=True, exist_ok=True)
    return base / app


def state_path(port: int, app: str | None = None) -> Path:
    base = _state_root(app or _BRIDGE_STATE_APPS[0])
    base.mkdir(parents=True, exist_ok=True)
    return base / f"session_{port}.json"


def _session_id() -> str:
    sid = os.environ.get("DSH_SESSION_ID") or os.environ.get("GSBRIDGE_SESSION") or "default"
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in sid)


def session_path(app: str | None = None) -> Path:
    base = _state_root(app or _BRIDGE_STATE_APPS[0])
    base.mkdir(parents=True, exist_ok=True)
    return base / f"current_{_session_id()}.json"


def write_session(port: int, **fields) -> None:
    try:
        record = dict(port=int(port), updated=time.time(), **fields)
        session_path().write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass


def read_session() -> dict:
    for app in _BRIDGE_STATE_APPS:
        try:
            record = json.loads(session_path(app).read_text(encoding="utf-8"))
            if isinstance(record, dict) and isinstance(record.get("port"), int):
                return record
        except (OSError, ValueError):
            continue
    return {}


def clear_session(port: int | None = None) -> None:
    record = read_session()
    if port is not None and record and record.get("port") != port:
        return
    for app in _BRIDGE_STATE_APPS:
        try:
            session_path(app).unlink()
        except OSError:
            pass


def port_alive(port: int) -> bool:
    try:
        return any(t.get("type") == "page" for t in list_targets(port=port, timeout=0.75))
    except (CDPError, TimeoutError, OSError):
        return False


def resolve_port(explicit: int | None, for_run: bool = False) -> int:
    """Explicit --port, then GSBRIDGE_PORT, then this session's last run, then 9400."""
    if explicit is not None:
        return int(explicit)
    env = os.environ.get("GSBRIDGE_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    if not for_run:
        record = read_session()
        if record and isinstance(record.get("port"), int):
            if os.environ.get("GSBRIDGE_TRUST_SESSION") == "1" or port_alive(record["port"]):
                return record["port"]
    return DEFAULT_PORT


def registry_path(port: int, app: str | None = None) -> Path:
    base = _state_root(app or _BRIDGE_STATE_APPS[0])
    base.mkdir(parents=True, exist_ok=True)
    return base / f"browser_{port}.json"


def write_registry(port: int, pid: int, profile, url: str = "", target: str = "") -> None:
    """Record a launched browser immediately, before any navigation or bridge work.

    ``state`` is written later and carries the session token; the registry is the
    crash/failure record that lets a later ``close`` or run reap the process even
    when the token/state never made it.
    """
    try:
        record = dict(port=int(port), pid=int(pid), profile=str(profile), url=url,
                      target=target, session=_session_id(), started=time.time())
        registry_path(port).write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass


def read_registry(port: int) -> dict:
    for app in _BRIDGE_STATE_APPS:
        try:
            record = json.loads(registry_path(port, app).read_text(encoding="utf-8"))
            if isinstance(record, dict):
                return record
        except (OSError, ValueError):
            continue
    return {}


def clear_registry(port: int) -> None:
    for app in _BRIDGE_STATE_APPS:
        try:
            registry_path(port, app).unlink()
        except OSError:
            pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reap_orphans(session_only: bool = True, kill_live: bool = False) -> list[int]:
    """Remove stale browser records; with ``kill_live`` also stop live owned browsers.

    Only records whose profile matches the PID's command line are killed, so a
    recycled PID or another tool's browser is left alone. ``session_only`` limits
    the scan to this DSH session to avoid touching concurrent bridges.
    """
    killed: list[int] = []
    sid = _session_id()
    for app in _BRIDGE_STATE_APPS:
        base = _state_root(app)
        for path in list(base.glob("browser_*.json")) + list(base.glob("session_*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if session_only and record.get("session") != sid:
                continue
            pid, profile = record.get("pid"), record.get("profile") or ""
            if not isinstance(pid, int):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if not pid_alive(pid):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if not kill_live:
                continue
            if pid_owns_profile(pid, profile, record.get("started")):
                try:
                    if terminate_pid(pid):
                        killed.append(pid)
                except OSError:
                    pass
                try:
                    path.unlink()
                except OSError:
                    pass
    return killed


def write_state(port: int, **fields) -> None:
    try:
        state_path(port).write_text(json.dumps(fields), encoding="utf-8")
    except OSError:
        pass


def read_state(port: int) -> dict:
    for app in _BRIDGE_STATE_APPS:
        try:
            return json.loads(state_path(port, app).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def clear_state(port: int) -> None:
    for app in _BRIDGE_STATE_APPS:
        try:
            state_path(port, app).unlink()
        except OSError:
            pass


def terminate_pid(pid: int) -> bool:
    """Best-effort terminate; the sandbox can deny os.kill even for our child."""
    if os.name != "nt":
        for sig in (15, 9):
            try:
                os.kill(pid, sig)
                return True
            except OSError:
                continue
        return False
    try:
        os.kill(pid, 15)
        return True
    except OSError:
        pass
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
        if handle:
            try:
                return bool(ctypes.windll.kernel32.TerminateProcess(handle, 1))
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
    return False


def process_start_epoch(pid: int) -> float | None:
    if os.name != "nt":
        try:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "etimes="],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            return time.time() - float(out) if out else None
        except Exception:
            return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid}).StartTime.ToUniversalTime().ToString('o')"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not out:
            return None
        return datetime.fromisoformat(out.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def pid_owns_profile(pid: int, profile: str, started: float | None = None) -> bool:
    """True only if `pid` is still the browser we launched.

    Prefer the command line (unique profile dir). Where WMI is access-denied (the
    harness sandbox), fall back to the profile dir plus the process start time
    recorded at launch, so cleanup still works without trusting a recycled PID.
    """
    if not profile:
        return False
    if os.name != "nt":
        command = ""
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace")
        except OSError:
            try:
                command = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True, text=True, timeout=5,
                ).stdout
            except Exception:
                return False
        return profile.lower() in command.lower()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").CommandLine'],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        out = ""
    if out:
        return profile.lower() in out.lower()
    try:
        if not Path(profile).is_dir():
            return False
        if started is None:
            started = Path(profile).stat().st_mtime
        actual = process_start_epoch(pid)
        return actual is not None and abs(actual - float(started)) <= 60.0
    except OSError:
        return False


def stop_own_browser(port: int) -> None:
    """Close only the browser this bridge session launched -- and only after
    verifying ownership. Never touch gsdev's host server (another local session
    may be using it), and never act on a stale record.

    Uses the token when state exists, and the launch registry as the fallback for
    failures that happened before state was written.
    """
    st = read_state(port)
    reg = read_registry(port)
    token = st.get("token")
    if token:
        cdp = open_target_page(port, timeout=5, match=st.get("host"))
        if cdp is not None:
            try:
                # Ownership proof: only our injected page carries this token.
                if cdp.evaluate(f"window.__bridgeSession === {json.dumps(token)}"):
                    try:
                        cdp.call("Browser.close", timeout=5)
                    except Exception:
                        pass
                else:
                    log("page on this port is not our session; not closing it")
            except Exception:
                pass
            try:
                cdp.close()
            except Exception:
                pass
    pid = st.get("pid") or reg.get("pid")
    profile = st.get("profile") or reg.get("profile") or ""
    if isinstance(pid, int) and pid_owns_profile(pid, profile, st.get("started") or reg.get("started")):
        terminate_pid(pid)
    clear_state(port)
    clear_registry(port)
    if not token and not reg:
        log(f"no bridge session record on port {port}; nothing to close")


def cmd_run(args) -> int:
    if args.target == "localhost":
        bridge_host.ensure_host_server()
    url = target_url(args)
    clear_session(args.port)
    reap_orphans(session_only=True, kill_live=False)
    port = args.port
    # Refuse to reuse a browser we don't own: an earlier --leave-running session on
    # this port would otherwise be silently re-attached (and mislabelled as the
    # newly requested target).
    try:
        existing = list_targets(port=port, timeout=1)
    except Exception:
        existing = []   # nothing listening = free
    if existing:
        raise SystemExit(
            f"port {port} already has a host; close it (gsbridge close --port {port}) "
            f"or use another --port"
        )
    match = match_for(url, args.target)
    # Launch blank and navigate over CDP: the navigation then happens after
    # Page.enable, so Page.loadEventFired is a deterministic event. Launching at the
    # URL races that event and can evaluate in the pre-navigation context.
    #
    # A launch that yields no CDP endpoint at all gets retried (same knob as gsdev):
    # the usual cause is a browser that died during startup. Each attempt uses a fresh
    # timestamped profile, so a stale instance cannot be holding ours.
    proc = None
    cdp = None
    profile = None
    for attempt in range(1, bridge_host.LAUNCH_ATTEMPTS + 1):
        profile = profile_dir(port, args.target)
        profile.mkdir(parents=True, exist_ok=True)
        proc, _marks = launch_browser(args, "about:blank", port, profile)
        write_registry(port, proc.pid, profile, url, args.target)
        cdp = open_target_page(port, timeout=args.ready_timeout)
        if cdp is not None:
            break
        proc.terminate()
        if attempt >= bridge_host.LAUNCH_ATTEMPTS:
            stop_own_browser(port)
            raise SystemExit(
                f"no page attached on port {port} after {bridge_host.LAUNCH_ATTEMPTS} "
                "attempt(s); is the browser blocked by policy? (raise --ready-timeout or "
                "GSDEV_LAUNCH_ATTEMPTS for more tries)"
            )
        log(f"no page attached on port {port}; relaunching "
            f"(attempt {attempt + 1}/{bridge_host.LAUNCH_ATTEMPTS})")
    try:
        cdp.call("Page.enable", timeout=10)
        cdp.call("Page.navigate", {"url": url}, timeout=10)
        cdp.event("Page.loadEventFired", timeout=args.ready_timeout)
    except (CDPError, TimeoutError, OSError):
        pass
    bridge_host.apply_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
    token = uuid.uuid4().hex
    session = dict(target=args.target, url=url, host=match, pid=proc.pid,
                   profile=str(profile), token=token, session=_session_id())
    write_state(port, **session)
    try:
        cdp.evaluate(f"window.__bridgeSession = {json.dumps(token)}")
        if not inject_bridge(cdp, timeout=args.ready_timeout):
            raise SystemExit(
                "could not install the bridge: the page exposes no reachable scratch VM "
                "within the ready budget (is the project still loading? raise "
                "--ready-timeout)"
            )
        info = cdp.evaluate(STATE_JS)
        log({"target": args.target, "url": info.get("origin"), "bridge": info.get("bridge")})

        sb3 = None
        if args.sb3:
            sb3 = Path(args.sb3)
        elif args.project:
            sb3 = gsdev.build_project_at(Path(args.project).resolve())
        if sb3:
            encoded = load_sb3_b64(sb3)
            # Wait for the page's own project to finish loading before replacing it.
            # The production editor initialises its default project asynchronously
            # after the page becomes attachable; loading first makes ours the loser.
            ready_deadline = time.monotonic() + 20.0
            ready_start = time.monotonic()
            # Our own localhost host page has no project of its own (gsbridge loads
            # it), so there is nothing to wait for; only live sites need this.
            if not cdp.evaluate("!!window.__host"):
                # Wait for the page's own project before replacing it: inject_bridge()
                # installs once, then waits on the page's per-step readiness.
                inject_bridge(cdp, timeout=args.ready_timeout)
            log({"page_ready_ms": int((time.monotonic() - ready_start) * 1000)})
            ms = cdp.evaluate(load_project_script(encoded), await_promise=True, timeout=120)
            log({"loaded": str(sb3), "load_ms": ms})
            cdp.evaluate(f"window.__bridgeSession = {json.dumps(token)}")
            # Verify the page's project was fully replaced (never merged): the target
            # list must be exactly Stage + our sprites. Re-load (never delete sprites)
            # if the page applied its own project after ours.
            expected = project_target_names(sb3)
            wanted = sorted(["Stage"] + expected)
            deadline = time.monotonic() + 15.0
            names: list[str] = []
            while True:
                inject_bridge(cdp, timeout=5)
                names = bridge_target_names(cdp)
                if sorted(names) == wanted or time.monotonic() >= deadline:
                    break
                log(f"reloading: page project not fully replaced (saw {names})")
                cdp.evaluate(load_project_script(encoded), await_promise=True, timeout=120)
                try:
                    cdp.evaluate("window.__bridge.waitFrames(2, 2000)",
                                 await_promise=True, timeout=5)
                except (CDPError, TimeoutError, OSError):
                    pass
            log({"targets": names})
            write_state(port, **session, sb3=str(sb3), targets=expected)
            if sorted(names) != wanted:
                log(f"WARNING: expected targets {wanted}, got {names}")

        # Live pages load their project asynchronously and the embed player mounts a
        # VM with no stepping interval until told. Wait until the bridge can see the
        # page's project (Stage + a sprite, or a PROJECT_LOADED) before starting or
        # green-flagging, even when we did not replace an sb3 (--project-id/--url).
        if not cdp.evaluate("!!window.__host"):
            inject_bridge(cdp, timeout=args.ready_timeout)

        # The page may have navigated since we first tagged it; re-assert ownership
        # (close verifies this token) on the settled document before starting.
        cdp.evaluate(f"window.__bridgeSession = {json.dumps(token)}")

        # The embed player mounts the VM without a stepping interval until play, so
        # start the loop (frames advance and fps is meaningful); the green flag is
        # separate and still honours --no-start.
        cdp.evaluate("window.__bridge.start()")
        if args.no_start:
            pass
        else:
            cdp.evaluate("window.__bridge.greenFlag()")

        if args.duration > 0:
            last_seq = 0
            deadline = time.monotonic() + args.duration
            while time.monotonic() < deadline:
                try:
                    entries = cdp.evaluate(f"window.__bridge.logsSince({last_seq})")
                except (CDPError, TimeoutError, OSError):
                    break
                for entry in entries:
                    last_seq = max(last_seq, int(entry.get("seq", last_seq)))
                    level = entry.get("level", "log")
                    if level == "stop":
                        log("[vm] project stopped")
                    elif level == "log":
                        log(f"[LOG {entry['sprite']}] {entry['value']}")
                    else:
                        log(f"[{str(level).upper()} {entry['sprite']}] {entry['value']}")
                time.sleep(0.1)

        if args.leave_running:
            write_session(port, **{k: v for k, v in session.items() if k != "token"})
            log({"left_running": True, "port": port})
            return 0
        try:
            cdp.evaluate("window.__bridge.stopAll()")
        except Exception:
            pass
        stop_own_browser(port)
        cdp.close()
        proc.terminate()
        return 0
    except Exception:
        proc.terminate()
        stop_own_browser(port)
        clear_session(port)
        raise


def attach(args) -> tuple:
    st = read_state(args.port)
    cdp = open_target_page(args.port, timeout=10, match=st.get("host"))
    if cdp is None:
        raise SystemExit(f"no bridge page on port {args.port}; start one with run --leave-running")
    return cdp, st


def _ensure_target(cdp, selector: str, state: dict | None = None,
                   timeout: float = 4.0) -> bool:
    """Wait for a sprite-qualified selector's target, reloading if it was clobbered.

    The production editor initialises its own default project asynchronously and can
    replace the unsaved project loaded by `run`, so an immediate get/set would
    otherwise read nothing. Re-resolve the VM, and re-load the session's sb3
    (bounded) so later commands are self-healing.
    """
    if "." not in selector:
        return True
    name = selector.split(".", 1)[0]
    if name in ("stage", "Stage"):
        return True
    sb3 = None
    if state and state.get("sb3") and Path(str(state["sb3"])).exists():
        sb3 = Path(str(state["sb3"]))
    deadline = time.monotonic() + timeout
    reloads = 2
    while True:
        try:
            if cdp.evaluate(f"!!window.__bridge.findTarget({json.dumps(name)})"):
                return True
        except (CDPError, TimeoutError, OSError):
            return False
        if time.monotonic() >= deadline:
            return False
        inject_bridge(cdp, timeout=3)
        if sb3 is not None and reloads:
            reloads -= 1
            try:
                log(f"reloading {sb3.name}: the page replaced the project")
                encoded = load_sb3_b64(sb3)
                cdp.evaluate(load_project_script(encoded), await_promise=True, timeout=120)
                cdp.evaluate("window.__bridge.start()")
                cdp.evaluate("window.__bridge.greenFlag()")
            except (CDPError, TimeoutError, OSError):
                pass
        # Advance the VM by frames instead of sleeping before re-checking.
        try:
            cdp.evaluate("window.__bridge.waitFrames(2, 1000)",
                         await_promise=True, timeout=3)
        except (CDPError, TimeoutError, OSError):
            return False


def cmd_targets(args) -> int:
    """List the loaded target (sprite) names, to confirm the project was replaced."""
    cdp, _ = attach(args)
    try:
        names = cdp.evaluate("window.__bridge.targetNames()", timeout=bridge_host.READ_TIMEOUT)
        log({"targets": names if isinstance(names, list) else []})
    finally:
        cdp.close()
    return 0


def _read_selector(cdp, session, selector: str):
    """Read one selector using gsdev's shared `_var_js` — one implementation, not two.

    `ns="window.__bridge"` points the same JS at the injected shim, so the result is
    gsdev's shape (`{value, target, name, index}`), including for `@` pseudo-metrics.
    """
    target, name, index = bridge_state.parse_selector(selector)
    js = bridge_state.var_js(target, name, index, ns="window.__bridge")
    value = cdp.evaluate(js, timeout=bridge_host.READ_TIMEOUT)
    if isinstance(value, dict) and "error" in value and "." in selector:
        # The page may still be mounting its own project; make sure our target is
        # there, then retry once (a genuinely missing name still reports the error).
        _ensure_target(cdp, selector, session)
        value = cdp.evaluate(js, timeout=bridge_host.READ_TIMEOUT)
    return value


def _write_selector(cdp, session, selector: str, value):
    """Write one selector with gsdev's shared `_var_js` (same result shape as gsdev)."""
    target, name, index = bridge_state.parse_selector(selector)
    js = bridge_state.var_js(target, name, index, value, ns="window.__bridge")
    result = cdp.evaluate(js, timeout=bridge_host.READ_TIMEOUT) or {}
    if isinstance(result, dict) and "error" in result and "." in selector:
        _ensure_target(cdp, selector, session)
        result = cdp.evaluate(js, timeout=bridge_host.READ_TIMEOUT) or {}
    return result


def cmd_get(args) -> int:
    cdp, session = attach(args)
    try:
        out = {}
        for selector in args.selectors:
            out[selector] = _read_selector(cdp, session, selector)
        payload = json.dumps(out)
        if getattr(args, "out", None):
            # Same contract as gsdev's `get --out`: UTF-8 file + a one-line summary.
            path = Path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload + "\n", encoding="utf-8", newline="\n")
            counts = {}
            for sel, entry in out.items():
                item = entry.get("value") if isinstance(entry, dict) else entry
                if isinstance(item, list):
                    counts[sel] = len(item)
            log({"out": str(path), "bytes": path.stat().st_size, "items": counts})
        else:
            log(out)
    finally:
        cdp.close()
    return 0


def cmd_set(args) -> int:
    cdp, session = attach(args)
    try:
        # Same result shape and exit status as gsdev's `set`.
        result = _write_selector(cdp, session, args.selector, json.loads(args.value))
        log({"selector": args.selector, **result})
        return 1 if result.get("error") else 0
    finally:
        cdp.close()


def cmd_set_batch(args) -> int:
    cdp, session = attach(args)
    try:
        # One evaluate for the whole batch, exactly as gsdev does it.
        bridge_state.apply_batch(cdp, args.assignments, ns="window.__bridge", emit=log)
        return 0
    finally:
        cdp.close()


def cmd_click(args) -> int:
    cdp, _ = attach(args)
    try:
        # Same down/hold/up shape as gsdev (shared _hold_input, bridge namespace).
        cdp.evaluate(bridge_page.mouse_js(args.x, args.y, None, ns="window.__bridge"))
        cdp.evaluate(bridge_page.mouse_js(args.x, args.y, True, ns="window.__bridge"))
        bridge_page.hold_input(cdp, float(args.settle), ns="window.__bridge")
        cdp.evaluate(bridge_page.mouse_js(args.x, args.y, False, ns="window.__bridge"))
        bridge_page.hold_input(cdp, 0.0, ns="window.__bridge")
        log({"click": [args.x, args.y]})
    finally:
        cdp.close()
    return 0


def cmd_mouse(args) -> int:
    cdp, _ = attach(args)
    try:
        state = True if args.down else (False if args.up else None)
        cdp.evaluate(bridge_page.mouse_js(args.x, args.y, state, ns="window.__bridge"))
        log({"mouse": [args.x, args.y], "down": state})
    finally:
        cdp.close()
    return 0


def cmd_key(args) -> int:
    cdp, _ = attach(args)
    key = bridge_page.input_key_name(args.name)
    try:
        # Same result shape and modes as gsdev: {"key": <resolved>, "mode": …}.
        if args.down:
            cdp.evaluate(bridge_page.key_js(key, True, ns="window.__bridge"))
            mode = "down"
        elif args.up:
            # release only; sending a press first would fire another key-pressed hat
            cdp.evaluate(bridge_page.key_js(key, False, ns="window.__bridge"))
            mode = "up"
        else:
            cdp.evaluate(bridge_page.key_js(key, True, ns="window.__bridge"))
            bridge_page.hold_input(cdp, float(args.settle), ns="window.__bridge")
            cdp.evaluate(bridge_page.key_js(key, False, ns="window.__bridge"))
            mode = "press"
        log({"key": key, "mode": mode})
    finally:
        cdp.close()
    return 0


def cmd_test(args) -> int:
    """Run session files as tests against the *live* page.

    The bridge cannot build: it uses the project already loaded on the page. `--mode`,
    `--no-build`, `--no-reload`, `--software` and the screenshot flags are accepted for
    syntax parity with gsdev but have no effect here. Each file reuses the session
    runner, so the same ops/asserts work in both tools.
    """
    build_only = [f for f in ("mode", "no_build", "no_reload", "software")
                  if getattr(args, f, None)]
    if build_only:
        log("note: the bridge runs the page's loaded project; "
            "--mode/--no-build/--no-reload/--software have no effect")
    files = [str(f) for f in args.files]
    if args.manifest:
        try:
            items, errors = gsdev.discover_tests(Path(args.manifest))
        except Exception as exc:  # noqa: BLE001
            log(f"manifest: {type(exc).__name__}: {exc}")
            return 1
        if errors:
            for error in errors:
                log(f"manifest: {error}")
            return 1
        if args.list:
            print(json.dumps({
                "schema": gsdev.MANIFEST_SCHEMA,
                "tests": [{"id": t["id"], "label": t["label"], "session": str(t["path"]),
                           "root": str(t["root"]), "build_mode": t.get("buildMode")}
                          for t in items],
            }, separators=(",", ":")), flush=True)
            return 0
        files += [str(t["path"]) for t in items]
    if not files:
        log("test: no session files (pass FILE... or --manifest)")
        return 1
    results = []
    failures = 0
    for path in files:
        sub = argparse.Namespace(**vars(args))
        sub.file = path
        rc = cmd_session(sub)
        failures += 0 if rc == 0 else 1
        results.append({"file": path, "ok": rc == 0})
    if args.json:
        print(json.dumps({"tests": results, "failures": failures},
                         separators=(",", ":")), flush=True)
    else:
        log(f"{len(files) - failures}/{len(files)} session files passed")
    return 1 if failures else 0


def cmd_watch(args) -> int:
    """gsdev's driver against the bridge page (its own watch global)."""
    cdp, _ = attach(args)
    try:
        bridge_state.watch_run(cdp, args.selectors, args.duration * 1000.0, args.interval,
                         ns="window.__bridge", glob="window.__bridgeWatch", emit=log)
    finally:
        try:
            cdp.evaluate(bridge_state.stop_watch_js("window.__bridgeWatch"), timeout=3)
        except Exception:
            pass
        cdp.close()
    return 0


def cmd_until(args) -> int:
    cdp, _ = attach(args)
    try:
        spec = bridge_state.spec(args.selector)
        value = bridge_state.parse_value(args.value)
        script = (
            f"window.__bridge.until({json.dumps(spec)}, {json.dumps(args.op)}, "
            f"{json.dumps(value)}, {'true' if args.pause else 'false'}, {int(args.max)})"
        )
        log({"until": cdp.evaluate(script, timeout=bridge_host.READ_TIMEOUT)})
    finally:
        cdp.close()
    return 0


def cmd_record(args) -> int:
    cdp, _ = attach(args)
    try:
        specs = [bridge_state.spec(selector) for selector in args.selectors]
        result = cdp.evaluate(f"window.__bridge.record({json.dumps(specs)}, {int(args.max)})",
                              timeout=bridge_host.READ_TIMEOUT)
        log({"record": result})
    finally:
        cdp.close()
    return 0


def cmd_stop_record(args) -> int:
    cdp, _ = attach(args)
    try:
        log(cdp.evaluate("window.__bridge.stopRecord()", timeout=bridge_host.READ_TIMEOUT))
    finally:
        cdp.close()
    return 0


def cmd_trace(args) -> int:
    cdp, _ = attach(args)
    try:
        result = cdp.evaluate(f"window.__bridge.trace({'true' if args.clear else 'false'})",
                              timeout=bridge_host.READ_TIMEOUT)
        log(result)
    finally:
        cdp.close()
    return 0


def cmd_broadcast_wait(args) -> int:
    cdp, _ = attach(args)
    try:
        result = cdp.evaluate(
            bridge_page.broadcast_wait_js(args.name, args.timeout, ns="window.__bridge"),
            await_promise=True, timeout=args.timeout / 1000.0 + 5,
        ) or {}
        if result.get("ok"):
            log({"broadcast": args.name, "threads": result.get("threads", 0),
                 "waitedFrames": result.get("waitedFrames")})
            return 0
        print(f"[ASSERT FAIL] broadcast_wait {args.name} timed out", flush=True)
        return 1
    finally:
        cdp.close()


def cmd_props(args) -> int:
    cdp, _ = attach(args)
    try:
        result = cdp.evaluate(f"window.__bridge.props({json.dumps(args.target)})",
                              timeout=bridge_host.READ_TIMEOUT) or {}
        log(result)
        return 1 if result.get("error") else 0
    finally:
        cdp.close()


def cmd_prop(args) -> int:
    cdp, _ = attach(args)
    try:
        if args.value is None:
            script = (f"window.__bridge.getProp({json.dumps(args.target)}, "
                      f"{json.dumps(args.name)})")
        else:
            value = bridge_state.parse_value(args.value)
            script = (f"window.__bridge.setProp({json.dumps(args.target)}, "
                      f"{json.dumps(args.name)}, {json.dumps(value)})")
        result = cdp.evaluate(script, timeout=bridge_host.READ_TIMEOUT) or {}
        log({"target": args.target, "prop": args.name, **result})
        return 1 if result.get("error") else 0
    finally:
        cdp.close()


def cmd_render(args) -> int:
    cdp, _ = attach(args)
    try:
        log(cdp.evaluate(bridge_page.events_js(ns="window.__bridge")))
    finally:
        cdp.close()
    return 0


def cmd_pause(args) -> int:
    cdp, _ = attach(args)
    try:
        cdp.evaluate(bridge_page.pause_js(ns="window.__bridge"))
        log({"paused": True, "frame": bridge_page.current_frame(cdp, js="window.__bridge.frame()")})
    finally:
        cdp.close()
    return 0


def cmd_resume(args) -> int:
    cdp, _ = attach(args)
    try:
        cdp.evaluate(bridge_page.resume_js(ns="window.__bridge"))
        log({"resumed": True, "frame": bridge_page.current_frame(cdp, js="window.__bridge.frame()")})
    finally:
        cdp.close()
    return 0


def cmd_step(args) -> int:
    cdp, _ = attach(args)
    try:
        cdp.evaluate(bridge_page.pause_js(ns="window.__bridge"))
        before = bridge_page.current_frame(cdp, js="window.__bridge.frame()")
        frame = int(cdp.evaluate(bridge_page.step_js(args.count, ns="window.__bridge")))
        log({"stepped": frame - before, "frame": frame})
    finally:
        cdp.close()
    return 0


def cmd_broadcast(args) -> int:
    cdp, _ = attach(args)
    try:
        result = cdp.evaluate(bridge_page.broadcast_js(args.name, ns="window.__bridge"))
        log({"broadcast": args.name, "threads": (result or {}).get("threads", 0)})
    finally:
        cdp.close()
    return 0


def cmd_frame(args) -> int:
    cdp, _ = attach(args)
    try:
        log({"frame": bridge_page.current_frame(cdp, js="window.__bridge.frame()")})
    finally:
        cdp.close()
    return 0


def cmd_clones(args) -> int:
    cdp, _ = attach(args)
    try:
        log({"clones": cdp.evaluate("window.__bridge.clones()", timeout=bridge_host.READ_TIMEOUT)})
    finally:
        cdp.close()
    return 0


def cmd_stop(args) -> int:
    cdp, _ = attach(args)
    try:
        gsdev.stop_host(cdp, ns="window.__bridge")
        log("project stopped")
    finally:
        cdp.close()
    return 0


def cmd_pixel(args) -> int:
    cdp, _ = attach(args)
    try:
        # gsdev's shape: {rgba, hex, x, y}.
        info = cdp.evaluate(f"window.__bridge.pixelInfo({args.x}, {args.y})", timeout=10)
        if info is None:
            log({"error": "no pixel (renderer unavailable)"})
            return 1
        log(info)
    finally:
        cdp.close()
    return 0


def cmd_screenshot(args) -> int:
    cdp, _ = attach(args)
    try:
        rect = cdp.evaluate("window.__bridge.stageRect()")
        data = cdp.call("Page.captureScreenshot", {"format": "png", "clip": {**rect, "scale": 1}}, timeout=20)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(data["data"]))
        log({"screenshot": str(out), "size": f"{int(rect['width'])}x{int(rect['height'])}"})
    finally:
        cdp.close()
    return 0


def cmd_logs(args) -> int:
    cdp, _ = attach(args)
    try:
        entries = cdp.evaluate("window.__bridge.logs()")
        if args.json:
            log({"logs": entries})
        else:
            for e in entries:
                log(f"[LOG {e['sprite']}] {e['value']}")
    finally:
        cdp.close()
    return 0


def cmd_errors(args) -> int:
    cdp, _ = attach(args)
    try:
        log(cdp.evaluate("window.__bridge.errors()", timeout=bridge_host.READ_TIMEOUT))
    finally:
        cdp.close()
    return 0


def cmd_inspect(args) -> int:
    cdp, _ = attach(args)
    try:
        log(cdp.evaluate(f"window.__bridge.inspect({json.dumps(args.target)})", timeout=bridge_host.READ_TIMEOUT))
    finally:
        cdp.close()
    return 0


def cmd_gpu(args) -> int:
    cdp, _ = attach(args)
    try:
        log(cdp.evaluate("window.__bridge.gpu()"))
    finally:
        cdp.close()
    return 0


def cmd_perf(args) -> int:
    cdp, _ = attach(args)
    try:
        log(cdp.evaluate("window.__bridge.perf()"))
    finally:
        cdp.close()
    return 0


def _bridge_has_profiler(cdp) -> bool:
    return bool(cdp.evaluate("!!(window.__bridge && window.__bridge.setProfiling)"))


def cmd_setprofiling(args) -> int:
    cdp, _ = attach(args)
    try:
        on = args.mode == "on"
        result = cdp.evaluate(f"window.__bridge.setProfiling({str(on).lower()})")
        log({"profiling": bool(result)})
    finally:
        cdp.close()
    return 0


def cmd_profilereset(args) -> int:
    cdp, _ = attach(args)
    try:
        log({"reset": bool(cdp.evaluate("window.__bridge.profileReset()"))})
    finally:
        cdp.close()
    return 0


def cmd_profiling(args) -> int:
    cdp, _ = attach(args)
    try:
        report = cdp.evaluate("window.__bridge.profile()")
        if not isinstance(report, dict):
            log("no profiler on this bridge (reload the page with this gsbridge)")
            return 1
        gsdev._print_profile(report, args.top)
    finally:
        cdp.close()
    return 0


def cmd_profile(args) -> int:
    cdp, _ = attach(args)
    try:
        if not _bridge_has_profiler(cdp):
            log("no profiler on this bridge (reload the page with this gsbridge)")
            return 1
        cdp.evaluate("window.__bridge.setProfiling(true)")
        cdp.evaluate("window.__bridge.profileReset()")
        cdp.evaluate("window.__bridge.start()")
        if not args.no_restart:
            cdp.evaluate("window.__bridge.greenFlag()")
        time.sleep(max(0.2, args.seconds))
        report = cdp.evaluate("window.__bridge.profile()") or {}
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        gsdev._print_profile(report, args.top)
        if args.off:
            cdp.evaluate("window.__bridge.setProfiling(false)")
    finally:
        cdp.close()
    return 0


def cmd_restart(args) -> int:
    cdp, _ = attach(args)
    try:
        cdp.evaluate("window.__bridge.start()")
        cdp.evaluate("window.__bridge.greenFlag()")
        log({"restarted": True})
    finally:
        cdp.close()
    return 0


def cmd_wait_frame(args) -> int:
    """Wait for N runtime frames (event-driven; mirrors gsdev's wait_frame)."""
    cdp, _ = attach(args)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(
            f"window.__bridge.waitFrames({int(args.count)}, {int(args.timeout)})",
            await_promise=True, timeout=args.timeout / 1000.0 + 5,
        ) or {}
        log(json.dumps(result))
        return 0 if result.get("ok") else 1
    finally:
        cdp.close()


def cmd_wait_pixel(args) -> int:
    """Wait until a stage pixel equals HEX (event-driven; mirrors gsdev)."""
    cdp, _ = attach(args)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(
            f"window.__bridge.waitPixel({float(args.x)}, {float(args.y)}, "
            f"{json.dumps(args.hex)}, {int(args.timeout)})",
            await_promise=True, timeout=args.timeout / 1000.0 + 5,
        ) or {}
        log(json.dumps(result))
        return 0 if result.get("ok") else 1
    finally:
        cdp.close()


def cmd_wait_stopped(args) -> int:
    """Wait for the VM's stop event (every script finished), event-driven."""
    cdp, _ = attach(args)
    if cdp is None:
        return 1
    try:
        since = cdp.evaluate("window.__bridge.stopSeq()")
        result = cdp.evaluate(
            f"window.__bridge.waitStopped({since}, {int(args.timeout)})",
            await_promise=True, timeout=args.timeout / 1000.0 + 5,
        ) or {}
        log(json.dumps(result))
        return 0 if result.get("stopped") else 1
    finally:
        cdp.close()


def cmd_wait_until(args) -> int:
    """Wait for a variable condition; the predicate runs in-page, once per VM step."""
    cdp, _ = attach(args)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(
            f"window.__bridge.waitUntil({json.dumps(args.selector)}, "
            f"{json.dumps(args.op)}, {json.dumps(args.value)}, {int(args.timeout)})",
            await_promise=True, timeout=args.timeout / 1000.0 + 5,
        ) or {}
        log(json.dumps(result))
        return 0 if result.get("ok") else 1
    finally:
        cdp.close()


def cmd_close(args) -> int:
    if not read_state(args.port):
        record = read_session()
        if record and record.get("port") == args.port:
            write_state(args.port, **{k: v for k, v in record.items()
                                      if k in ("host", "target", "url", "pid", "profile", "token")})
    stop_own_browser(args.port)   # only our browser; never gsdev's host server
    reap_orphans(session_only=True, kill_live=True)
    clear_session(args.port)
    return 0


def cmd_expect_no_errors(args) -> int:
    cdp, _ = attach(args)
    try:
        errs = cdp.evaluate("window.__bridge.errors()", timeout=bridge_host.READ_TIMEOUT)
        log(errs)
        return 1 if int(errs.get("count", 0)) else 0
    finally:
        cdp.close()


# ---------------------------------------------------------------------------
# session script runner
# ---------------------------------------------------------------------------
def cmd_session(args) -> int:
    cdp, session = attach(args)
    bridge_host.apply_cpu_throttle(cdp, getattr(args, "cpu", 0.0))
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    failures = 0
    try:
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            verb = parts[0]
            try:
                if verb == "get":
                    log({v: _read_selector(cdp, session, v) for v in parts[1:]})
                elif verb == "set" and len(parts) >= 3:
                    log({"selector": parts[1],
                         **_write_selector(cdp, session, parts[1],
                                           json.loads(" ".join(parts[2:])))})
                elif verb == "set_batch":
                    # gsdev's own one-evaluate batch (it logs the same shape for us).
                    bridge_state.apply_batch(cdp, parts[1:], ns="window.__bridge", emit=log)
                elif verb == "click" and len(parts) == 3:
                    x, y = float(parts[1]), float(parts[2])
                    cdp.evaluate(bridge_page.mouse_js(x, y, None, ns="window.__bridge"))
                    cdp.evaluate(bridge_page.mouse_js(x, y, True, ns="window.__bridge"))
                    bridge_page.hold_input(cdp, 0.0, ns="window.__bridge")
                    cdp.evaluate(bridge_page.mouse_js(x, y, False, ns="window.__bridge"))
                    bridge_page.hold_input(cdp, 0.0, ns="window.__bridge")
                    log(f"[click {parts[1]} {parts[2]}]")
                elif verb == "key" and len(parts) >= 2:
                    key = bridge_page.input_key_name(parts[1])
                    mode = parts[2] if len(parts) > 2 else "press"
                    if mode == "down":
                        cdp.evaluate(bridge_page.key_js(key, True, ns="window.__bridge"))
                    elif mode == "up":
                        cdp.evaluate(bridge_page.key_js(key, False, ns="window.__bridge"))
                    else:
                        cdp.evaluate(bridge_page.key_js(key, True, ns="window.__bridge"))
                        bridge_page.hold_input(cdp, 0.0, ns="window.__bridge")
                        cdp.evaluate(bridge_page.key_js(key, False, ns="window.__bridge"))
                    log(f"[key {parts[1]} {mode}]")
                elif verb == "sleep":
                    time.sleep(float(parts[1]) / 1000.0)
                elif verb == "waitframe":
                    timeout = float(parts[2]) if len(parts) > 2 else 5000.0
                    result = cdp.evaluate(
                        f"window.__bridge.waitFrames({int(parts[1])}, {int(timeout)})",
                        await_promise=True, timeout=timeout / 1000.0 + 5,
                    ) or {}
                    if result.get("ok"):
                        log(json.dumps({"waitframe": int(parts[1]), "frame": result.get("frame")}))
                    else:
                        log(f"[ASSERT FAIL] waitframe {parts[1]} timed out")
                        failures += 1
                elif verb == "waitpixel" and len(parts) >= 4:
                    timeout = float(parts[4]) if len(parts) > 4 else 5000.0
                    result = cdp.evaluate(
                        f"window.__bridge.waitPixel({float(parts[1])}, {float(parts[2])}, "
                        f"{json.dumps(parts[3])}, {int(timeout)})",
                        await_promise=True, timeout=timeout / 1000.0 + 5,
                    ) or {}
                    if result.get("ok"):
                        log(json.dumps({"waitpixel": [float(parts[1]), float(parts[2])],
                                        "pixel": result.get("pixel"), "frame": result.get("frame")}))
                    else:
                        log(f"[ASSERT FAIL] waitpixel {parts[1]} {parts[2]} {parts[3]} timed out")
                        failures += 1
                elif verb == "waituntil" and len(parts) >= 4:
                    timeout = float(parts[4]) if len(parts) > 4 else 5000.0
                    result = cdp.evaluate(
                        f"window.__bridge.waitUntil({json.dumps(parts[1])}, "
                        f"{json.dumps(parts[2])}, {json.dumps(parts[3])}, {int(timeout)})",
                        await_promise=True, timeout=timeout / 1000.0 + 5,
                    ) or {}
                    if result.get("ok"):
                        log(json.dumps({"waituntil": parts[1], "value": result.get("value"),
                                        "frame": result.get("frame")}))
                    else:
                        log(f"[ASSERT FAIL] waituntil {parts[1]} {parts[2]} {parts[3]} timed out")
                        failures += 1
                elif verb == "waitstopped":
                    timeout = float(parts[1]) if len(parts) > 1 else 60000.0
                    result = cdp.evaluate(
                        f"window.__bridge.waitStopped(0, {int(timeout)})",
                        await_promise=True, timeout=timeout / 1000.0 + 5,
                    ) or {}
                    if result.get("stopped"):
                        log(json.dumps({"waitstopped": result}))
                    else:
                        log("[ASSERT FAIL] waitstopped timed out")
                        failures += 1
                elif verb == "restart":
                    cdp.evaluate("window.__bridge.start()")
                    cdp.evaluate("window.__bridge.greenFlag()")
                elif verb == "stop":
                    cdp.evaluate("window.__bridge.stopAll()")
                elif verb == "frame":
                    log({"frame": cdp.evaluate("window.__bridge.frame()")})
                elif verb == "pixel" and len(parts) >= 3:
                    log({"pixel": cdp.evaluate(f"window.__bridge.pixel({parts[1]}, {parts[2]})", await_promise=True, timeout=10)})
                elif verb == "expect" and len(parts) >= 4:
                    selector, op = parts[1], parts[2]
                    want = " ".join(parts[3:])
                    got = cdp.evaluate(f"window.__bridge.get({json.dumps(selector)})", timeout=bridge_host.READ_TIMEOUT)
                    ok = _compare(got, op, want)
                    log(f"[ASSERT {'ok' if ok else 'FAIL'}] {selector} {op} {want} (got {got!r})")
                    failures += 0 if ok else 1
                elif verb == "expectpixel" and len(parts) >= 4:
                    got = cdp.evaluate(f"window.__bridge.pixel({parts[1]}, {parts[2]})", await_promise=True, timeout=10)
                    ok = str(got).lower() == parts[3].lower()
                    log(f"[ASSERT {'ok' if ok else 'FAIL'}] pixel {parts[1]} {parts[2]} == {parts[3]} (got {got})")
                    failures += 0 if ok else 1
                elif verb == "errors":
                    log(cdp.evaluate("window.__bridge.errors()", timeout=bridge_host.READ_TIMEOUT))
                elif verb == "expect_no_errors":
                    errs = cdp.evaluate("window.__bridge.errors()", timeout=bridge_host.READ_TIMEOUT)
                    ok = int(errs.get("count", 0)) == 0
                    log(f"[ASSERT {'ok' if ok else 'FAIL'}] expect_no_errors (count={errs.get('count')})")
                    failures += 0 if ok else 1
                elif verb == "perf":
                    log(cdp.evaluate("window.__bridge.perf()"))
                elif verb == "setprofiling" and len(parts) >= 2:
                    on = parts[1].lower() in ("on", "true", "1")
                    result = cdp.evaluate(f"window.__bridge.setProfiling({str(on).lower()})")
                    log(f"[profiling {'on' if result else 'off'}]")
                elif verb == "profilereset":
                    cdp.evaluate("window.__bridge.profileReset()")
                    log("[profilereset]")
                elif verb == "profiling":
                    report = cdp.evaluate("window.__bridge.profile()") or {}
                    log(f"[profiling on={bool(cdp.evaluate('window.__bridge.profilingOn()'))} "
                        f"steps={report.get('steps', 0)}]")
                elif verb == "gpu":
                    log(cdp.evaluate("window.__bridge.gpu()"))
                elif verb == "log":
                    log(" ".join(parts[1:]))
                else:
                    log(f"unknown verb: {verb}")
                    failures += 1
            except Exception as exc:  # keep the session going, but report
                log({"verb": verb, "error": str(exc)})
                failures += 1
    finally:
        cdp.close()
    return 1 if failures else 0


def _num(v):
    try:
        return float(v), True
    except (TypeError, ValueError):
        return v, False


def _compare(got, op, want) -> bool:
    g, gn = _num(got)
    w, wn = _num(want)
    if gn and wn:
        return {"==": g == w, "!=": g != w, ">": g > w, "<": g < w, ">=": g >= w, "<=": g <= w}.get(op, False)
    return {"==": str(got) == want, "!=": str(got) != want}.get(op, False)


def add_port(p):
    p.add_argument("--port", type=int, default=None,
                   help="CDP port (default: this session's last run, else 9400)")


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser.

    Exposed (rather than built inside ``main``) so the cross-tool parity guard can
    compare usage strings against gsdev's parser.
    """
    parser = argparse.ArgumentParser(prog="gsbridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="launch a target, inject the bridge, optionally run")
    run.add_argument("--target", choices=["localhost", "github", "editor", "production", "url"], default="localhost")
    run.add_argument("--url")
    run.add_argument("--project-id")
    run.add_argument("--sb3")
    run.add_argument("--project", help="goboscript project DIR to build then load")
    run.add_argument("--browser", help="browser executable (default: discovered)")
    run.add_argument("--headless", action="store_true")
    run.add_argument("--duration", type=float, default=0.0, help="seconds to stream logs")
    run.add_argument("--leave-running", action="store_true")
    run.add_argument("--no-start", action="store_true", help="do not green-flag")
    run.add_argument("--cpu", type=float, default=0.0,
                     help="CPU throttling rate (e.g. 4 = slow phone)")
    run.add_argument("--ready-timeout", type=float, default=15.0)
    add_port(run)
    run.set_defaults(func=cmd_run)

    get = sub.add_parser("get", help="read variables/lists/pseudo-metrics")
    add_port(get)
    get.add_argument("selectors", nargs="+", help="sprite.variable (bare name = stage global)")
    get.add_argument("--out", help="write the JSON to FILE (UTF-8) instead of stdout")
    get.set_defaults(func=cmd_get)

    tgts = sub.add_parser("targets", help="list loaded target (sprite) names")
    add_port(tgts); tgts.set_defaults(func=cmd_targets)

    frame_p = sub.add_parser("frame", help="print the runtime frame counter")
    add_port(frame_p)
    frame_p.set_defaults(func=cmd_frame)

    clones_p = sub.add_parser("clones", help="print the clone count per sprite")
    add_port(clones_p)
    clones_p.set_defaults(func=cmd_clones)

    stop_p = sub.add_parser("stop", help="stop the running project")
    add_port(stop_p)
    stop_p.set_defaults(func=cmd_stop)

    render_p = sub.add_parser("render", help="frame/render counters and event totals")
    add_port(render_p)
    render_p.set_defaults(func=cmd_render)

    pause_p = sub.add_parser("pause", help="pause the runtime for manual stepping")
    add_port(pause_p)
    pause_p.set_defaults(func=cmd_pause)

    resume_p = sub.add_parser("resume", help="resume the runtime after pause/step")
    add_port(resume_p)
    resume_p.set_defaults(func=cmd_resume)

    step_p = sub.add_parser("step", help="pause and advance exactly N frames")
    add_port(step_p)
    step_p.add_argument("count", type=int)
    step_p.set_defaults(func=cmd_step)

    broadcast_p = sub.add_parser(
        "broadcast", help="start the 'when I receive' hats for a message"
    )
    add_port(broadcast_p)
    broadcast_p.add_argument("name", help="broadcast message name")
    broadcast_p.set_defaults(func=cmd_broadcast)

    props_p = sub.add_parser("props", help="print a target's properties")
    add_port(props_p)
    props_p.add_argument("target", help="sprite name or name#N")
    props_p.set_defaults(func=cmd_props)

    prop_p = sub.add_parser("prop", help="read or write one target property")
    add_port(prop_p)
    prop_p.add_argument("target", help="sprite name or name#N")
    prop_p.add_argument("name", help="x, y, direction, size, visible, costume, ...")
    prop_p.add_argument("value", nargs="?", default=None, help="JSON value; omit to read")
    prop_p.set_defaults(func=cmd_prop)

    bwait_p = sub.add_parser(
        "broadcast_wait", help="broadcast a message, then wait for its handlers"
    )
    add_port(bwait_p)
    bwait_p.add_argument("name", help="broadcast message name")
    bwait_p.add_argument("--timeout", type=int, default=5000, help="timeout in ms")
    bwait_p.set_defaults(func=cmd_broadcast_wait)

    test_p = sub.add_parser(
        "test", help="run session files (or a test manifest) as tests"
    )
    add_port(test_p)
    test_p.add_argument("--mode", choices=["debug", "release"], default="debug",
                        help="build mode (debug by default; no effect on the bridge)")
    test_p.add_argument("files", nargs="*", help="session files to run as tests")
    test_p.add_argument("--manifest", default="", help="run the tests in this manifest")
    test_p.add_argument(
        "--list", action="store_true",
        help="validate and list the manifest\'s tests without running (requires --manifest)",
    )
    test_p.add_argument(
        "--artifacts", default="",
        help="directory for failure screenshots (default debug/)",
    )
    test_p.add_argument("--timeout", type=int, default=300, help="per-file timeout in seconds")
    test_p.add_argument("--no-build", action="store_true", help="skip build (no effect on the bridge)")
    test_p.add_argument(
        "--no-reload", action="store_true", help="reuse the running project (no per-file reload)"
    )
    test_p.add_argument(
        "--no-screenshots", action="store_true", help="do not save a screenshot on failure"
    )
    test_p.add_argument("--headless", action="store_true", help="no visible browser window")
    test_p.add_argument("--software", action="store_true",
                        help="force software GL (no effect on the bridge)")
    test_p.add_argument("--json", action="store_true", help="machine-readable output")
    test_p.set_defaults(func=cmd_test)

    watch_p = sub.add_parser("watch", help="sample variables every frame while running")
    add_port(watch_p)
    watch_p.add_argument("selectors", nargs="+", help="sprite.variable (bare name = stage global)")
    watch_p.add_argument("--duration", type=float, default=5.0, help="stop after N seconds")
    watch_p.add_argument("--interval", type=int, default=20, help="sampling interval in ms")
    watch_p.set_defaults(func=cmd_watch)

    record_p = sub.add_parser(
        "record", help="record variables on every frame (event-driven, no polling)"
    )
    add_port(record_p)
    record_p.add_argument("selectors", nargs="+")
    record_p.add_argument("--max", type=int, default=100000, help="stop after N rows")
    record_p.set_defaults(func=cmd_record)

    stop_record_p = sub.add_parser(
        "stop_record", help="stop the frame recorder (keeps the rows)"
    )
    add_port(stop_record_p)
    stop_record_p.set_defaults(func=cmd_stop_record)

    trace_p = sub.add_parser("trace", help="dump the recorded per-frame rows")
    add_port(trace_p)
    trace_p.add_argument("--clear", action="store_true", help="clear rows after reading")
    trace_p.set_defaults(func=cmd_trace)

    until_p = sub.add_parser(
        "until", help="record the exact frame a condition first holds"
    )
    add_port(until_p)
    until_p.add_argument("selector")
    until_p.add_argument("op", choices=["==", "!=", ">", "<", ">=", "<="])
    until_p.add_argument("value")
    until_p.add_argument("--pause", action="store_true", help="pause exactly on the hit frame")
    until_p.add_argument("--max", type=int, default=100000, help="give up after N frames")
    until_p.set_defaults(func=cmd_until)

    setp = sub.add_parser("set", help="write a variable/list element")
    setp.add_argument("selector"); setp.add_argument("value")
    add_port(setp); setp.set_defaults(func=cmd_set)

    sb = sub.add_parser("set_batch", help="write several in one evaluate")
    sb.add_argument("assignments", nargs="+")
    add_port(sb); sb.set_defaults(func=cmd_set_batch)

    click = sub.add_parser("click", help="click at stage coords")
    add_port(click)
    click.add_argument("x", type=float)
    click.add_argument("y", type=float)
    click.add_argument("--settle", type=int, default=80, help="ms between down and up")
    click.set_defaults(func=cmd_click)

    mouse = sub.add_parser("mouse", help="press/release at stage coords")
    add_port(mouse)
    mouse.add_argument("x", type=float)
    mouse.add_argument("y", type=float)
    mouse_group = mouse.add_mutually_exclusive_group()
    mouse_group.add_argument("--down", action="store_true", help="press the mouse button")
    mouse_group.add_argument("--up", action="store_true", help="release the mouse button")
    mouse.set_defaults(func=cmd_mouse)

    key = sub.add_parser("key", help="press/hold/release a key")
    add_port(key)
    key.add_argument("name", help="space, enter, up/down/left/right, or a character")
    key_group = key.add_mutually_exclusive_group()
    key_group.add_argument("--down", action="store_true", help="hold the key")
    key_group.add_argument("--up", action="store_true", help="release the key")
    key.add_argument("--settle", type=int, default=80, help="ms between down and up")
    key.set_defaults(func=cmd_key)

    pixel = sub.add_parser("pixel", help="read a stage pixel")
    pixel.add_argument("x", type=float); pixel.add_argument("y", type=float)
    add_port(pixel); pixel.set_defaults(func=cmd_pixel)

    shot = sub.add_parser("screenshot", help="save a PNG of the stage")
    shot.add_argument("--out", default="debug/gsbridge.png")
    add_port(shot); shot.set_defaults(func=cmd_screenshot)

    logs = sub.add_parser("logs", help="goboscript log/warn/error captured by the bridge")
    logs.add_argument("--json", action="store_true")
    add_port(logs); logs.set_defaults(func=cmd_logs)

    err = sub.add_parser("errors", help="captured VM/page errors (includes goboscript errors)")
    add_port(err); err.set_defaults(func=cmd_errors)

    ene = sub.add_parser("expect_no_errors", help="exit 1 if any error was captured")
    add_port(ene); ene.set_defaults(func=cmd_expect_no_errors)

    insp = sub.add_parser("inspect", help="targets/variables")
    insp.add_argument("target", nargs="?", default=None)
    add_port(insp); insp.set_defaults(func=cmd_inspect)

    gpu = sub.add_parser("gpu", help="GL renderer / software flag")
    add_port(gpu); gpu.set_defaults(func=cmd_gpu)

    perf = sub.add_parser("perf", help="frame/fps/rendertime snapshot")
    add_port(perf); perf.set_defaults(func=cmd_perf)

    setprof = sub.add_parser("setprofiling", help="enable/disable procedure profiling")
    setprof.add_argument("mode", choices=["on", "off"])
    add_port(setprof); setprof.set_defaults(func=cmd_setprofiling)

    profreset = sub.add_parser("profilereset", help="start a fresh profiling window")
    add_port(profreset); profreset.set_defaults(func=cmd_profilereset)

    prof = sub.add_parser("profiling", help="print the current profiling report")
    prof.add_argument("--top", type=int, default=12)
    add_port(prof); prof.set_defaults(func=cmd_profiling)

    profile = sub.add_parser("profile", help="capture a bounded profiling window")
    profile.add_argument("--seconds", type=float, default=2.0)
    profile.add_argument("--top", type=int, default=12)
    profile.add_argument("--no-restart", action="store_true")
    profile.add_argument("--json", default="")
    profile.add_argument("--off", action="store_true", help="disable profiling afterwards")
    add_port(profile); profile.set_defaults(func=cmd_profile)

    rs = sub.add_parser("restart", help="green flag again")
    add_port(rs); rs.set_defaults(func=cmd_restart)

    ws = sub.add_parser("wait_stopped",
                        help="wait for the VM stop event (all scripts finished)")
    add_port(ws)
    ws.add_argument("--timeout", type=int, default=60000, help="timeout in ms")
    ws.set_defaults(func=cmd_wait_stopped)

    wf = sub.add_parser("wait_frame", help="wait for N runtime frames")
    add_port(wf)
    wf.add_argument("count", type=int)
    wf.add_argument("--timeout", type=int, default=5000, help="timeout in ms")
    wf.set_defaults(func=cmd_wait_frame)

    wp = sub.add_parser("wait_pixel", help="wait until a stage pixel is a colour")
    add_port(wp)
    wp.add_argument("x", type=float)
    wp.add_argument("y", type=float)
    wp.add_argument("hex", help="colour to wait for, e.g. #ff0000")
    wp.add_argument("--timeout", type=int, default=5000, help="timeout in ms")
    wp.set_defaults(func=cmd_wait_pixel)

    wu = sub.add_parser("wait_until",
                        help="wait for a variable condition (evaluated in-page per step)")
    add_port(wu)
    wu.add_argument("selector")
    wu.add_argument("op", choices=["==", "!=", ">", "<", ">=", "<="])
    wu.add_argument("value")
    wu.add_argument("--timeout", type=int, default=5000, help="timeout in ms")
    wu.set_defaults(func=cmd_wait_until)

    close = sub.add_parser("close", help="close the target browser")
    add_port(close); close.set_defaults(func=cmd_close)

    sess = sub.add_parser("session", help="run a verb script from --file or stdin")
    sess.add_argument("--file")
    sess.add_argument("--cpu", type=float, default=0.0,
                      help="CPU throttling rate (e.g. 4 = slow phone)")
    add_port(sess); sess.set_defaults(func=cmd_session)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    is_run = getattr(args, "func", None) is cmd_run
    args.port = resolve_port(getattr(args, "port", None), for_run=is_run)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
