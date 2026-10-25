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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import (  # noqa: E402
    CDP,
    CDPError,
)
import bootstrap  # noqa: E402
from gsdevlib import preflight as gsdev_preflight  # noqa: E402
from gsdevlib import profile as gsdev_profile  # noqa: E402
from gsdevlib import state as gsdev_state  # noqa: E402
from gsdevlib import build as gsdev_build  # noqa: E402
from gsdevlib import host as gsdev_host  # noqa: E402
from gsdevlib import session as gsdev_session  # noqa: E402
from gsdevlib import page as gsdev_page  # noqa: E402

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


HOST_SERVER_PORT = _int_setting("GSDEV_HOST_PORT", "hostPort", _DEFAULT_HOST_PORT)
# Shared across projects; the host server mounts it at /vendor/ (see hostserver.py).
BUNDLES_DIR = bootstrap.bundles_dir()

# Phase 1: how long a freshly launched browser has to expose a debuggable page.
# Deliberately short (5s) so a blocked/broken browser fails fast instead of
# hanging; raise GSDEV_HOST_TIMEOUT for a slow cold start.
HOST_ATTACH_TIMEOUT = float(os.environ.get("GSDEV_HOST_TIMEOUT", "5"))
# How many times to launch the browser if it yields no debuggable page at all. Two
# failure modes look identical from here — a browser that dies during startup, and one
# that handed off to a stale instance still holding this profile, so our CDP port never
# opens (observed intermittently on Windows Edge). `abort_host(port)` clears the second
# before the retry. Raise GSDEV_LAUNCH_ATTEMPTS for a flaky machine.
LAUNCH_ATTEMPTS = int(os.environ.get("GSDEV_LAUNCH_ATTEMPTS", "2"))
# Phase 2: once the page exists, how long it may take to expose window.__host
# (parsing the ~5.8 MB VM on a cold start), and the budget for the individual
# navigation/ready steps inside it. Raise GSDEV_READY_TIMEOUT if needed.
HOST_READY_TIMEOUT = float(os.environ.get("GSDEV_READY_TIMEOUT", "10"))
# Per-attempt CDP timeout inside those windows, so one unresponsive target cannot
# consume the whole budget.
CDP_ATTACH_TIMEOUT = float(os.environ.get("GSDEV_CDP_TIMEOUT", "2"))
# Budget for one live state read/write (`get`/`set`/`inspect`/`props`/`clones`…). These
# are single round trips: reading a whole 200k-item list measured 0.2–0.6 s, so this
# fails fast instead of making an unresponsive page cost 30 s. Raise
# GSDEV_READ_TIMEOUT for a pathologically large list or a saturated page.
READ_TIMEOUT = float(os.environ.get("GSDEV_READ_TIMEOUT", "5"))
# How long to wait for the static host server's ready line. hostserver.py announces
# on stdout immediately after binding (then detaches it), so this is an event wait
# rather than a port-probing loop — and it can stay short: a child that fails to bind
# closes stdout at once, which is detected as an exit instead of a timeout.
HOST_SERVER_TIMEOUT = float(os.environ.get("GSDEV_HOSTSERVER_TIMEOUT", "2"))


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
    # sb2gs needs Python 3.12+; on an older interpreter it can never be present, so
    # do not let that keep triggering setup. A user-installed sb2gs counts too.
    missing_sb2gs = (
        sys.version_info[:2] >= (3, 12)
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


# Host/browser layer (gsdevlib/host.py); config injected once.
gsdev_host.configure(
    DEFAULT_PORT=DEFAULT_PORT,
    PORT_FILE=PORT_FILE,
    STATE_ROOT=STATE_ROOT,
    HOST_DIR=HOST_DIR,
    HOST_PROFILE_ROOT=HOST_PROFILE_ROOT,
    HOST_SERVER_PORT=HOST_SERVER_PORT,
    BUNDLES_DIR=BUNDLES_DIR,
    TOOLS_DIR=TOOLS_DIR,
    PROJECT_ROOT=PROJECT_ROOT,
    HARNESS_FINGERPRINT=HARNESS_FINGERPRINT,
    HOST_ATTACH_TIMEOUT=HOST_ATTACH_TIMEOUT,
    LAUNCH_ATTEMPTS=LAUNCH_ATTEMPTS,
    HOST_READY_TIMEOUT=HOST_READY_TIMEOUT,
    CDP_ATTACH_TIMEOUT=CDP_ATTACH_TIMEOUT,
    READ_TIMEOUT=READ_TIMEOUT,
    HOST_SERVER_TIMEOUT=HOST_SERVER_TIMEOUT,
    _setup_hint=_setup_hint,
    host_bundles_present=host_bundles_present,
    log=log,
)

host_profile_dir = gsdev_host.host_profile_dir
BROWSER_CANDIDATES = gsdev_host.BROWSER_CANDIDATES
port_open = gsdev_host.port_open
free_port = gsdev_host.free_port
resolve_port = gsdev_host.resolve_port
find_editor_pids = gsdev_host.find_editor_pids
terminate_pids = gsdev_host.terminate_pids
find_browser = gsdev_host.find_browser
host_url = gsdev_host.host_url
read_host_state = gsdev_host.read_host_state
write_host_state = gsdev_host.write_host_state
update_host_state = gsdev_host.update_host_state
clear_host_state = gsdev_host.clear_host_state
reap_host_orphans = gsdev_host.reap_host_orphans
probe_host_server = gsdev_host.probe_host_server
ensure_host_server = gsdev_host.ensure_host_server
host_target = gsdev_host.host_target
any_page_target = gsdev_host.any_page_target
prepare_host_profile = gsdev_host.prepare_host_profile
is_browser_dialog = gsdev_host.is_browser_dialog
dismiss_browser_dialogs = gsdev_host.dismiss_browser_dialogs
abort_host = gsdev_host.abort_host
browser_flags = gsdev_host.browser_flags
spawn_browser = gsdev_host.spawn_browser
ensure_host = gsdev_host.ensure_host
open_host = gsdev_host.open_host
apply_cpu_throttle = gsdev_host.apply_cpu_throttle




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


def stop_host(cdp: CDP, ns: str = "window.__host") -> None:
    cdp.evaluate(f"{ns}.stop()")


close_host = gsdev_host.close_host


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


MODE_MACROS = gsdev_build.MODE_MACROS
resolve_mode = gsdev_build.resolve_mode


def preflight_budgets() -> dict:
    """Thresholds: GSDEV_PREFLIGHT_* env -> gobo-agent.json -> conservative default."""
    return gsdev_preflight.budgets(_int_setting)


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
    report = gsdev_preflight.preflight_scan(path, preflight_budgets())
    errors = [f for f in report['findings'] if f['level'] == 'error']
    warnings = [f for f in report['findings'] if f['level'] == 'warning']
    if getattr(args, 'json', False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        gsdev_preflight.print_preflight(report)
    if errors or (getattr(args, 'strict', False) and warnings):
        return 1
    return 0


def build_project_at(root: Path, mode: str = "debug") -> Path:
    goboscript = resolve_goboscript()
    if goboscript is None:
        raise SystemExit(
            "goboscript is not on PATH. "
            f"{_setup_hint()} (or install the GoboScript compiler)."
        )
    return gsdev_build.build_project_at(
        root, mode, goboscript=goboscript, mode_file=mode_include_path(root),
        prebuild=run_prebuild, emit=log, json_mode=JSON_MODE)


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
    reap_host_orphans(kill_live=False)
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
    out_path = Path(args.out).expanduser() if args.out else DEBUG_DIR / "stage.png"
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    if getattr(args, "no_reload", False):
        # Capture whatever the warm host is showing right now. No build, no
        # load, and the project keeps running: this is the live-tuning
        # counterpart to the default reload-and-wait capture.
        cdp = open_host(args.port)
        if cdp is None:
            raise SystemExit(
                "no running host to capture; start one with `run --leave-running`, "
                "or drop --no-reload for a fresh load")
        try:
            time.sleep(max(0, args.delay) / 1000.0)
            rect = host_stage_rect(cdp)
            data = cdp.call(
                "Page.captureScreenshot",
                {"format": "png", "clip": {**rect, "scale": 1}},
                timeout=20,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(base64.b64decode(data["data"]))
            log(f"screenshot (live): {out_path} ({int(rect['width'])}x{int(rect['height'])})")
        finally:
            cdp.close()
        return 0
    if not getattr(args, "sb3", "") and not args.no_build:
        build_project(resolve_mode(args))
    artifact = artifact_path(args)
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
    reap_host_orphans(kill_live=False)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cdp = open_host(args.port)
    if cdp is None:
        return 1
    try:
        state = cdp.evaluate(
            "JSON.stringify({sprites: window.__host.vm.runtime.targets.map(t => t.getName()),"
            " running: window.__host.vm.runtime.threads.length > 0})",
            timeout=READ_TIMEOUT,
        )
        log(state)
    finally:
        cdp.close()
    return 0


# --- Live tuning: read/write variables in a running project -------------------


parse_selector = gsdev_state.parse_selector
parse_value = gsdev_state.parse_value
_spec = gsdev_state.spec


def _var_js(*args, **kwargs):
    return gsdev_state.var_js(*args, **kwargs)


def select_js(selector: str) -> str:
    return gsdev_state.select_js(selector)


def _stop_watch_js(glob: str = "window.__gsdevWatch") -> str:
    return gsdev_state.stop_watch_js(glob)


def apply_batch(cdp, tokens, ns="window.__host", log_fn=None) -> None:
    gsdev_state.apply_batch(cdp, tokens, ns=ns, emit=log_fn or log)


def _watch_run(cdp, selectors, duration_ms, interval_ms=20, ns="window.__host",
               glob="window.__gsdevWatch", log_fn=None) -> dict:
    return gsdev_state.watch_run(
        cdp, selectors, duration_ms, interval_ms, ns=ns, glob=glob,
        emit=log_fn or log, json_mode=JSON_MODE)


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
            result[selector] = cdp.evaluate(_var_js(target, name, index),
                                            timeout=READ_TIMEOUT)
        payload = json.dumps(result)
        if getattr(args, "out", None):
            # A whole list comes back as one big JSON line. Write it to a UTF-8 file
            # (a PowerShell 5.1 pipe is not UTF-8) instead of printing megabytes.
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(payload + "\n", encoding="utf-8", newline="\n")
            counts = {}
            for sel, entry in result.items():
                value = entry.get("value") if isinstance(entry, dict) else entry
                if isinstance(value, list):
                    counts[sel] = len(value)
            log(json.dumps({"out": str(out), "bytes": out.stat().st_size,
                            "items": counts}))
        else:
            log(payload)
    except (CDPError, TimeoutError) as exc:
        log(f"no answer within {READ_TIMEOUT:g}s ({type(exc).__name__}); the page may be "
            f"busy or mid-navigation — raise GSDEV_READ_TIMEOUT if the read is genuinely slow")
        return 1
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
        result = cdp.evaluate(_var_js(target, name, index, value), timeout=READ_TIMEOUT) or {}
        log(json.dumps({"selector": args.selector, **result}))
        return 1 if result.get("error") else 0
    finally:
        cdp.close()


def cmd_set_batch(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        apply_batch(cdp, args.assignments)
    finally:
        cdp.close()
    return 0


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
            cdp.evaluate(_stop_watch_js())
        except (CDPError, TimeoutError, OSError):
            pass
        cdp.close()
    return 0


SESSION_SCHEMA = 1


def run_session_lines(cdp: CDP, lines, quiet: bool = False,
                      event_sink=None) -> dict:
    api = gsdev_session.SessionAPI(
        log=log,
        parse_selector=parse_selector,
        parse_value=parse_value,
        var_js=_var_js,
        apply_batch=apply_batch,
        watch_run=_watch_run,
        mouse_js=mouse_js,
        key_js=key_js,
        input_key_name=input_key_name,
        select_js=select_js,
        compare=compare,
        capture_js=_capture_js,
        assert_capture_js=_assert_capture_js,
        spec=_spec,
        pixel_js=pixel_js,
        current_frame=current_frame,
        pause_js=pause_js,
        resume_js=resume_js,
        events_js=events_js,
        inspect_js=_inspect_js,
        broadcast_wait_js=_broadcast_wait_js,
        get_errors=get_errors,
        errors_ok=_errors_ok,
        stop_watch_js=_stop_watch_js,
        read_timeout=READ_TIMEOUT,
        schema=SESSION_SCHEMA,
    )
    return gsdev_session.run_session_lines(cdp, lines, quiet, event_sink, api=api)


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

INPUT_KEY_ALIASES = gsdev_page.INPUT_KEY_ALIASES


input_key_name = gsdev_page.input_key_name
mouse_js = gsdev_page.mouse_js
key_js = gsdev_page.key_js
_hold_input = gsdev_page.hold_input
pixel_js = gsdev_page.pixel_js
events_js = gsdev_page.events_js
pause_js = gsdev_page.pause_js
resume_js = gsdev_page.resume_js
step_js = gsdev_page.step_js
broadcast_js = gsdev_page.broadcast_js
current_frame = gsdev_page.current_frame


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
        cdp.evaluate(mouse_js(args.x, args.y, None))
        cdp.evaluate(mouse_js(args.x, args.y, True))
        _hold_input(cdp, float(args.settle))
        cdp.evaluate(mouse_js(args.x, args.y, False))
        _hold_input(cdp, 0.0)
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
        if args.down:
            cdp.evaluate(key_js(key, True))
            mode = "down"
        elif args.up:
            cdp.evaluate(key_js(key, False))
            mode = "up"
        else:
            cdp.evaluate(key_js(key, True))
            _hold_input(cdp, float(args.settle))
            cdp.evaluate(key_js(key, False))
            mode = "press"
        log(json.dumps({"key": key, "mode": mode}))
    finally:
        cdp.close()
    return 0


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
        cdp.evaluate(pause_js())
        log(json.dumps({"paused": True, "frame": current_frame(cdp)}))
    finally:
        cdp.close()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        cdp.evaluate(resume_js())
        log(json.dumps({"resumed": True, "frame": current_frame(cdp)}))
    finally:
        cdp.close()
    return 0


def cmd_step(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        cdp.evaluate(pause_js())
        before = current_frame(cdp)
        frame = int(cdp.evaluate(step_js(args.count)))
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


def cmd_wait_stopped(args: argparse.Namespace) -> int:
    """Wait for the VM's stop event (every script finished), event-driven."""
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        since = cdp.evaluate("window.__host.stopSeq()")
        result = cdp.evaluate(
            f"window.__host.waitStopped({since}, {int(args.timeout)})",
            await_promise=True, timeout=args.timeout / 1000.0 + 5,
        ) or {}
        log(json.dumps(result))
        return 0 if result.get("stopped") else 1
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


def cmd_render(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        log(json.dumps(cdp.evaluate(events_js())))
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


def _attach_sources(report: dict, project_root) -> None:
    gsdev_profile.attach_sources(report, project_root)


def _print_children(report: dict, name: str, top: int) -> None:
    gsdev_profile.print_children(report, name, top, log=log)


def _print_profile(report: dict, top: int, order: str = "self",
                   hats_min: float = 2.0) -> None:
    gsdev_profile.print_profile(report, top, order, hats_min, log=log)


def _print_profile_steps(report: dict, count: int) -> None:
    gsdev_profile.print_profile_steps(report, count, log=log)


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
        result = cdp.evaluate(broadcast_js(args.name))
        log(json.dumps({"broadcast": args.name, "threads": (result or {}).get("threads", 0)}))
    finally:
        cdp.close()
    return 0


_broadcast_wait_js = gsdev_page.broadcast_wait_js


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
        result = cdp.evaluate(_inspect_js(args.target), timeout=READ_TIMEOUT) or {}
        log(json.dumps(result))
        return 1 if result.get("error") else 0
    finally:
        cdp.close()


def cmd_props(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        result = cdp.evaluate(f"window.__host.props({json.dumps(args.target)})", timeout=READ_TIMEOUT) or {}
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


def cmd_targets(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        names = cdp.evaluate("window.__host.targetNames()", timeout=READ_TIMEOUT)
        log(json.dumps({"targets": names if isinstance(names, list) else []}))
    finally:
        cdp.close()
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        entries = cdp.evaluate("window.__host.logs()", timeout=READ_TIMEOUT) or []
        if args.json:
            log(json.dumps({"logs": entries}))
        else:
            for e in entries:
                log(f"[LOG {e['sprite']}] {e['value']}")
    finally:
        cdp.close()
    return 0


def cmd_clones(args: argparse.Namespace) -> int:
    cdp = open_live_cdp(args.port)
    if cdp is None:
        return 1
    try:
        log(json.dumps({"clones": cdp.evaluate("window.__host.clones()", timeout=READ_TIMEOUT)}))
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
    return cdp.evaluate("window.__host.errors()", timeout=READ_TIMEOUT) or {}


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
        source = bootstrap.sb2gs_source_dir()
        installed = bootstrap.sb2gs_patch_version()
        if installed is None:
            warn("sb2gs", f"{source} (install predates patch versioning; run setup)")
        elif installed < bootstrap.SB2GS_PATCH_VERSION:
            warn("sb2gs", f"{source} (patch v{installed}, behind this checkout's "
                          f"v{bootstrap.SB2GS_PATCH_VERSION}; run setup)")
        elif installed > bootstrap.SB2GS_PATCH_VERSION:
            warn("sb2gs", f"{source} (patch v{installed}, newer than this checkout's "
                          f"v{bootstrap.SB2GS_PATCH_VERSION}; update the checkout)")
        else:
            ok("sb2gs", str(source))
    elif sys.version_info[:2] < (3, 12):
        warn("sb2gs", f"needs Python 3.12+ (found {platform.python_version()})")
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

    # A system sb2gs is reported by doctor/status but never executed: it has its
    # own interpreter and cannot receive the Brotli-blocking runner that ships
    # with the bundled source. Always run the pinned copy.
    try:
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
    # Provenance tracks the source project, independent of whether the generated
    # code compiles, so write it whenever the decompile produced output - even if
    # --verify failed.
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
    # Shared verbs must keep identical syntax across gsdev and gsbridge; verb_parity is
    # the single source of truth for what is shared and what may differ (see its module
    # docstring). Imported lazily so a broken gsbridge never breaks the rest of selftest.
    try:
        import verb_parity
        parity_problems, parity_shared = verb_parity.compare()
        check(
            "verb parity (gsdev/gsbridge)",
            not parity_problems,
            "; ".join(parity_problems) if parity_problems
            else f"{parity_shared} shared verbs identical",
        )
    except Exception as exc:  # noqa: BLE001
        check("verb parity (gsdev/gsbridge)", False, f"{type(exc).__name__}: {exc}")

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
    shot.add_argument("--no-reload", action="store_true",
                      help="capture the running project instead of loading it (skips the build; "
                           "leaves the host and project running)")
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
    get.add_argument("--out", help="write the JSON to FILE (UTF-8) instead of stdout")
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

    waitstopped = subparsers.add_parser(
        "wait_stopped", help="wait for the VM stop event (all scripts finished)")
    add_port(waitstopped)
    waitstopped.add_argument("--timeout", type=int, default=60000,
                             help="timeout in ms")
    waitstopped.set_defaults(func=cmd_wait_stopped)

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

    targets = subparsers.add_parser("targets", help="list loaded target (sprite) names")
    add_port(targets)
    targets.set_defaults(func=cmd_targets)

    logs = subparsers.add_parser("logs", help="goboscript log/warn/error captured by the host")
    logs.add_argument("--json", action="store_true")
    add_port(logs); logs.set_defaults(func=cmd_logs)

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
