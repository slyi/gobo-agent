
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
               stopSeq: 0, waiters: [],
               projectLoaded: 0,
               lastStepAt: 0, stepInterval: 0, lastRenderAt: 0, renderInterval: 0, stepAt: 0,
               rendertime: 0, rendertimeAvg: 0, steptime: 0, steptimeAvg: 0,
                rendered: 0, marks: [], logSeq: 0, logErrors: 0 };

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
      // Condition waits are checked here, once per VM step: the page evaluates the
      // predicate itself, so callers never poll over CDP.
      if (st.waiters.length) {
        const keep = [];
        for (const w of st.waiters) {
          let v;
          try { v = w.read(); } catch (e) { v = undefined; }
          if (w.test(v)) w.resolve({ ok: true, value: v, frame: st.frame });
          else if (performance.now() >= w.deadline) w.resolve({ ok: false, expired: true, value: v, frame: st.frame });
          else keep.push(w);
        }
        st.waiters = keep;
      }
      const now = performance.now();
      if (st.lastStepAt) { const dt = now - st.lastStepAt; st.stepInterval = st.stepInterval ? st.stepInterval * 0.9 + dt * 0.1 : dt; }
      st.lastStepAt = now; st.stepAt = now;
      try { window.dispatchEvent(new CustomEvent('gsbridge:frame', { detail: { frame: st.frame } })); } catch (e) {}
      const started = performance.now();
      try { return orig.apply(this, a); }
      catch (e) { recordError('vm', (e && e.stack) || e); return undefined; }
      finally {
        const took = performance.now() - started;
        st.steptime = took;
        st.steptimeAvg = st.steptimeAvg ? st.steptimeAvg * 0.9 + took * 0.1 : took;
      }
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
      st.rendertimeAvg = st.rendertimeAvg ? st.rendertimeAvg * 0.9 + st.rendertime * 0.1 : st.rendertime;
      st.renderEvents += 1;
      st.rendered = st.frame;
      try { window.dispatchEvent(new CustomEvent('gsbridge:render', { detail: { frame: st.frame } })); } catch (e) {}
      return r;
    };
    wrapped.__bridge = true; renderer.draw = wrapped;
  }

  try { vm.on('PROJECT_RUN_STOP', () => { st.stopped = true; st.stopSeq += 1; st.logs.push({ seq: ++st.logSeq, sprite: 'vm', level: 'stop', value: 'project stopped', frame: st.frame }); }); } catch (e) {}
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
  // Accepts a selector string ("sprite.var", "sprite.var[3]") OR a spec object
  // ({target, name, index, label}, which is what gsdev's _spec/record/wait_until pass).
  const resolve = spec => {
    let targetName = null, varName = null, idx = null;
    if (spec && typeof spec === 'object') {
      targetName = spec.target === undefined ? null : spec.target;
      varName = spec.name;
      idx = spec.index === undefined ? null : spec.index;
    } else {
      const p = parseSpec(spec);
      targetName = p.targetName; varName = p.varName; idx = p.idx;
    }
    const target = targetName ? findTarget(targetName) : rt.getTargetForStage();
    if (!target) return null;
    const variable = variableFor(target, varName);
    return variable ? { target, variable, idx } : null;
  };
  const compareJs = (a, op, b) => {
    const na = Number(a), nb = Number(b);
    const numeric = a !== '' && b !== '' && isFinite(na) && isFinite(nb);
    const x = numeric ? na : String(a);
    const y = numeric ? nb : String(b);
    if (op === '==') return x === y;
    if (op === '!=') return x !== y;
    if (op === '<') return x < y;
    if (op === '<=') return x <= y;
    if (op === '>') return x > y;
    if (op === '>=') return x >= y;
    return false;
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
  // Same result shape as host.js's pixel read: {rgba, hex, x, y}. The bytes come from
  // the same readPixels call, so alpha is the framebuffer's own value.
  const pixelInfo = (x, y) => {
    const hex = readPixel(x, y);
    if (hex === null || hex === undefined) return null;
    const n = parseInt(hex.slice(1), 16);
    return { rgba: [(n >> 16) & 255, (n >> 8) & 255, n & 255, 255], hex, x, y };
  };
  const stageCanvas = () => {
    const cs = [...document.querySelectorAll('canvas')];
    if (!cs.length) return null;
    return cs.reduce((a, b) => (a.getBoundingClientRect().width * a.getBoundingClientRect().height >= b.getBoundingClientRect().width * b.getBoundingClientRect().height ? a : b));
  };
  const perfFresh = t => t && performance.now() - t < 1500;
  // Key names, freshness gate and EMA semantics mirror host.js exactly, so `render`
  // and `@` metrics are identical on both tools.
  const perf = () => {
    const gl = renderer && renderer.gl;
    let gpu = '', gpuVendor = '', software = false;
    try {
      const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
      gpu = gl ? String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)) : '';
      gpuVendor = gl && ext ? String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL)) : '';
      software = /swiftshader|software|llvmpipe|basic render|mesa offscreen/i.test(gpu);
    } catch (e) {}
    return { stepFps: perfFresh(st.lastStepAt) ? (st.stepInterval ? 1000 / st.stepInterval : 0) : 0,
             renderFps: perfFresh(st.lastRenderAt) ? (st.renderInterval ? 1000 / st.renderInterval : 0) : 0,
             rendertime: st.rendertime, rendertimeAvg: st.rendertimeAvg,
             steptime: st.steptime, steptimeAvg: st.steptimeAvg,
             frame: st.frame, rendered: st.rendered,
             gpu, gpuVendor, software };
  };

  // Same property/variable descriptions as host.js (props/getProp/setProp + inspect).
  const targetProps = target => {
    const costumes = target.getCostumes ? target.getCostumes() : [];
    const sounds = target.getSounds ? target.getSounds() : [];
    const cloneIndex = target.sprite ? target.sprite.clones.indexOf(target) : -1;
    const current = costumes[target.currentCostume];
    return {
      name: target.getName(),
      isStage: target.isStage,
      isOriginal: target.isOriginal,
      clone: target.isStage ? null : (cloneIndex >= 0 ? cloneIndex : null),
      visible: target.visible,
      x: target.x,
      y: target.y,
      size: target.size,
      direction: target.direction,
      rotationStyle: target.rotationStyle,
      draggable: target.draggable,
      layerOrder: target.getLayerOrder ? target.getLayerOrder() : null,
      currentCostume: target.currentCostume,
      costume: current ? current.name : null,
      costumes: costumes.map(item => item.name),
      sounds: sounds.map(item => item.name)
    };
  };
  const targetVariables = target => {
    const out = [];
    for (const k in target.variables) {
      const v = target.variables[k];
      out.push({ name: v.name, type: v.type === '' ? 'scalar' : v.type,
                 scope: v.type === 'broadcast' ? 'broadcast' : (target.isStage ? 'global' : 'sprite'),
                 value: Array.isArray(v.value) ? v.value.slice(0, 50) : v.value });
    }
    return out;
  };

  window.__bridge = {
    __installed: true, vm, rt,
    frame: () => st.frame, rendered: () => st.rendered, stopped: () => st.stopped,
    stopSeq: () => st.stopSeq,
    // Frame and pixel waits mirror host.js: promise-based, driven by the per-step
    // waiter list, with the wall-clock timeout only as a backstop.
    // Readiness wait: the page's own project is in place (the same predicate the
    // CLI used to poll for), evaluated once per VM step.
    waitReady: (timeoutMs) => new Promise(finish => {
      // The project counts as ready once the page has targets, or once its own load
      // event fired. Do NOT wait for a VM step: a player page that has not been
      // green-flagged (scratch.mit.edu/embed) never steps, so the step-driven waiter
      // would time out with the project sitting there. A page-side timer covers that.
      const read = () => (rt.targets.length >= 2) || st.projectLoaded > 0
        || (window.__host && window.__host.vm === vm);
      if (read()) return finish({ ok: true, ready: true, frame: st.frame });
      const deadline = performance.now() + (timeoutMs || 20000);
      const tick = () => {
        if (read()) return finish({ ok: true, ready: true, frame: st.frame });
        if (performance.now() >= deadline) {
          return finish({ ok: false, ready: false, frame: st.frame, timeout: true });
        }
        setTimeout(tick, 100);
      };
      setTimeout(tick, 100);
    }),
    waitFrames: (count, timeoutMs) => new Promise(finish => {
      const target = st.frame + Math.max(0, count | 0);
      const read = () => st.frame;
      const test = value => value >= target;
      if (test(st.frame)) return finish({ ok: true, frame: st.frame });
      st.waiters.push({
        read, test, deadline: performance.now() + (timeoutMs || 5000),
        resolve: r => finish({ ok: !!r.ok, frame: st.frame }),
      });
    }),
    // Event-driven condition wait: the predicate is evaluated once per VM step inside
    // the page (no CDP polling), resolving on `hit` or on timeout.
    waitUntil: (spec, op, value, timeoutMs) => new Promise(finish => {
      const r = resolve(spec);
      if (!r) return finish({ ok: false, error: 'variable not found: ' + spec });
      const read = () => {
        const v = r.variable.value;
        if (r.idx === null || r.idx === undefined) return v;
        return Array.isArray(v) ? v[r.idx - 1] : undefined;
      };
      const test = actual => {
        const na = Number(actual), nb = Number(value);
        const numeric = actual !== '' && value !== '' && isFinite(na) && isFinite(nb);
        const a = numeric ? na : String(actual);
        const b = numeric ? nb : String(value);
        if (op === '==') return a === b;
        if (op === '!=') return a !== b;
        if (op === '<') return a < b;
        if (op === '<=') return a <= b;
        if (op === '>') return a > b;
        if (op === '>=') return a >= b;
        return false;
      };
      st.waiters.push({ read, test, deadline: performance.now() + (timeoutMs || 60000), resolve: finish });
    }),
    // Wait for the VM's stop event (all threads finished) rather than polling:
    // resolves on the next PROJECT_RUN_STOP after `since`, or on timeout.
    // Pixel wait: same shape as host.js, checked once per VM step.
    waitPixel: (x, y, hex, timeoutMs) => new Promise(finish => {
      const want = String(hex).toLowerCase().replace('#', '');
      const norm = value => (value == null ? null : String(value).toLowerCase().replace('#', ''));
      const read = () => readPixel(x, y);
      const test = value => norm(value) === want;
      if (test(read())) return finish({ ok: true, pixel: read(), frame: st.frame });
      st.waiters.push({
        read, test, deadline: performance.now() + (timeoutMs || 5000),
        resolve: r => finish({ ok: !!r.ok, pixel: r.value, frame: st.frame }),
      });
    }),
    waitStopped: (since, timeoutMs) => new Promise(resolve => {
      const idle = () => rt.threads.length === 0;
      const finish = why => {
        try { vm.off('PROJECT_RUN_STOP', onStop); } catch (e) {}
        resolve({ stopped: true, why, seq: st.stopSeq, frame: st.frame, threads: rt.threads.length });
      };
      const onStop = () => finish('event');
      if (idle()) return finish('idle');
      try { vm.on('PROJECT_RUN_STOP', onStop); } catch (e) { return finish('unavailable'); }
      if (timeoutMs > 0) setTimeout(() => resolve({ stopped: false, why: 'timeout', seq: st.stopSeq, frame: st.frame, threads: rt.threads.length }), timeoutMs);
    }),
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
    vm,
    // Single-metric lookup, matching host.js, so gsdev's shared _var_js can serve the
    // bridge's `get @fps` etc. without a second implementation.
    perfValue: name => {
      const p = perf();
      switch (String(name || '').toLowerCase()) {
      case 'fps': case 'renderfps': return p.renderFps;
      case 'stepfps': return p.stepFps;
      case 'rendertime': return p.rendertime;
      case 'rendertimeavg': return p.rendertimeAvg;
      case 'steptime': return p.steptime;
      case 'steptimeavg': return p.steptimeAvg;
      case 'frame': return p.frame;
      case 'rendered': return p.rendered;
      case 'gpu': case 'gpurenderer': return p.gpu;
      case 'gpuvendor': return p.gpuVendor;
      case 'software': return p.software;
      default: return undefined;
      }
    },
    targetNames: () => rt.targets.map(t => t.getName()),
    // Mirror host.js so the shared frame/clones/stop verbs need no bridge-specific code.
    clones: () => {
      const out = {};
      for (const target of rt.targets) {
        if (target.isStage || !target.isOriginal || !target.sprite) continue;
        out[target.getName()] = target.sprite.clones.length - 1;
      }
      return out;
    },
    stop: () => vm.stopAll(),
    // Mirror host.js so the shared render/pause/resume/step/broadcast verbs reuse
    // gsdev's builders with ns="window.__bridge".
    events: () => Object.assign({
      frame: st.frame, rendered: st.rendered,
      frameEvents: st.frameEvents || 0, renderEvents: st.renderEvents || 0,
    }, perf()),
    pause: () => { clearInterval(rt._steppingInterval); rt._steppingInterval = null; },
    // No compatibility-mode pin: that is a local-host concern and forcing it here
    // would change a foreign page's pacing.
    resume: () => rt.start(),
    step: count => { for (let i = 0; i < count; i++) rt._step(); return st.frame; },
    // Mirror host.js's broadcastWait: start the hats, then resolve on the shim's own
    // render event when none of the started threads are still running.
    broadcastWait: (name, timeoutMs) => new Promise(resolve => {
      const started = rt.startHats(
        'event_whenbroadcastreceived', { BROADCAST_OPTION: name }) || [];
      const startFrame = st.frame;
      const result = extra => Object.assign({ name, threads: started.length, frame: startFrame }, extra);
      const pending = () => started.some(thread => rt.threads.indexOf(thread) > -1);
      if (!pending()) { resolve(result({ ok: true, endFrame: st.frame, waitedFrames: 0 })); return; }
      let done = false;
      const finish = extra => {
        if (done) return;
        done = true;
        window.removeEventListener('gsbridge:render', listener);
        resolve(result(Object.assign({ endFrame: st.frame, waitedFrames: st.frame - startFrame }, extra)));
      };
      const listener = () => { if (!pending()) finish({ ok: true }); };
      window.addEventListener('gsbridge:render', listener);
      setTimeout(() => finish({ ok: false, timeout: true }), timeoutMs);
    }),
    broadcast: name => {
      const started = rt.startHats('event_whenbroadcastreceived', { BROADCAST_OPTION: name }) || [];
      return { name, threads: started.length };
    },
    projectLoaded: () => st.projectLoaded,
    // True once the page's own project is in place (Stage plus a sprite, or a
    // PROJECT_LOADED we witnessed). `run` waits for this before replacing it, so
    // our loadProject is the last one applied and the page cannot clobber it.
    pageReady: () => (rt.targets.length >= 2) || st.projectLoaded > 0,
    get: spec => {
      if (typeof spec === 'string' && spec.startsWith('@')) {
        // One metric lookup, shared with gsdev's _var_js (host.js has the same switch).
        const v = window.__bridge.perfValue(spec.slice(1));
        return typeof v === 'undefined' ? null : v;
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
    pixelInfo,
    // Same keys as host.js (vendor included).
    gpu: () => { const p = perf(); return { vendor: p.gpuVendor, renderer: p.gpu, software: p.software }; },
    perf,
    // Top level matches host.js: {extensions, target} (or {extensions, targetCount,
    // targets} for no name). The per-target description is still the bridge's own.
    // Per-frame condition watcher, ported from host.js: records the exact frame (and
    // value) where the condition first held, optionally pausing on that frame.
    until: (spec, op, value, pause, maxFrames) => {
      const r = resolve(spec);
      if (!r) return { error: 'not found: ' + (spec && (spec.label || spec.name)) };
      if (st.until && st.until.listener) window.removeEventListener('gsbridge:render', st.until.listener);
      const state = (st.until = {
        hit: false, frame: null, value: null, paused: false, active: true, expired: false, listener: null
      });
      const start = st.frame;
      const readActual = () => {
        const all = r.variable.value;
        return (r.idx === null || r.idx === undefined)
          ? all : (Array.isArray(all) ? all[r.idx - 1] : undefined);
      };
      state.listener = event => {
        if (!state.active) return;
        if (event.detail.frame - start > maxFrames) {
          state.active = false; state.expired = true;
          window.removeEventListener('gsbridge:render', state.listener);
          return;
        }
        const actual = readActual();
        if (compareJs(actual, op, value)) {
          state.active = false; state.hit = true;
          state.frame = event.detail.frame;
          state.value = Array.isArray(actual) ? JSON.stringify(actual).slice(0, 200) : actual;
          window.removeEventListener('gsbridge:render', state.listener);
          if (pause) { window.__bridge.pause(); state.paused = true; }
        }
      };
      window.addEventListener('gsbridge:render', state.listener);
      return { ok: true };
    },
    // Recorder, ported from host.js: `gs.` -> `st.`, and the shim's own render event.
    record: (specs, max) => {
      const variables = [];
      for (const spec of specs) {
        const resolved = resolve(spec);
        if (!resolved) {
          return { error: 'not found: ' + (spec && (spec.label || spec.target)) };
        }
        variables.push({ spec, variable: resolved.variable, idx: resolved.idx });
      }
      if (st.recorder && st.recorder.listener) {
        window.removeEventListener('gsbridge:render', st.recorder.listener);
      }
      const recorder = (st.recorder = {
        rows: [], active: true, overflow: false, max: max || 100000, variables, listener: null
      });
      recorder.listener = event => {
        if (!recorder.active) return;
        if (recorder.rows.length >= recorder.max) {
          recorder.overflow = true; recorder.active = false; return;
        }
        const row = [event.detail.frame];
        for (const item of recorder.variables) {
          const all = item.variable.value;
          const raw = item.idx === null ? all
            : (Array.isArray(all) ? all[item.idx - 1] : undefined);
          // host.js's `format`: arrays become a truncated JSON string.
          row.push(Array.isArray(raw) ? JSON.stringify(raw).slice(0, 200) : raw);
        }
        recorder.rows.push(row);
      };
      window.addEventListener('gsbridge:render', recorder.listener);
      return { ok: true, labels: specs.map(x => x.label) };
    },
    trace: clear => {
      const recorder = st.recorder;
      const until = st.until
        ? { hit: st.until.hit, frame: st.until.frame, value: st.until.value,
            paused: st.until.paused, active: st.until.active, expired: st.until.expired }
        : null;
      if (!recorder) return { error: 'not recording', until };
      const out = {
        labels: recorder.variables.map(i => i.spec.label),
        rows: recorder.rows, overflow: recorder.overflow, active: recorder.active,
        max: recorder.max, until
      };
      if (clear) recorder.rows = [];
      return out;
    },
    stopRecord: () => {
      const recorder = st.recorder;
      if (!recorder) return { error: 'not recording' };
      recorder.active = false;
      if (recorder.listener) window.removeEventListener('gsbridge:render', recorder.listener);
      return { ok: true, rows: recorder.rows.length, overflow: recorder.overflow };
    },
    props: name => {
      const target = findTarget(name);
      if (!target) return { error: 'target not found: ' + name };
      return targetProps(target);
    },
    getProp: (name, prop) => {
      const target = findTarget(name);
      if (!target) return { error: 'target not found: ' + name };
      const p = targetProps(target);
      if (!Object.prototype.hasOwnProperty.call(p, prop)) {
        return { error: 'unknown property: ' + prop };
      }
      return { name: target.getName(), property: prop, value: p[prop] };
    },
    setProp: (name, prop, value) => {
      const target = findTarget(name);
      if (!target) return { error: 'target not found: ' + name };
      if (target.isStage && ['x', 'y', 'size', 'direction', 'visible', 'draggable',
        'costume', 'rotationStyle', 'layer'].indexOf(prop) > -1) {
        return { error: 'not settable on the stage: ' + prop };
      }
      switch (prop) {
      case 'x': target.setXY(Number(value), target.y, true); break;
      case 'y': target.setXY(target.x, Number(value), true); break;
      case 'direction': target.setDirection(Number(value)); break;
      case 'size': target.setSize(Number(value)); break;
      case 'visible': target.setVisible(Boolean(value)); break;
      case 'draggable': target.setDraggable(Boolean(value)); break;
      case 'costume': {
        const index = (typeof value === 'number')
          ? Math.round(value)
          : target.getCostumeIndexByName(String(value));
        if (index === undefined || index === null || index < 0 ||
            index >= target.getCostumes().length) {
          return { error: 'no such costume: ' + value };
        }
        target.setCostume(index);
        break;
      }
      case 'rotationStyle': target.setRotationStyle(String(value)); break;
      case 'layer': {
        const layer = String(value).toLowerCase();
        if (layer === 'front') target.goToFront();
        else if (layer === 'back') target.goToBack();
        else return { error: 'layer must be front or back' };
        break;
      }
      default: return { error: 'not settable: ' + prop };
      }
      return Object.assign({ ok: true }, targetProps(target));
    },
    inspect: name => {
      let extensions = [];
      try {
        const manager = vm.extensionManager || (rt.extensions && rt.extensions);
        const loaded = manager && manager._loadedExtensions;
        if (loaded) extensions = Array.from(loaded.keys());
      } catch (e) {}
      const describe = t => Object.assign(targetProps(t), { variables: targetVariables(t) });
      if (name) { const t = findTarget(name); return t ? { extensions, target: describe(t) } : null; }
      return { extensions, targetCount: rt.targets.length, targets: rt.targets.map(describe) };
    }
  };
  window.__bridge.clearLogs();
  return true;
})()
