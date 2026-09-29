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
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gsdev  # noqa: E402
from gsdev import (  # noqa: E402
    CDPError,
    connect,
    dismiss_browser_dialogs,
    ensure_host_server,
    free_port,
    input_key_name,
    list_targets,
)

DEFAULT_PORT = int(os.environ.get("GSBRIDGE_PORT", "9400"))
DEFAULT_TIMEOUT = 30.0

TARGET_URLS = {
    "github": "https://scratchfoundation.github.io/scratch-gui/player.html",
    "editor": "https://scratch.mit.edu/projects/editor/",
}

# ---------------------------------------------------------------------------
# Injected bridge (mirrors host.js capability surface against a discovered VM)
# ---------------------------------------------------------------------------
BRIDGE_JS = r"""
(() => {
  const targetCount = v => (v && v.runtime && v.runtime.targets) ? v.runtime.targets.length : -1;
  const usable = v => !!(v && v.runtime && v.loadProject);
  // Reuse an installed bridge only while its VM still holds the project. The
  // production editor keeps settling just after loadProject and can expose a
  // stale/empty VM, so a cached VM with no sprite targets must be re-resolved
  // instead of trusted (otherwise every later get/set silently reads nothing).
  if (window.__bridge && window.__bridge.__installed && targetCount(window.__bridge.vm) > 1) {
    window.__probeVM = window.__bridge.vm; return true;
  }
  const candidates = [];
  const push = v => { if (usable(v) && candidates.indexOf(v) < 0) candidates.push(v); };
  push(window.__host && window.__host.vm);
  push(window.__probeVM);
  for (const el of document.querySelectorAll('*')) {
    const key = Object.keys(el).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
    if (!key) continue;
    for (let f = el[key]; f; f = f.return) {
      for (const p of [f.memoizedProps, f.pendingProps]) {
        const cand = p && (p.vm || (p.store && p.store.getState && p.store.getState().scratchGui && p.store.getState().scratchGui.vm));
        push(cand);
      }
    }
  }
  if (!candidates.length) return false;
  // Prefer the VM that actually has a loaded project (Stage + sprites).
  let vm = candidates.find(v => targetCount(v) > 1) || candidates[0];
  if (!vm || !vm.runtime) return false;
  window.__probeVM = vm;
  const rt = vm.runtime;
  const Z = '\u200B\u200B';
  const st = { frame: 0, frameEvents: 0, renderEvents: 0, logs: [], errors: [], stopped: false,
               projectLoaded: 0,
               lastStepAt: 0, stepInterval: 0, lastRenderAt: 0, renderInterval: 0, stepAt: 0,
               rendertime: 0, steptime: 0, rendered: 0, marks: [], logSeq: 0, logErrors: 0 };

  // --- goboscript log/warn/error capture (same proccode trick as host.js) ---
  try {
    const prim = rt._primitives, levels = {};
    levels[Z + 'log' + Z + ' %s'] = 'log';
    levels[Z + 'warn' + Z + ' %s'] = 'warn';
    levels[Z + 'error' + Z + ' %s'] = 'error';
    const existing = prim['procedures_call'];
    if (existing && !existing.__bridgeLog) {
      const wrapped = function (args, util) {
        try {
          const pc = args && args.mutation && args.mutation.proccode;
          const level = levels[pc];
          if (level) {
            let value = args.arg0;
            if (value === undefined) { for (const k in args) { if (k !== 'mutation') { value = args[k]; break; } } }
            let sprite = 'unknown';
            try { sprite = util && util.target ? util.target.getName() : 'unknown'; } catch (e) {}
            st.logs.push({ seq: ++st.logSeq, sprite: sprite === 'Stage' ? 'stage' : sprite, level, value: String(value), frame: st.frame });
            if (level === 'error') { st.logErrors += 1; }
            if (st.logs.length > 1000) st.logs.shift();
          }
        } catch (e) {}
        return existing.apply(this, arguments);
      };
      wrapped.__bridgeLog = true;
      prim['procedures_call'] = wrapped;
    }
  } catch (e) {}

  const recordError = (where, message) => {
    st.errors.push({ where, frame: st.frame, message: String(message) });
    if (st.errors.length > 500) st.errors.shift();
  };
  window.addEventListener('error', ev => recordError('page', (ev.error && ev.error.stack) || ev.message || 'error'));
  window.addEventListener('unhandledrejection', ev => recordError('page', (ev.reason && ev.reason.stack) || ev.reason || 'unhandled rejection'));

  // --- frame/render hooks ---
  if (rt._step && !rt._step.__bridge) {
    const orig = rt._step;
    const wrapped = function (...a) {
      st.frame += 1; st.frameEvents += 1;
      const now = performance.now();
      if (st.lastStepAt) { const dt = now - st.lastStepAt; st.stepInterval = st.stepInterval ? st.stepInterval * 0.9 + dt * 0.1 : dt; }
      st.lastStepAt = now; st.stepAt = now;
      try { window.dispatchEvent(new CustomEvent('gsbridge:frame', { detail: { frame: st.frame } })); } catch (e) {}
      try { return orig.apply(this, a); }
      catch (e) { recordError('vm', (e && e.stack) || e); return undefined; }
    };
    wrapped.__bridge = true; rt._step = wrapped;
  }
  const renderer = vm.renderer;
  if (renderer && renderer.draw && !renderer.draw.__bridge) {
    const orig = renderer.draw;
    const wrapped = function (...a) {
      const r = orig.apply(this, a);
      const now = performance.now();
      if (st.lastRenderAt) { const dt = now - st.lastRenderAt; st.renderInterval = st.renderInterval ? st.renderInterval * 0.9 + dt * 0.1 : dt; }
      st.lastRenderAt = now;
      st.rendertime = st.stepAt ? now - st.stepAt : 0;
      st.renderEvents += 1;
      st.rendered = st.frame;
      try { window.dispatchEvent(new CustomEvent('gsbridge:render', { detail: { frame: st.frame } })); } catch (e) {}
      return r;
    };
    wrapped.__bridge = true; renderer.draw = wrapped;
  }

  try { vm.on('PROJECT_RUN_STOP', () => { st.stopped = true; st.logs.push({ seq: ++st.logSeq, sprite: 'vm', level: 'stop', value: 'project stopped', frame: st.frame }); }); } catch (e) {}
  try { vm.on('PROJECT_LOADED', () => { st.projectLoaded += 1; }); } catch (e) {}

  // Procedure profiling reuses the shared module (scratchhost/profiler.js, injected
  // just before this bridge). It installs its own runtime/primitive/pen hooks, so the
  // bridge only re-exposes the API below.
  let profiler = null;
  try {
    if (window.GsdevProfiler && renderer) profiler = window.GsdevProfiler({ runtime: rt, renderer });
  } catch (e) {}

  const findTarget = name => {
    if (name === null || name === undefined) return null;
    let base = String(name), clone = null;
    const hash = base.lastIndexOf('#');
    if (hash > 0) { const p = parseInt(base.slice(hash + 1), 10); if (!Number.isNaN(p)) { clone = p; base = base.slice(0, hash); } }
    if (base === 'stage' || base === 'Stage') return rt.getTargetForStage() || null;
    const any = rt.targets.find(t => t.sprite && t.sprite.name === base);
    if (!any) return null;
    const clones = any.sprite.clones;
    if (clone === null) return clones.find(t => t.isOriginal) || clones[0] || null;
    return clones[clone] || null;
  };
  const variableFor = (target, name) => { for (const k in target.variables) { if (target.variables[k].name === name) return target.variables[k]; } return null; };
  const parseSpec = spec => {
    let s = String(spec), idx = null;
    const m = s.match(/^(.*)\[(-?\d+)\]$/);
    if (m) { s = m[1]; idx = parseInt(m[2], 10); }
    const dot = s.indexOf('.');
    if (dot >= 0) return { targetName: s.slice(0, dot), varName: s.slice(dot + 1), idx };
    return { targetName: null, varName: s, idx };
  };
  const resolve = spec => {
    const p = parseSpec(spec);
    const target = p.targetName ? findTarget(p.targetName) : rt.getTargetForStage();
    if (!target) return null;
    const variable = variableFor(target, p.varName);
    return variable ? { target, variable, idx: p.idx } : null;
  };
  const stageSize = () => ({ w: rt.constructor.STAGE_WIDTH || 480, h: rt.constructor.STAGE_HEIGHT || 360 });
  const readPixel = (x, y) => {
    const gl = renderer && renderer.gl;
    if (!gl || typeof gl.readPixels !== 'function') return null;
    const width = gl.drawingBufferWidth, height = gl.drawingBufferHeight, s = stageSize();
    const ix = Math.min(width - 1, Math.max(0, Math.round((x / s.w + 0.5) * width)));
    const iy = Math.min(height - 1, Math.max(0, Math.round((y / s.h + 0.5) * height)));
    const px = new Uint8Array(4);
    try { gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.readPixels(ix, iy, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); }
    catch (e) { return null; }
    return '#' + [px[0], px[1], px[2]].map(v => v.toString(16).padStart(2, '0')).join('');
  };
  const stageCanvas = () => {
    const cs = [...document.querySelectorAll('canvas')];
    if (!cs.length) return null;
    return cs.reduce((a, b) => (a.getBoundingClientRect().width * a.getBoundingClientRect().height >= b.getBoundingClientRect().width * b.getBoundingClientRect().height ? a : b));
  };
  const perf = () => {
    const gl = renderer && renderer.gl;
    let gpu = '', software = false;
    try {
      const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
      gpu = gl ? String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)) : '';
      software = /swiftshader|software|llvmpipe|basic render|mesa offscreen/i.test(gpu);
    } catch (e) {}
    return { frame: st.frame, rendered: st.rendered,
             fps: st.renderInterval ? 1000 / st.renderInterval : 0,
             stepfps: st.stepInterval ? 1000 / st.stepInterval : 0,
             rendertime: st.rendertime, steptime: st.steptime,
             logErrors: st.errors.length + st.logErrors, gpu, software };
  };

  window.__bridge = {
    __installed: true, vm, rt,
    frame: () => st.frame, rendered: () => st.rendered, stopped: () => st.stopped,
    setProfiling: on => (profiler ? profiler.setEnabled(on) : false),
    profilingOn: () => !!(profiler && profiler.enabled()),
    profile: () => (profiler ? profiler.report() : null),
    profileReset: () => (profiler ? profiler.reset() : false),
    profileSteps: since => (profiler ? profiler.steps(since) : { series_start: 0, steps: [] }),
    profileValue: name => (profiler ? profiler.value(name) : undefined),
    logs: () => st.logs.slice(),
    logsSince: n => st.logs.filter(x => x.seq > n),
    logErrors: () => st.logErrors,
    errors: () => ({ count: st.errors.length + st.logErrors, logErrors: st.logErrors, errors: st.errors }),
    clearLogs: () => { st.logs = []; st.errors = []; st.logErrors = 0; st.logSeq = 0; st.stopped = false; },
    findTarget,
    targetNames: () => rt.targets.map(t => t.getName()),
    projectLoaded: () => st.projectLoaded,
    // True once the page's own project is in place (Stage plus a sprite, or a
    // PROJECT_LOADED we witnessed). `run` waits for this before replacing it, so
    // our loadProject is the last one applied and the page cannot clobber it.
    pageReady: () => (rt.targets.length >= 2) || st.projectLoaded > 0,
    get: spec => {
      if (typeof spec === 'string' && spec.startsWith('@')) {
        const p = perf(); const k = spec.slice(1).toLowerCase();
        // Alias so @renderfps means the same thing in gsdev and the bridge.
        const alias = { renderfps: 'fps' }[k] || k;
        return alias in p ? p[alias] : null;
      }
      const r = resolve(spec);
      if (!r) return null;
      const v = r.variable.value;
      return r.idx === null ? v : (Array.isArray(v) ? v[r.idx - 1] : null);
    },
    set: (spec, value) => {
      const r = resolve(spec);
      if (!r) return false;
      if (r.idx === null) { r.variable.value = value; }
      else if (Array.isArray(r.variable.value)) { r.variable.value[r.idx - 1] = value; }
      else { return false; }
      try { rt.requestUpdateMonitor && rt.requestUpdateMonitor({ id: r.variable.id, value: r.variable.value }); } catch (e) {}
      return true;
    },
    setBatch: pairs => { for (const [s, v] of pairs) { if (!window.__bridge.set(s, v)) return false; } return true; },
    mouse: (x, y, isDown) => {
      const c = stageCanvas(); if (!c) return;
      const r = c.getBoundingClientRect(); const s = stageSize();
      const px = (x / s.w + 0.5) * r.width;    // Scratch -> canvas coords (same as gsdev)
      const py = (0.5 - y / s.h) * r.height;
      vm.postIOData('mouse', { x: px, y: py, canvasWidth: r.width, canvasHeight: r.height,
                               isDown: isDown === null ? undefined : isDown });
    },
    click: (x, y) => { window.__bridge.mouse(x, y, true); window.__bridge.mouse(x, y, false); },
    key: (key, isDown) => { vm.postIOData('keyboard', { key, isDown }); },
    greenFlag: () => vm.greenFlag(),
    stopAll: () => vm.stopAll(),
    start: () => vm.start(),
    stageRect: () => { const c = stageCanvas(); if (!c) return null; const r = c.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; },
    pixel: (x, y) => new Promise(resolve => {
      let done = false;
      const listener = () => { if (done) return; const hex = readPixel(x, y); if (hex) { done = true; window.removeEventListener('gsbridge:render', listener); resolve(hex); } };
      window.addEventListener('gsbridge:render', listener);
      setTimeout(() => { if (!done) { done = true; window.removeEventListener('gsbridge:render', listener); resolve(readPixel(x, y)); } }, 2000);
    }),
    readPixel,
    gpu: () => { const p = perf(); return { renderer: p.gpu, software: p.software }; },
    perf,
    inspect: name => {
      const describe = t => {
        const out = [];
        for (const k in t.variables) { const v = t.variables[k]; out.push({ name: v.name, type: v.type === '' ? 'scalar' : v.type, value: Array.isArray(v.value) ? v.value.slice(0, 50) : v.value }); }
        return { name: t.getName(), isStage: t.isStage, x: t.x, y: t.y, visible: t.visible, variables: out };
      };
      if (name) { const t = findTarget(name); return t ? describe(t) : null; }
      return { targetCount: rt.targets.length, targets: rt.targets.map(describe) };
    }
  };
  window.__bridge.clearLogs();
  return true;
})()
"""

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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if cdp.evaluate(BOOT_WAIT_JS, timeout=10):
                return True
            cdp.evaluate(PROFILER_JS, timeout=10)
            if cdp.evaluate(BRIDGE_JS, timeout=10) and cdp.evaluate(BOOT_WAIT_JS, timeout=10):
                return True
        except (CDPError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    return False


def open_target_page(port: int, timeout: float = 60.0, match: str | None = None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            targets = list_targets(port=port)
            page = None
            for t in targets:
                if t.get("type") != "page":
                    continue
                url = t.get("url", "")
                if url.startswith("edge://") or url.startswith("chrome://"):
                    continue
                if match and match not in url:
                    continue
                page = t
                break
            if page:
                return connect(page)
        except (CDPError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    return None


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
    return gsdev.find_browser()


def launch_browser(args, url: str, port: int, profile: Path) -> subprocess.Popen:
    browser = resolve_browser(args.browser)
    if not browser:
        raise SystemExit("no Chrome/Edge found; set GSDEV_BROWSER")
    # Shared with gsdev so both hosts run under the same browser settings; the
    # bridge only adds its own extras below.
    flags = gsdev.browser_flags(browser, port, profile)
    flags += ["--disable-extensions", "--window-size=1200,900"]
    if args.headless:
        flags.append("--headless=new")
    flags.append(url)
    log(f"launching {Path(browser).name}{' headless' if args.headless else ''} -> {url}")
    return subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        return gsdev.host_url()
    raise SystemExit(f"unknown target {args.target}")


def profile_dir(port: int, tag: str) -> Path:
    base = Path(os.environ.get("TEMP", ".")) / "kilo" / "gsbridge"
    return base / f"{tag}_{port}_{int(time.time() * 1000)}"


def match_for(url: str, target: str) -> str:
    """URL substring identifying the page we launched (for CDP target matching)."""
    if target == "localhost":
        return f"127.0.0.1:{gsdev.HOST_SERVER_PORT}"
    return urlparse(url).netloc or ""


def state_path(port: int) -> Path:
    base = Path(os.environ.get("TEMP", ".")) / "kilo" / "gsbridge"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"session_{port}.json"


def write_state(port: int, **fields) -> None:
    try:
        state_path(port).write_text(json.dumps(fields), encoding="utf-8")
    except OSError:
        pass


def read_state(port: int) -> dict:
    try:
        return json.loads(state_path(port).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def clear_state(port: int) -> None:
    try:
        state_path(port).unlink()
    except OSError:
        pass


def pid_owns_profile(pid: int, profile: str) -> bool:
    """True only if `pid` is still the browser we launched (its command line
    contains our unique profile dir). Guards against reusing a recycled PID."""
    if not profile or os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").CommandLine'],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return False
    return profile.lower() in (out or "").lower()


def stop_own_browser(port: int) -> None:
    """Close only the browser this bridge session launched -- and only after
    verifying ownership. Never touch gsdev's host server (another local session
    may be using it), and never act on a stale record."""
    st = read_state(port)
    token = st.get("token")
    if not token:
        log(f"no bridge session record on port {port}; nothing to close")
        return
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
    pid, profile = st.get("pid"), st.get("profile")
    if isinstance(pid, int) and pid_owns_profile(pid, profile or ""):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    clear_state(port)


def cmd_run(args) -> int:
    if args.target == "localhost":
        ensure_host_server()
    url = target_url(args)
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
    profile = profile_dir(port, args.target)
    profile.mkdir(parents=True, exist_ok=True)
    proc = launch_browser(args, url, port, profile)

    match = match_for(url, args.target)
    cdp = open_target_page(port, timeout=60, match=match)
    if cdp is None:
        proc.terminate()
        raise SystemExit(f"no page attached (expected a page matching {match!r})")
    token = uuid.uuid4().hex
    session = dict(target=args.target, url=url, host=match, pid=proc.pid,
                   profile=str(profile), token=token)
    write_state(port, **session)
    try:
        cdp.evaluate(f"window.__bridgeSession = {json.dumps(token)}")
        if not inject_bridge(cdp, timeout=args.ready_timeout):
            log("WARNING: bridge not installed (no VM found?)")
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
                while time.monotonic() < ready_deadline:
                    inject_bridge(cdp, timeout=5)
                    try:
                        if cdp.evaluate("!!(window.__bridge.pageReady && window.__bridge.pageReady())"):
                            break
                    except (CDPError, TimeoutError, OSError):
                        pass
                    time.sleep(0.25)
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
                time.sleep(0.4)
            log({"targets": names})
            write_state(port, **session, sb3=str(sb3), targets=expected)
            if sorted(names) != wanted:
                log(f"WARNING: expected targets {wanted}, got {names}")

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
        clear_state(port)
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
                cdp.evaluate("window.__bridge.greenFlag()")
                time.sleep(0.4)
            except (CDPError, TimeoutError, OSError):
                pass
        time.sleep(0.15)


def cmd_targets(args) -> int:
    """List the loaded target (sprite) names, to confirm the project was replaced."""
    cdp, _ = attach(args)
    try:
        names = cdp.evaluate("window.__bridge.targetNames()")
        log({"targets": names if isinstance(names, list) else []})
    finally:
        cdp.close()
    return 0


def cmd_get(args) -> int:
    cdp, session = attach(args)
    try:
        out = {}
        for selector in args.selectors:
            value = cdp.evaluate(f"window.__bridge.get({json.dumps(selector)})")
            if value is None and "." in selector:
                _ensure_target(cdp, selector, session)
                value = cdp.evaluate(f"window.__bridge.get({json.dumps(selector)})")
            out[selector] = value
        log(out)
    finally:
        cdp.close()
    return 0


def cmd_set(args) -> int:
    cdp, session = attach(args)
    try:
        value = json.loads(args.value)
        ok = cdp.evaluate(
            f"window.__bridge.set({json.dumps(args.selector)}, {json.dumps(value)})")
        if not ok and "." in args.selector:
            _ensure_target(cdp, args.selector, session)
            ok = cdp.evaluate(
                f"window.__bridge.set({json.dumps(args.selector)}, {json.dumps(value)})")
        log({args.selector: ok})
        return 0 if ok else 1
    finally:
        cdp.close()


def cmd_set_batch(args) -> int:
    cdp, session = attach(args)
    try:
        pairs = []
        for item in args.assignments:
            if "=" not in item:
                raise SystemExit(f"expected name=value, got {item!r}")
            name, value = item.split("=", 1)
            pairs.append([name, json.loads(value)])
        ok = cdp.evaluate(f"window.__bridge.setBatch({json.dumps(pairs)})")
        if not ok:
            for name, _ in pairs:
                if "." in name:
                    _ensure_target(cdp, name, session)
            ok = cdp.evaluate(f"window.__bridge.setBatch({json.dumps(pairs)})")
        log({"set_batch": ok})
        return 0 if ok else 1
    finally:
        cdp.close()


def cmd_click(args) -> int:
    cdp, _ = attach(args)
    try:
        cdp.evaluate(f"window.__bridge.click({args.x}, {args.y})")
        log({"click": [args.x, args.y]})
    finally:
        cdp.close()
    return 0


def cmd_mouse(args) -> int:
    cdp, _ = attach(args)
    try:
        state = True if args.down else (False if args.up else None)
        cdp.evaluate(f"window.__bridge.mouse({args.x}, {args.y}, {json.dumps(state)})")
        log({"mouse": [args.x, args.y], "down": state})
    finally:
        cdp.close()
    return 0


def cmd_key(args) -> int:
    cdp, _ = attach(args)
    key = input_key_name(args.key)
    try:
        if args.down:
            cdp.evaluate(f"window.__bridge.key({json.dumps(key)}, true)")
        elif args.up:
            # release only; sending a press first would fire another key-pressed hat
            cdp.evaluate(f"window.__bridge.key({json.dumps(key)}, false)")
        else:
            cdp.evaluate(f"window.__bridge.key({json.dumps(key)}, true)")
            cdp.evaluate(f"window.__bridge.key({json.dumps(key)}, false)")
        log({"key": args.key, "sent": key})
    finally:
        cdp.close()
    return 0


def cmd_pixel(args) -> int:
    cdp, _ = attach(args)
    try:
        hexv = cdp.evaluate(f"window.__bridge.pixel({args.x}, {args.y})", await_promise=True, timeout=10)
        log({"x": args.x, "y": args.y, "hex": hexv})
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
        log(cdp.evaluate("window.__bridge.errors()"))
    finally:
        cdp.close()
    return 0


def cmd_inspect(args) -> int:
    cdp, _ = attach(args)
    try:
        log(cdp.evaluate(f"window.__bridge.inspect({json.dumps(args.target)})"))
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
        cdp.evaluate("window.__bridge.greenFlag()")
        log({"restarted": True})
    finally:
        cdp.close()
    return 0


def cmd_close(args) -> int:
    stop_own_browser(args.port)   # only our browser; never gsdev's host server
    return 0


def cmd_expect_no_errors(args) -> int:
    cdp, _ = attach(args)
    try:
        errs = cdp.evaluate("window.__bridge.errors()")
        log(errs)
        return 1 if int(errs.get("count", 0)) else 0
    finally:
        cdp.close()


# ---------------------------------------------------------------------------
# session script runner
# ---------------------------------------------------------------------------
def cmd_session(args) -> int:
    cdp, _ = attach(args)
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
                    log({v: cdp.evaluate(f"window.__bridge.get({json.dumps(v)})") for v in parts[1:]})
                elif verb == "set" and len(parts) >= 3:
                    log({parts[1]: cdp.evaluate(f"window.__bridge.set({json.dumps(parts[1])}, {json.dumps(json.loads(' '.join(parts[2:])))})")})
                elif verb == "set_batch":
                    pairs = []
                    for item in parts[1:]:
                        n, v = item.split("=", 1)
                        pairs.append([n, json.loads(v)])
                    log({"set_batch": cdp.evaluate(f"window.__bridge.setBatch({json.dumps(pairs)})")})
                elif verb == "click" and len(parts) == 3:
                    cdp.evaluate(f"window.__bridge.click({parts[1]}, {parts[2]})")
                    log(f"[click {parts[1]} {parts[2]}]")
                elif verb == "key" and len(parts) >= 2:
                    key = input_key_name(parts[1])
                    mode = parts[2] if len(parts) > 2 else "press"
                    down = "true" if mode in ("down", "press") else "false"
                    cdp.evaluate(f"window.__bridge.key({json.dumps(key)}, {down})")
                    if mode == "press":
                        cdp.evaluate(f"window.__bridge.key({json.dumps(key)}, false)")
                    log(f"[key {parts[1]} {mode}]")
                elif verb == "sleep":
                    time.sleep(float(parts[1]) / 1000.0)
                elif verb == "waitframe":
                    target = cdp.evaluate("window.__bridge.frame()") + int(parts[1])
                    deadline = time.monotonic() + (float(parts[2]) / 1000.0 if len(parts) > 2 else 10.0)
                    while time.monotonic() < deadline and cdp.evaluate("window.__bridge.frame()") < target:
                        time.sleep(0.01)
                    if cdp.evaluate("window.__bridge.frame()") < target:
                        log(f"[ASSERT FAIL] waitframe {parts[1]} timed out")
                        failures += 1
                elif verb == "restart":
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
                    got = cdp.evaluate(f"window.__bridge.get({json.dumps(selector)})")
                    ok = _compare(got, op, want)
                    log(f"[ASSERT {'ok' if ok else 'FAIL'}] {selector} {op} {want} (got {got!r})")
                    failures += 0 if ok else 1
                elif verb == "expectpixel" and len(parts) >= 4:
                    got = cdp.evaluate(f"window.__bridge.pixel({parts[1]}, {parts[2]})", await_promise=True, timeout=10)
                    ok = str(got).lower() == parts[3].lower()
                    log(f"[ASSERT {'ok' if ok else 'FAIL'}] pixel {parts[1]} {parts[2]} == {parts[3]} (got {got})")
                    failures += 0 if ok else 1
                elif verb == "errors":
                    log(cdp.evaluate("window.__bridge.errors()"))
                elif verb == "expect_no_errors":
                    errs = cdp.evaluate("window.__bridge.errors()")
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
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="CDP port (default 9400)")


def main(argv=None) -> int:
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
    run.add_argument("--ready-timeout", type=float, default=45.0)
    add_port(run)
    run.set_defaults(func=cmd_run)

    get = sub.add_parser("get", help="read variables/lists/pseudo-metrics")
    get.add_argument("selectors", nargs="+")
    add_port(get); get.set_defaults(func=cmd_get)

    tgts = sub.add_parser("targets", help="list loaded target (sprite) names")
    add_port(tgts); tgts.set_defaults(func=cmd_targets)

    setp = sub.add_parser("set", help="write a variable/list element")
    setp.add_argument("selector"); setp.add_argument("value")
    add_port(setp); setp.set_defaults(func=cmd_set)

    sb = sub.add_parser("set_batch", help="write several in one evaluate")
    sb.add_argument("assignments", nargs="+")
    add_port(sb); sb.set_defaults(func=cmd_set_batch)

    click = sub.add_parser("click", help="click at stage coords")
    click.add_argument("x", type=float); click.add_argument("y", type=float)
    add_port(click); click.set_defaults(func=cmd_click)

    mouse = sub.add_parser("mouse", help="press/release at stage coords")
    mouse.add_argument("x", type=float); mouse.add_argument("y", type=float)
    mouse.add_argument("--down", action="store_true"); mouse.add_argument("--up", action="store_true")
    add_port(mouse); mouse.set_defaults(func=cmd_mouse)

    key = sub.add_parser("key", help="press/hold/release a key")
    key.add_argument("key"); key.add_argument("--down", action="store_true"); key.add_argument("--up", action="store_true")
    add_port(key); key.set_defaults(func=cmd_key)

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

    close = sub.add_parser("close", help="close the target browser")
    add_port(close); close.set_defaults(func=cmd_close)

    sess = sub.add_parser("session", help="run a verb script from --file or stdin")
    sess.add_argument("--file")
    add_port(sess); sess.set_defaults(func=cmd_session)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
