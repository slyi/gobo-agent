/* Minimal headless host for upstream @scratch/scratch-vm.
   Phase 0 spike: prove the VM + renderer + storage can run a local .sb3 with
   embedded assets, expose the VM for get/set, and capture goboscript logs. */
(() => {
  const marks = (window.__marks = { start: performance.now() });
  const hud = window.__hostHud || (() => {});
  hud('gsdev host: starting VM\u2026');
  try {
    const vm = new window.VirtualMachine();
    // Scratch's player steps Scratch projects at 30 Hz via compatibility mode;
    // scratch-vm defaults to 60 Hz. It only takes effect in runtime.start(), so
    // re-apply it after every load and before each start rather than trusting the
    // page-load setting alone.
    const applyCompatibilityMode = () => vm.setCompatibilityMode(true);
    applyCompatibilityMode();

    // The web UMD global is the module namespace: the constructor is
    // ScratchStorage.ScratchStorage (playground requires `.ScratchStorage`).
    const StorageCtor = window.ScratchStorage.ScratchStorage || window.ScratchStorage;
    const storage = new StorageCtor();
    vm.attachStorage(storage);

    const canvas = document.getElementById('stage');
    const renderer = new window.ScratchRender(canvas);
    vm.attachRenderer(renderer);
    try {
      vm.attachV2BitmapAdapter(new window.ScratchSVGRenderer.BitmapAdapter());
    } catch (error) {
      marks.bitmapAdapterError = String(error);
    }

    window.__host = {
      vm,
      renderer,
      load: bytes => {
        const promise = vm.loadProject(bytes);
        // A fresh project starts from a clean slate. Without clearing `stopped`
        // and `logs`, a warm host that saw a project stop would report
        // "project stopped" and drop logs on the very next `run`.
        if (promise && promise.then) {
          promise.then(() => {
            applyCompatibilityMode();
            hud('gsdev host \u2014 project loaded');
            // Extensions register their primitives during load; re-wrap them.
            if (gs.perf && gs.perf.logicEnabled) refreshLogic();
            const state = window.__gsdev;
            if (state) {
              state.errors = [];
              state.logErrors = 0;
              state.stopped = false;
              state.logs = [];
              state.seen = [];
              const p = state.perf;
              if (p) {
                p.stepInterval = 0; p.renderInterval = 0;
                p.stepFps = 0; p.renderFps = 0;
                p.lastStepAt = 0; p.lastRenderAt = 0; p.stepAt = 0;
                p.rendertime = 0; p.rendertimeAvg = 0;
                p.steptime = 0; p.steptimeAvg = 0;
                p.drawcount = 0; p.stamps = 0; p.drawcalls = 0;
                p._pen = 0; p._stamps = 0; p._gl = 0;
              }
            }
          });
        }
        return promise;
      },
      start: () => {
        applyCompatibilityMode();
        vm.start();
        vm.greenFlag();
        hud('gsdev host \u2014 running');
      },
      stop: () => vm.stopAll(),
      state: () => ({
        targets: vm.runtime.targets.map(t => t.getName()),
        threads: vm.runtime.threads.length
      })
    };
    hud('gsdev host ready \u2014 waiting for a project');

    // Deterministic time + frame events. Wrapping _step (step entry) and
    // renderer.draw (render complete) is the only way to get frame-accurate
    // hooks: scratch-vm fires no frame/render event of its own. Agents prefer
    // subscribing to these over polling, so we both dispatch DOM events and
    // expose a per-frame recorder that captures every frame (no sampling).
    const runtime = vm.runtime;
    const gs = (window.__gsdev = window.__gsdev || { logs: [], stopped: false });
    gs.frame = 0;
    gs.rendered = 0;
    gs.frameEvents = 0;
    gs.renderEvents = 0;
    gs.errors = [];
    gs.logErrors = 0;

    // Render-side perf counters (see plan-perf-metrics.md): fps (step + render),
    // per-frame pen drawcount / stamps / GL draw calls, and frame rendertime.
    // Logic op counts are a separate metric (plan-logic-cost.md).
    gs.perf = {
      enabled: true,
      stepInterval: 0, renderInterval: 0,
      stepFps: 0, renderFps: 0,
      stepAt: 0, lastStepAt: 0, lastRenderAt: 0,
      rendertime: 0, rendertimeAvg: 0,
      steptime: 0, steptimeAvg: 0,
      drawcount: 0, stamps: 0, drawcalls: 0,
      _pen: 0, _stamps: 0, _gl: 0, _inDraw: false, _penMs: 0, _stampMs: 0,
      pentime: 0, pentimeAvg: 0, stamptime: 0,
      _installed: false, _wraps: [],
      logicEnabled: false, logicInstalled: false,
      counts: {}, blocks: 0, _opNames: [], _opCounts: [], _opTotals: [],
      _totals: {}, _blocksTotal: 0,
      // Procedure attribution (plan-procedure-hotspots.md). Only populated while
      // profileEnabled; the counter convention matches @blocks exactly.
      profileEnabled: false, prof: null
    };

    // Runtime error capture. scratch-vm fires no error event and has no
    // try/catch in _step or stepThreads, so a throwing thread escapes to the
    // setInterval callback. Wrapping _step captures it and lets the runtime keep
    // going; page-level errors (window.onerror / unhandledrejection) land in the
    // same list. Bounded so a thread that throws every frame cannot grow forever.
    const MAX_ERRORS = 200;
    const recordError = entry => {
      if (gs.errors.length < MAX_ERRORS) gs.errors.push(entry);
    };
    // @steptime: time spent in sequencer.stepThreads (VM/logic only, before the
    // draw). @rendertime is step + draw, so logic cost is validated against this.
    const sequencer = runtime.sequencer;
    if (sequencer && typeof sequencer.stepThreads === 'function') {
      const originalStepThreads = sequencer.stepThreads;
      sequencer.stepThreads = function (...args) {
        const t0 = performance.now();
        const result = originalStepThreads.apply(this, args);
        const st = performance.now() - t0;
        gs.perf.steptime = st;
        gs.perf.steptimeAvg = gs.perf.steptimeAvg ? gs.perf.steptimeAvg * 0.9 + st * 0.1 : st;
        const prof = gs.perf.prof;
        if (prof && prof.enabled) {
          prof.stepMsSum += st;
          prof.stepMsCount += 1;
          prof.lastStepMs = st;
        }
        return result;
      };
    }

    const originalStep = runtime._step;
    runtime._step = function (...args) {
      gs.frame += 1;
      gs.frameEvents += 1;
      const perf = gs.perf;
      const now = perf && performance.now();
      if (perf) {
        if (perf.lastStepAt) {
          const dt = now - perf.lastStepAt;
          perf.stepInterval = perf.stepInterval ? perf.stepInterval * 0.9 + dt * 0.1 : dt;
        }
        perf.lastStepAt = now;
        perf.stepAt = now;
        perf._pen = 0;
        perf._stamps = 0;
        // Close the previous step's profile sample (partial boundary steps are
        // excluded from the per-step series; the plan's observed-step means use
        // only these completed steps).
        const prof = perf.prof;
        if (prof && prof.enabled) {
          prof.steps += 1;
          prof.series.push({ frame: gs.frame - 1, t: now, step_ms: prof.lastStepMs,
                             draws: prof.drawStep, blocks: prof.stepOps });
          if (prof.series.length > prof.cap) {
            prof.series.splice(0, prof.series.length - prof.cap);
            prof.perStepCapped = true;
          }
          prof.stepOps = 0;
          prof.drawStep = 0;
        }
      }
      try {
        window.dispatchEvent(new CustomEvent('gsdev:frame', { detail: { frame: gs.frame } }));
      } catch (error) {}
      try {
        return originalStep.apply(this, args);
      } catch (error) {
        recordError({
          where: 'vm', frame: gs.frame,
          message: String((error && error.stack) || error)
        });
        return undefined;
      }
    };
    window.addEventListener('error', event => {
      recordError({
        where: 'page', frame: gs.frame,
        message: String((event.error && event.error.stack) || event.message || 'error')
      });
    });
    window.addEventListener('unhandledrejection', event => {
      recordError({
        where: 'page', frame: gs.frame,
        message: String((event.reason && event.reason.stack) || event.reason || 'unhandled rejection')
      });
    });

    const originalDraw = renderer.draw;
    renderer.draw = function (...args) {
      const perf = gs.perf;
      if (perf) { perf._inDraw = true; perf._gl = 0; }
      const result = originalDraw.apply(this, args);
      if (perf) {
        perf._inDraw = false;
        const now = performance.now();
        if (perf.lastRenderAt) {
          const dt = now - perf.lastRenderAt;
          perf.renderInterval = perf.renderInterval ? perf.renderInterval * 0.9 + dt * 0.1 : dt;
        }
        perf.lastRenderAt = now;
        perf.drawcount = perf._pen;
        perf.stamps = perf._stamps;
        if (perf.prof && perf.prof.enabled) {
          perf.prof.drawSum += perf._pen;
          perf.prof.drawStep += perf._pen;
          perf.prof.drawFrames += 1;
        }
        perf.drawcalls = perf._gl;
        perf.pentime = perf._penMs;
        perf.stamptime = perf._stampMs;
        perf.pentimeAvg = perf.pentimeAvg ? perf.pentimeAvg * 0.9 + perf.pentime * 0.1 : perf.pentime;
        perf._penMs = 0;
        perf._stampMs = 0;
        const rt = perf.stepAt ? now - perf.stepAt : 0;
        perf.rendertime = rt;
        perf.rendertimeAvg = perf.rendertimeAvg ? perf.rendertimeAvg * 0.9 + rt * 0.1 : rt;
        perf.stepFps = perf.stepInterval ? 1000 / perf.stepInterval : 0;
        perf.renderFps = perf.renderInterval ? 1000 / perf.renderInterval : 0;
        if (perf.logicInstalled) {
          const counts = {};
          let total = 0;
          const opCounts = perf._opCounts;
          for (let i = 0; i < opCounts.length; i++) {
            const n = opCounts[i];
            if (!n) continue;
            const op = perf._opNames[i];
            counts[op] = n;
            perf._opTotals[i] += n;
            perf._totals[op] = (perf._totals[op] || 0) + n;
            total += n;
            opCounts[i] = 0;
          }
          perf.counts = counts;
          perf.blocks = total;
          perf._blocksTotal += total;
        }
      }
      gs.rendered = gs.frame;
      gs.renderEvents += 1;
      try {
        window.dispatchEvent(new CustomEvent('gsdev:render', {
          detail: { frame: gs.frame, rendered: gs.rendered }
        }));
      } catch (error) {}
      return result;
    };

    // Per-draw instrumentation affects pen-heavy timings. setPerf(false) restores
    // these originals; frame events and step/render timing still remain active.
    const perfWraps = gs.perf._wraps;
    // Fixed-arity wrappers for the hot paths: no rest-args array allocation per call.
    const installPerf = () => {
      if (gs.perf._installed) return;
      gs.perf._installed = true;
      const P = gs.perf;
      const wrap = (obj, name, wrapped) => {
        const original = obj[name];
        wrapped.__gsdevPerf = true;
        perfWraps.push({ obj, name, original });
        obj[name] = wrapped;
      };
      // penStamp(penSkinID, stampID) — the pen `stamp` block.
      if (typeof renderer.penStamp === 'function' && !renderer.penStamp.__gsdevPerf) {
        const o = renderer.penStamp;
        wrap(renderer, 'penStamp', function (a, b) {
          if (!P.enabled) return o.call(this, a, b);
          const t0 = performance.now();
          const result = o.call(this, a, b);
          P._stamps += 1;
          P._stampMs += performance.now() - t0;
          return result;
        });
      }
      // PenSkin.drawLine(penAttributes, x0, y0, x1, y1) — covers drawPoint, which
      // delegates to it. The pen skin is created lazily, so hook createPenSkin.
      const wrapPenSkin = skin => {
        if (!skin || typeof skin.drawLine !== 'function' || skin.drawLine.__gsdevPerf) return;
        const o = skin.drawLine;
        wrap(skin, 'drawLine', function (pa, x0, y0, x1, y1) {
          if (!P.enabled) return o.call(this, pa, x0, y0, x1, y1);
          const t0 = performance.now();
          const result = o.call(this, pa, x0, y0, x1, y1);
          P._pen += 1;
          P._penMs += performance.now() - t0;
          return result;
        });
      };
      if (typeof renderer.createPenSkin === 'function' && !renderer.createPenSkin.__gsdevPerf) {
        const o = renderer.createPenSkin;
        wrap(renderer, 'createPenSkin', function () {
          const id = o.apply(this, arguments);
          wrapPenSkin(this._allSkins && this._allSkins[id]);
          return id;
        });
      }
      for (const skin of (renderer._allSkins || [])) wrapPenSkin(skin);
      // GL draw calls during the main draw pass (pen strokes are one batched call).
      const gl = renderer.gl;
      if (gl) {
        if (typeof gl.drawElements === 'function' && !gl.drawElements.__gsdevPerf) {
          const o = gl.drawElements;
          wrap(gl, 'drawElements', function (m, c, t, off) {
            if (P.enabled && P._inDraw) P._gl += 1;
            return o.call(this, m, c, t, off);
          });
        }
        if (typeof gl.drawArrays === 'function' && !gl.drawArrays.__gsdevPerf) {
          const o = gl.drawArrays;
          wrap(gl, 'drawArrays', function (m, f, c) {
            if (P.enabled && P._inDraw) P._gl += 1;
            return o.call(this, m, f, c);
          });
        }
      }
    };
    const removePerf = () => {
      for (const item of perfWraps.splice(0)) {
        if (item.obj[item.name] && item.obj[item.name].__gsdevPerf) {
          item.obj[item.name] = item.original;
        }
      }
      gs.perf._installed = false;
    };
    window.__host.setPerf = on => {
      gs.perf.enabled = !!on;
      if (on) installPerf();
      else removePerf();
      return gs.perf.enabled;
    };
    // Logic op-count instrumentation (opt-in; see plan-logic-cost.md). Wraps every
    // primitive and invalidates the per-target execute cache — the primitive
    // reference is captured per block there — so counts must be re-applied after
    // each project load. Off by default: per-execution counting perturbs hot loops.
    const logicWraps = [];
    const LOGIC_CATEGORIES = {
      varreads: ['data_variable'],
      varwrites: ['data_setvariableto', 'data_changevariableby'],
      listreads: ['data_itemoflist', 'data_lengthoflist', 'data_listcontainsitem',
        'data_itemnumoflist', 'data_listcontents'],
      listwrites: ['data_replaceitemoflist', 'data_addtolist', 'data_deleteoflist',
        'data_deletealloflist', 'data_insertatlist'],
      paramreads: ['argument_reporter_string_number', 'argument_reporter_boolean'],
      controlops: ['control_repeat', 'control_forever', 'control_if', 'control_if_else',
        'control_wait', 'control_wait_until', 'control_repeat_until']
    };
    const clearExecuteCache = () => {
      for (const target of runtime.targets) {
        if (target.blocks && target.blocks._cache) target.blocks._cache._executeCached = {};
      }
    };
    // --- procedure attribution (plan-procedure-hotspots.md) -----------------
    // Counting convention matches @blocks exactly: only runtime._primitives
    // dispatches are counted (no hats, literals, cached reporters or menu
    // shadows). The active procedure is read from the thread's own stack:
    // stepToProcedure pushes the procedure *definition*, so the nearest
    // procedures_call block at or below the current frame is the active callee.
    const PROF_STEP_CAP = 2048;
    const spriteLabel = target => {
      try {
        if (!target) return 'stage';
        const cloneSprite = target.isOriginal === false && target.sprite && target.sprite.name;
        const name = cloneSprite || (target.getName ? target.getName()
          : (target.sprite && target.sprite.name));
        return name === 'Stage' ? 'stage' : String(name || 'unknown');
      } catch (error) { return 'unknown'; }
    };
    const procRecord = (prof, target, proccode) => {
      const key = spriteLabel(target) + ' :: ' + proccode;
      let rec = prof.procedures[key];
      if (!rec) {
        rec = prof.procedures[key] = {
          key, sprite: spriteLabel(target), name: proccode,
          calls: 0, self: 0, inclusive: 0, ops: {}
        };
      }
      return rec;
    };
    const topFrameIdentity = thread => {
      try {
        const block = thread.target.blocks.getBlock(thread.topBlock);
        return { key: spriteLabel(thread.target) + ' :: '
          + (block ? block.opcode : 'top-level'),
          proccode: block ? block.opcode : 'top-level' };
      } catch (error) { return null; }
    };
    const initProf = () => {
      gs.perf.prof = {
        enabled: true, steps: 0, stepOps: 0, totalOps: 0, unattributed: 0,
        startedAt: performance.now(), procedures: {}, perStepCapped: false,
        stepMsSum: 0, stepMsCount: 0, drawSum: 0, drawFrames: 0,
        lastStepMs: 0, drawStep: 0, series: [],
        cap: PROF_STEP_CAP
      };
    };
    const attributeOp = (P, op, util) => {
      const prof = P.prof;
      if (!prof || !prof.enabled) return;
      prof.stepOps += 1;
      prof.totalOps += 1;
      const thread = util && util.thread;
      if (!thread || !thread.target || !thread.target.blocks || !thread.stack) {
        prof.unattributed += 1;
        return;
      }
      const blocks = thread.target.blocks;
      const stack = thread.stack;
      // For procedures_call the call block is the current top frame and the
      // callee has not been entered: call setup belongs to the caller (plan 4).
      const entering = op === 'procedures_call';
      const limit = entering ? stack.length - 1 : stack.length;
      const active = [];
      for (let i = 0; i < limit; i++) {
        const id = stack[i];
        if (!id) continue;
        const block = blocks.getBlock(id);
        if (block && block.opcode === 'procedures_call') {
          const code = (block.mutation && block.mutation.proccode) || 'call';
          active.push({ key: spriteLabel(thread.target) + ' :: ' + code, proccode: code });
        }
      }
      if (entering && stack.length) {
        const call = blocks.getBlock(stack[stack.length - 1]);
        if (call && call.opcode === 'procedures_call') {
          const code = (call.mutation && call.mutation.proccode) || 'call';
          procRecord(prof, thread.target, code).calls += 1;
        }
      }
      const top = topFrameIdentity(thread);
      const self = active.length ? active[active.length - 1] : top;
      if (!self) {
        prof.unattributed += 1;
      } else {
        const rec = procRecord(prof, thread.target, self.proccode);
        rec.self += 1;
        rec.ops[op] = (rec.ops[op] || 0) + 1;
      }
      // Inclusive: credit each distinct active identity once per operation
      // (recursion collapses to one key; this is a flat view, not a call tree).
      const seen = {};
      for (const frame of active) {
        if (seen[frame.key]) continue;
        seen[frame.key] = true;
        procRecord(prof, thread.target, frame.proccode).inclusive += 1;
      }
      if (top && !seen[top.key]) {
        procRecord(prof, thread.target, top.proccode).inclusive += 1;
      }
    };
    const reportData = prof => {
      const procedures = Object.keys(prof.procedures).map(k => {
        const r = prof.procedures[k];
        const dominant = Object.keys(r.ops).map(op => [op, r.ops[op]])
          .sort((a, b) => b[1] - a[1]).slice(0, 10);
        return { key: r.key, sprite: r.sprite, name: r.name, calls: r.calls,
                 self: r.self, inclusive: r.inclusive,
                 share: prof.totalOps ? r.self / prof.totalOps : 0, dominant };
      }).sort((a, b) => b.self - a.self);
      let selfTotal = 0;
      for (const p of procedures) selfTotal += p.self;
      const series = prof.series.slice();
      const perStep = series.map(s => s.blocks);
      let maxStep = 0;
      let sumSteps = 0;
      let sumMs = 0;
      let sumDraws = 0;
      for (const s of series) {
        sumSteps += s.blocks; if (s.blocks > maxStep) maxStep = s.blocks;
        sumMs += s.step_ms; sumDraws += s.draws;
      }
      const n = series.length;
      return {
        enabled: prof.enabled, steps: n, step_boundaries: prof.steps,
        steps_total: prof.steps, series_start_index: prof.steps - n,
        window_ms: prof.startedAt ? performance.now() - prof.startedAt : 0,
        total_ops: prof.totalOps, unattributed: prof.unattributed, unknown: prof.unattributed,
        self_total: selfTotal,
        ops_per_step_mean: n ? sumSteps / n : 0,
        ops_per_step_max: maxStep, per_step: perStep, per_step_capped: prof.perStepCapped,
        step_series: series,
        step_ms_mean: n ? sumMs / n : 0, step_samples: n,
        draws_per_step: n ? sumDraws / n : 0,
        rendered_frames: prof.drawFrames,
        procedures
      };
    };

    const installLogic = () => {
      const P = gs.perf;
      const opNames = Object.keys(runtime._primitives);
      P._opNames = opNames;
      P._opCounts = new Array(opNames.length).fill(0);
      P._opTotals = new Array(opNames.length).fill(0);
      opNames.forEach((op, i) => {
        const original = runtime._primitives[op];
        if (typeof original !== 'function' || original.__gsdevLogic) return;
        const wrapped = function (args, util) {
          P._opCounts[i] += 1;
          if (P.profileEnabled) attributeOp(P, op, util);
          return original.call(this, args, util);
        };
        wrapped.__gsdevLogic = true;
        logicWraps.push({ op, original });
        runtime._primitives[op] = wrapped;
      });
      clearExecuteCache();
      P.logicInstalled = true;
      P.counts = {};
      P.blocks = 0;
      P._totals = {};
      P._blocksTotal = 0;
      if (P.profileEnabled) initProf();
    };
    const removeLogic = () => {
      for (const item of logicWraps.splice(0)) {
        if (runtime._primitives[item.op] && runtime._primitives[item.op].__gsdevLogic) {
          runtime._primitives[item.op] = item.original;
        }
      }
      const P = gs.perf;
      P.logicInstalled = false;
      P.counts = {};
      P.blocks = 0;
      P._opNames = [];
      P._opCounts = [];
      P._opTotals = [];
      P._totals = {};
      P._blocksTotal = 0;
      clearExecuteCache();
    };
    const refreshLogic = () => {
      if (!gs.perf.logicEnabled) return;
      removeLogic();
      installLogic();
    };
    const logicSummary = () => {
      const P = gs.perf;
      const counts = P.counts || {};
      const totalsCounts = P._totals || {};
      const categories = {};
      const totals = { blocks: P._blocksTotal || 0, categories: {} };
      for (const name of Object.keys(LOGIC_CATEGORIES)) {
        let n = 0;
        let t = 0;
        for (const op of LOGIC_CATEGORIES[name]) {
          n += counts[op] || 0;
          t += totalsCounts[op] || 0;
        }
        categories[name] = n;
        totals.categories[name] = t;
      }
      const top = Object.keys(counts).map(op => [op, counts[op]])
        .sort((a, b) => b[1] - a[1]).slice(0, 15);
      return { blocks: P.blocks || 0, categories, top, enabled: P.logicInstalled, totals };
    };
    window.__host.setLogic = on => {
      gs.perf.logicEnabled = !!on;
      if (on) { removeLogic(); installLogic(); }
      else removeLogic();
      return gs.perf.logicEnabled;
    };
    window.__host.logic = () => logicSummary();

    // Procedure profiling (plan-procedure-hotspots.md). Same wrapper/counting
    // convention as @blocks; the report is counts + attribution only.
    window.__host.setProfiling = on => {
      const P = gs.perf;
      P.profileEnabled = !!on;
      P.logicEnabled = P.logicEnabled || P.profileEnabled;
      removeLogic();
      if (P.logicEnabled) installLogic();
      if (!P.profileEnabled) P.prof = null;
      return P.profileEnabled;
    };
    window.__host.profileReset = () => {
      if (!gs.perf.profileEnabled) return false;
      initProf();
      return true;
    };
    window.__host.profile = () => (gs.perf.prof ? reportData(gs.perf.prof)
      : { enabled: false, steps: 0, step_boundaries: 0, steps_total: 0,
          series_start_index: 0, window_ms: 0, total_ops: 0,
          unattributed: 0, unknown: 0, self_total: 0, ops_per_step_mean: 0,
          ops_per_step_max: 0, per_step: [], per_step_capped: false, step_series: [],
          step_ms_mean: 0, step_samples: 0, draws_per_step: 0, rendered_frames: 0,
          procedures: [] });
    // Cheap incremental read for live follow: returns the bounded series entries
    // with absolute index >= since.
    window.__host.profileSteps = since => {
      const prof = gs.perf.prof;
      if (!prof || !prof.series) return { series_start: 0, steps: [] };
      const seriesStart = prof.steps - prof.series.length;
      const offset = Math.max(0, (Number(since) || 0) - seriesStart);
      return { series_start: seriesStart + offset, steps: prof.series.slice(offset) };
    };
    window.__host.profileValue = name => {
      const report = window.__host.profile();
      switch (String(name || '').toLowerCase()) {
      case 'steps': return report.steps;
      case 'totalops': case 'blocks': return report.total_ops;
      case 'unattributed': case 'unknown': return report.unattributed;
      case 'selftotal': return report.self_total;
      case 'opsperstep': return report.ops_per_step_mean;
      default: return undefined;
      }
    };

    // GL backend, for "are we actually on the GPU?" checks. Production Scratch
    // runs on the GPU; a software (SwiftShader) fallback is a silent, large
    // perf cliff, so surface the renderer and flag software explicitly.
    const gpuInfo = (() => {
      try {
        const gl = renderer.gl;
        if (!gl) return { vendor: '', renderer: '' };
        const ext = gl.getExtension('WEBGL_debug_renderer_info');
        return {
          vendor: String(ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR)),
          renderer: String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER))
        };
      } catch (error) {
        return { vendor: '', renderer: '', error: String(error) };
      }
    })();
    const softwareGL = () =>
      /swiftshader|software|llvmpipe|basic render|mesa offscreen/i.test(gpuInfo.renderer || '');
    window.__host.gpu = () => ({
      vendor: gpuInfo.vendor, renderer: gpuInfo.renderer, software: softwareGL()
    });

    const perfFresh = t => t && performance.now() - t < 1500;
    window.__host.perf = () => ({
      stepFps: perfFresh(gs.perf.lastStepAt) ? gs.perf.stepFps : 0,
      renderFps: perfFresh(gs.perf.lastRenderAt) ? gs.perf.renderFps : 0,
      drawcount: gs.perf.drawcount,
      stamps: gs.perf.stamps,
      drawcalls: gs.perf.drawcalls,
      pentime: gs.perf.pentime,
      pentimeAvg: gs.perf.pentimeAvg,
      stamptime: gs.perf.stamptime,
      penus: gs.perf.drawcount ? (gs.perf.pentime * 1000) / gs.perf.drawcount : 0,
      stampus: gs.perf.stamps ? (gs.perf.stamptime * 1000) / gs.perf.stamps : 0,
      rendertime: gs.perf.rendertime,
      rendertimeAvg: gs.perf.rendertimeAvg,
      steptime: gs.perf.steptime,
      steptimeAvg: gs.perf.steptimeAvg,
      blocks: gs.perf.blocks,
      frame: gs.frame,
      rendered: gs.rendered,
      enabled: gs.perf.enabled,
      gpu: gpuInfo.renderer,
      gpuVendor: gpuInfo.vendor,
      software: softwareGL()
    });
    window.__host.perfValue = name => {
      const p = window.__host.perf();
      switch (String(name || '').toLowerCase()) {
      case 'fps': case 'renderfps': return p.renderFps;
      case 'stepfps': return p.stepFps;
      case 'drawcount': return p.drawcount;
      case 'stamps': return p.stamps;
      case 'drawcalls': return p.drawcalls;
      case 'pentime': return p.pentime;
      case 'pentimeavg': return p.pentimeAvg;
      case 'stamptime': return p.stamptime;
      case 'penus': return p.penus;
      case 'stampus': return p.stampus;
      case 'rendertime': return p.rendertime;
      case 'rendertimeavg': return p.rendertimeAvg;
      case 'steptime': return p.steptime;
      case 'steptimeavg': return p.steptimeAvg;
      case 'blocks': return p.blocks;
      case 'varreads': case 'varwrites': case 'listreads': case 'listwrites':
      case 'paramreads': case 'controlops':
        return logicSummary().categories[String(name).toLowerCase()];
      case 'totalblocks': return logicSummary().totals.blocks;
      case 'totalvarreads': case 'totalvarwrites': case 'totallistreads':
      case 'totallistwrites': case 'totalparamreads': case 'totalcontrolops':
        return logicSummary().totals.categories[String(name).toLowerCase().slice(5)];
      case 'frame': return p.frame;
      case 'rendered': return p.rendered;
      case 'gpu': case 'gpurenderer': return p.gpu;
      case 'gpuvendor': return p.gpuVendor;
      case 'software': return p.software ? 1 : 0;
      default: return undefined;
      }
    };
    installPerf();

    window.__host.frame = () => gs.frame;
    window.__host.rendered = () => gs.rendered;
    window.__host.events = () => Object.assign({
      frame: gs.frame, rendered: gs.rendered,
      frameEvents: gs.frameEvents, renderEvents: gs.renderEvents
    }, window.__host.perf());
    window.__host.pause = () => {
      clearInterval(runtime._steppingInterval);
      runtime._steppingInterval = null;
      const p = gs.perf;
      if (p) {
        p.stepInterval = 0; p.renderInterval = 0;
        p.stepFps = 0; p.renderFps = 0;
        p.lastStepAt = 0; p.lastRenderAt = 0;
      }
    };
    window.__host.resume = () => {
      applyCompatibilityMode();
      runtime.start();
    };
    window.__host.step = count => {
      for (let i = 0; i < count; i++) runtime._step();
      return gs.frame;
    };
    window.__host.restart = () => {
      applyCompatibilityMode();
      runtime.start();
      vm.stopAll();
      vm.greenFlag();
    };
    // Event-driven frame wait (no polling): resolves on the Nth 'gsdev:frame'.
    window.__host.waitFrames = (count, timeoutMs) => new Promise(resolve => {
      const target = gs.frame + count;
      let done = false;
      if (gs.frame >= target) {
        resolve({ ok: true, frame: gs.frame });
        return;
      }
      const listener = event => {
        if (done || event.detail.frame < target) return;
        done = true;
        window.removeEventListener('gsdev:frame', listener);
        resolve({ ok: true, frame: gs.frame });
      };
      window.addEventListener('gsdev:frame', listener);
      setTimeout(() => {
        if (done) return;
        done = true;
        window.removeEventListener('gsdev:frame', listener);
        resolve({ ok: false, frame: gs.frame, timeout: true });
      }, timeoutMs);
    });

    // Target addressing + introspection. A selector is a sprite name, with an
    // optional `#N` clone suffix: `main` is the original, `main#1` the first
    // clone (clones are addressed in `sprite.clones` order, the original at 0).
    // Clone indices are only stable within a frame — they shift as clones are
    // created and deleted.
    window.__host.findTarget = name => {
      if (name === null || name === undefined) return null;
      let base = String(name);
      let clone = null;
      const hash = base.lastIndexOf('#');
      if (hash > 0) {
        const parsed = parseInt(base.slice(hash + 1), 10);
        if (!Number.isNaN(parsed)) {
          clone = parsed;
          base = base.slice(0, hash);
        }
      }
      if (base === 'stage' || base === 'Stage') return vm.runtime.getTargetForStage() || null;
      const any = vm.runtime.targets.find(t => t.sprite && t.sprite.name === base);
      if (!any) return null;
      const clones = any.sprite.clones;
      if (clone === null) return clones.find(t => t.isOriginal) || clones[0] || null;
      return clones[clone] || null;
    };
    const summarize = value => {
      if (Array.isArray(value)) {
        const text = JSON.stringify(value);
        return text.length > 200 ? text.slice(0, 200) + '...(' + value.length + ')' : text;
      }
      if (value !== null && typeof value === 'object') {
        try { return JSON.stringify(value); } catch (error) { return String(value); }
      }
      return value;
    };
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
      for (const key in target.variables) {
        const variable = target.variables[key];
        out.push({
          name: variable.name,
          type: variable.type === '' ? 'scalar' : variable.type,
          scope: variable.type === 'broadcast'
            ? 'broadcast'
            : (target.isStage ? 'global' : 'sprite'),
          value: summarize(variable.value)
        });
      }
      return out;
    };
    window.__host.inspect = name => {
      let extensions = [];
      try {
        const manager = vm.extensionManager ||
          (vm.runtime.extensions && vm.runtime.extensions);
        const loaded = manager && manager._loadedExtensions;
        if (loaded) extensions = Array.from(loaded.keys());
      } catch (error) {}
      const describe = target => Object.assign(targetProps(target), {
        variables: targetVariables(target)
      });
      if (name === null || name === undefined || name === '') {
        return {
          extensions,
          targetCount: vm.runtime.targets.length,
          targets: vm.runtime.targets.map(describe)
        };
      }
      const target = window.__host.findTarget(name);
      if (!target) return { error: 'target not found: ' + name };
      return { extensions, target: describe(target) };
    };
    window.__host.props = name => {
      const target = window.__host.findTarget(name);
      if (!target) return { error: 'target not found: ' + name };
      return targetProps(target);
    };
    window.__host.getProp = (name, prop) => {
      const target = window.__host.findTarget(name);
      if (!target) return { error: 'target not found: ' + name };
      const props = targetProps(target);
      if (!Object.prototype.hasOwnProperty.call(props, prop)) {
        return { error: 'unknown property: ' + prop };
      }
      return { name: target.getName(), property: prop, value: props[prop] };
    };
    window.__host.setProp = (name, prop, value) => {
      const target = window.__host.findTarget(name);
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
        else return { error: "layer must be 'front' or 'back'" };
        break;
      }
      default: return { error: 'not settable: ' + prop };
      }
      return Object.assign({ ok: true }, targetProps(target));
    };
    window.__host.clones = () => {
      const out = {};
      for (const target of vm.runtime.targets) {
        if (target.isStage || !target.isOriginal || !target.sprite) continue;
        out[target.getName()] = target.sprite.clones.length - 1;
      }
      return out;
    };
    window.__host.errors = () => ({
      count: gs.errors.length,
      logErrors: gs.logErrors,
      errors: gs.errors.slice()
    });
    // One evaluate for the run loop: drain logs, read the stopped flag, and
    // return only errors added since `since` (so a full list of stack traces is
    // not re-serialized every poll). Kept separate from `errors()` for callers
    // that want the whole list.
    window.__host.poll = since => ({
      logs: gs.logs.splice(0, gs.logs.length),
      stopped: gs.stopped,
      count: gs.errors.length,
      logErrors: gs.logErrors,
      errors: gs.errors.slice(Number.isFinite(since) ? Math.max(0, since) : 0)
    });
    window.__host.clearErrors = () => {
      gs.errors.length = 0;
      gs.logErrors = 0;
      return { ok: true };
    };

    // Shared helpers for the frame recorder / condition watcher.
    const resolveVar = spec => {
      if (spec.target === '@perf') return { variable: { __perf: spec.name } };
      const target = window.__host.findTarget(spec.target);
      if (!target) return { error: 'target not found: ' + spec.target };
      let variable = null;
      for (const key in target.variables) {
        if (target.variables[key].name === spec.name) { variable = target.variables[key]; break; }
      }
      if (!variable) return { error: 'variable not found: ' + spec.name };
      return { variable };
    };
    const readVar = (variable, index) => {
      if (variable && variable.__perf) return window.__host.perfValue(variable.__perf);
      return (index === null || index === undefined)
        ? variable.value
        : (Array.isArray(variable.value) ? variable.value[index - 1] : undefined);
    };
    const format = value => (Array.isArray(value) ? JSON.stringify(value).slice(0, 200) : value);
    const compareJs = (a, op, b) => {
      const na = Number(a), nb = Number(b);
      const numeric = a !== '' && b !== '' && isFinite(na) && isFinite(nb);
      const x = numeric ? na : String(a);
      const y = numeric ? nb : String(b);
      if (op === '==') return x === y;
      if (op === '!=') return x !== y;
      if (op === '>') return x > y;
      if (op === '<') return x < y;
      if (op === '>=') return x >= y;
      if (op === '<=') return x <= y;
      return false;
    };

    // record(specs, max): capture [frame, ...values] for every completed frame.
    // Frames are never skipped while active: when the buffer hits `max` the
    // recorder stops and sets `overflow` (explicit), rather than dropping rows.
    window.__host.record = (specs, max) => {
      const variables = [];
      for (const spec of specs) {
        const resolved = resolveVar(spec);
        if (resolved.error) return resolved;
        variables.push({ spec, variable: resolved.variable });
      }
      if (gs.recorder && gs.recorder.listener) {
        window.removeEventListener('gsdev:render', gs.recorder.listener);
      }
      const recorder = (gs.recorder = {
        rows: [], active: true, overflow: false, max: max || 100000, variables, listener: null
      });
      recorder.listener = event => {
        if (!recorder.active) return;
        if (recorder.rows.length >= recorder.max) {
          recorder.overflow = true;
          recorder.active = false;
          return;
        }
        const row = [event.detail.frame];
        for (const item of recorder.variables) {
          row.push(format(readVar(item.variable, item.spec.index)));
        }
        recorder.rows.push(row);
      };
      window.addEventListener('gsdev:render', recorder.listener);
      return { ok: true, labels: specs.map(s => s.label) };
    };
    window.__host.trace = clear => {
      const recorder = gs.recorder;
      const until = gs.until
        ? {
            hit: gs.until.hit, frame: gs.until.frame, value: gs.until.value,
            paused: gs.until.paused, active: gs.until.active, expired: gs.until.expired
          }
        : null;
      if (!recorder) return { error: 'not recording', until };
      const out = {
        labels: recorder.variables.map(i => i.spec.label),
        rows: recorder.rows,
        overflow: recorder.overflow,
        active: recorder.active,
        max: recorder.max,
        until
      };
      if (clear) recorder.rows = [];
      return out;
    };
    window.__host.stopRecord = () => {
      const recorder = gs.recorder;
      if (!recorder) return { error: 'not recording' };
      recorder.active = false;
      if (recorder.listener) window.removeEventListener('gsdev:render', recorder.listener);
      return { ok: true, rows: recorder.rows.length, overflow: recorder.overflow };
    };

    // until(spec, op, value, pause, maxFrames): per-frame condition watcher.
    // Records the exact frame (and value) where the condition first held, and
    // optionally pauses the runtime on that frame (race-free freeze).
    window.__host.until = (spec, op, value, pause, maxFrames) => {
      const resolved = resolveVar(spec);
      if (resolved.error) return resolved;
      if (gs.until && gs.until.listener) window.removeEventListener('gsdev:render', gs.until.listener);
      const state = (gs.until = {
        hit: false, frame: null, value: null, paused: false, active: true, expired: false, listener: null
      });
      const start = gs.frame;
      state.listener = event => {
        if (!state.active) return;
        if (event.detail.frame - start > maxFrames) {
          state.active = false;
          state.expired = true;
          window.removeEventListener('gsdev:render', state.listener);
          return;
        }
        const actual = readVar(resolved.variable, spec.index);
        if (compareJs(actual, op, value)) {
          state.active = false;
          state.hit = true;
          state.frame = event.detail.frame;
          state.value = format(actual);
          window.removeEventListener('gsdev:render', state.listener);
          if (pause) {
            window.__host.pause();
            state.paused = true;
          }
        }
      };
      window.addEventListener('gsdev:render', state.listener);
      return { ok: true };
    };

    // Promise-based, event-driven waits. `waitUntil` resolves on the first
    // 'gsdev:render' where a variable matches; `waitPixel` does the same for one
    // framebuffer pixel. Neither polls on a timer: the check runs as a direct
    // consequence of a frame, and a wall-clock timeout is only a backstop.
    window.__host.waitUntil = (spec, op, value, timeoutMs) => new Promise(resolve => {
      const resolved = resolveVar(spec);
      if (resolved.error) {
        resolve({ ok: false, error: resolved.error });
        return;
      }
      let done = false;
      let listener = null;
      const finish = extra => {
        if (done) return;
        done = true;
        if (listener) window.removeEventListener('gsdev:render', listener);
        resolve(Object.assign({ frame: gs.frame, value: format(readVar(resolved.variable, spec.index)) }, extra));
      };
      const check = () => {
        const actual = readVar(resolved.variable, spec.index);
        if (compareJs(actual, op, value)) {
          finish({ ok: true, value: format(actual) });
          return true;
        }
        return false;
      };
      listener = () => { check(); };
      if (check()) return;
      window.addEventListener('gsdev:render', listener);
      setTimeout(() => finish({ ok: false, timeout: true }), timeoutMs);
    });

    const snapshotPixel = (x, y) => new Promise(resolve => {
      const renderer = vm.runtime.renderer;
      if (!renderer || typeof renderer.requestSnapshot !== 'function') {
        resolve(null);
        return;
      }
      try {
        renderer.requestSnapshot(url => {
          if (!url) { resolve(null); return; }
          const img = new Image();
          img.onload = () => {
            try {
              const canvas = document.createElement('canvas');
              canvas.width = img.width;
              canvas.height = img.height;
              const context = canvas.getContext('2d');
              context.drawImage(img, 0, 0, img.width, img.height);
              const sw = vm.runtime.constructor.STAGE_WIDTH || 480;
              const sh = vm.runtime.constructor.STAGE_HEIGHT || 360;
              const ix = Math.round((x / sw + 0.5) * img.width);
              const iy = Math.round((0.5 - y / sh) * img.height);
              const px = context.getImageData(
                Math.min(ix, img.width - 1), Math.min(iy, img.height - 1), 1, 1
              ).data;
              const hex = '#' + [px[0], px[1], px[2]]
                .map(v => v.toString(16).padStart(2, '0')).join('');
              resolve(hex);
            } catch (error) { resolve(null); }
          };
          img.onerror = () => resolve(null);
          img.src = url;
        });
      } catch (error) { resolve(null); }
    });
    // Read one stage pixel straight from the renderer's WebGL back buffer. This
    // runs synchronously inside the wrapped renderer.draw, before the browser
    // composites, so the frame is still present; `readPixels` is bottom-up, which
    // already matches Scratch's +y-up stage coords. Far cheaper than a snapshot.
    const readPixel = (x, y) => {
      const gl = renderer.gl;
      if (!gl || typeof gl.readPixels !== 'function') return null;
      const width = gl.drawingBufferWidth;
      const height = gl.drawingBufferHeight;
      const sw = vm.runtime.constructor.STAGE_WIDTH || 480;
      const sh = vm.runtime.constructor.STAGE_HEIGHT || 360;
      const ix = Math.min(width - 1, Math.max(0, Math.round((x / sw + 0.5) * width)));
      const iy = Math.min(height - 1, Math.max(0, Math.round((y / sh + 0.5) * height)));
      const px = new Uint8Array(4);
      try {
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.readPixels(ix, iy, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
      } catch (error) { return null; }
      return '#' + [px[0], px[1], px[2]].map(v => v.toString(16).padStart(2, '0')).join('');
    };
    window.__host.waitPixel = (x, y, hex, timeoutMs) => new Promise(resolve => {
      const want = String(hex).toLowerCase();
      let done = false;
      const finish = extra => {
        if (done) return;
        done = true;
        window.removeEventListener('gsdev:render', listener);
        resolve(Object.assign({ x, y, want, frame: gs.frame }, extra));
      };
      const listener = () => {
        if (done) return;
        const actual = readPixel(x, y);
        if (actual && actual.toLowerCase() === want) finish({ ok: true, hex: actual });
      };
      window.addEventListener('gsdev:render', listener);
      setTimeout(() => finish({ ok: false, timeout: true }), timeoutMs);
      // One snapshot check so an already-matching pixel resolves even when the
      // runtime is paused and never fires 'gsdev:render'.
      snapshotPixel(x, y).then(actual => {
        if (done || !actual) return;
        if (actual.toLowerCase() === want) finish({ ok: true, hex: actual });
      }).catch(() => {});
    });

    // Broadcast helpers. `startHats` returns the threads it created (Scratch's
    // own `event_broadcast` primitive uses exactly this call), so a broadcast is
    // just startHats with the message name (startHats uppercases it to match
    // case-insensitively). `broadcastWait` is
    // event-driven, not a poll: it resolves on the first 'gsdev:render' (end of a
    // step, after the sequencer has retired finished threads) where none of the
    // started threads are still in runtime.threads. A wall-clock timeout covers a
    // paused runtime, which never steps and so never fires the event.
    window.__host.broadcast = name => {
      const started = vm.runtime.startHats(
        'event_whenbroadcastreceived', { BROADCAST_OPTION: name }) || [];
      return { name, threads: started.length };
    };
    window.__host.broadcastWait = (name, timeoutMs) => new Promise(resolve => {
      const rt = vm.runtime;
      const started = rt.startHats(
        'event_whenbroadcastreceived', { BROADCAST_OPTION: name }) || [];
      const startFrame = gs.frame;
      const result = extra => Object.assign({
        name, threads: started.length, frame: startFrame
      }, extra);
      const pending = () => started.some(thread => rt.threads.indexOf(thread) > -1);
      if (!pending()) {
        resolve(result({ ok: true, endFrame: gs.frame, waitedFrames: 0 }));
        return;
      }
      let done = false;
      const finish = extra => {
        if (done) return;
        done = true;
        window.removeEventListener('gsdev:render', listener);
        resolve(result(Object.assign({
          endFrame: gs.frame, waitedFrames: gs.frame - startFrame
        }, extra)));
      };
      const listener = () => { if (!pending()) finish({ ok: true }); };
      window.addEventListener('gsdev:render', listener);
      setTimeout(() => finish({ ok: false, timeout: true }), timeoutMs);
    });

    // Capture goboscript log/warn/error (custom-block calls with zero-width
    // proccodes), same approach as the Scratch Desktop shim in gsdev.py.
    const g = (window.__gsdev = window.__gsdev || { logs: [], stopped: false });
    g.logs = [];
    g.stopped = false;
    g.seen = [];
    try {
      const primitive = vm.runtime._primitives;
      const Z = '\u200B\u200B';
      const levels = {};
      levels[Z + 'log' + Z + ' %s'] = 'log';
      levels[Z + 'warn' + Z + ' %s'] = 'warn';
      levels[Z + 'error' + Z + ' %s'] = 'error';
      const existing = primitive['procedures_call'];
      g.installed = !!existing;
      if (existing && !existing.__gsdev) {
        const wrapped = function (args, util) {
          try {
            const proccode = args && args.mutation && args.mutation.proccode;
            if (g.seen.length < 40) g.seen.push(proccode);
            const level = levels[proccode];
            if (level) {
              let value = args.arg0;
              if (value === undefined) {
                for (const key in args) {
                  if (key !== 'mutation') {
                    value = args[key];
                    break;
                  }
                }
              }
              let sprite = 'unknown';
              try {
                sprite = util && util.target ? util.target.getName() : 'unknown';
              } catch (error) {}
              g.logs.push({
                sprite: sprite === 'Stage' ? 'stage' : sprite,
                level,
                value: String(value),
                time: Date.now()
              });
              if (level === 'error') g.logErrors += 1;
            }
          } catch (error) {}
          return existing.apply(this, arguments);
        };
        wrapped.__gsdev = true;
        wrapped.__gsdevOriginal = existing;
        primitive['procedures_call'] = wrapped;
      }
      vm.on('PROJECT_RUN_STOP', () => {
        g.logs.push(null);
        g.stopped = true;
        hud('gsdev host \u2014 project stopped');
      });
    } catch (error) {
      g.error = String(error);
    }
  } catch (error) {
    marks.fatal = String((error && error.stack) || error);
    hud('gsdev host error: ' + marks.fatal, true);
  }
  marks.ready = performance.now();
})();
