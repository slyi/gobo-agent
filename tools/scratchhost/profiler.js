/* Shared procedure profiler for the Scratch VM (see plan-procedure-hotspots.md).
 *
 * One implementation, loaded two ways: `host.html` loads it before `host.js` for
 * the local host; `gsbridge` injects the same file into third-party pages
 * (github player, scratch.mit.edu). The module owns its own hooks on
 * `runtime.sequencer.stepThreads`, `runtime._step` and `renderer.draw`, plus the
 * per-primitive wrapper and the pen hooks, so a host only has to instantiate it
 * and expose the API.
 *
 *   const profiler = window.GsdevProfiler({ runtime, renderer });
 *   profiler.setEnabled(true);       // install wrappers + start a window
 *   profiler.reset();                // fresh window, wrappers stay
 *   profiler.report();               // report object (same shape for host + bridge)
 *   profiler.steps(since);           // bounded per-step series for live follow
 *   profiler.value(name);            // cheap scalar read
 *   profiler.refresh();              // re-wrap after a project load
 *
 * Counting convention: only `runtime._primitives` dispatches (no hats, literals,
 * cached reporters or menu shadows). The active procedure is the nearest
 * `procedures_call` block at or below the current stack frame.
 */
(function () {
  'use strict';

  const STEP_CAP = 2048;
  const FRAME_CAP = 256;

  const DISABLED_REPORT = () => ({
    schema: 1, instrumented: true, enabled: false, steps: 0, step_boundaries: 0,
    steps_total: 0, series_start_index: 0, window_ms: 0, total_ops: 0,
    unattributed: 0, unknown: 0, self_total: 0, ops_per_step_mean: 0,
    ops_per_step_max: 0, per_step: [], per_step_capped: false, step_series: [],
    step_ms_mean: 0, step_samples: 0, draws_per_step: 0, rendered_frames: 0,
    work_time_ms: 0, budget_steps: 0, ids: {}, frames: [],
    frames_truncated: false, edges: [], procedures: []
  });

  window.GsdevProfiler = function createProfiler(env) {
    const runtime = env.runtime;
    const renderer = env.renderer;

    // Per-window state and the wrappers installed while profiling is on.
    const st = { frame: 0, prof: null, opNames: [], profileEnabled: false };
    const primitiveWraps = [];
    const drawWraps = [];
    const runtimeWraps = [];

    const spriteLabel = target => {
      try {
        if (!target) return 'stage';
        const cloneSprite = target.isOriginal === false && target.sprite && target.sprite.name;
        const name = cloneSprite || (target.getName ? target.getName()
          : (target.sprite && target.sprite.name));
        return name === 'Stage' ? 'stage' : String(name || 'unknown');
      } catch (error) { return 'unknown'; }
    };
    // Identities are interned to integer ids. The hot path then works on numbers
    // and arrays only: string keys and per-op string building are what V8 cannot
    // inline-cache, and they dominated the profiler's own overhead (~1.3-2.8us/op).
    const newRec = (prof, sprite, name) => {
      const rec = {
        id: prof.recs.length, key: sprite + ' :: ' + name, sprite, name,
        calls: 0, self: 0, inclusive: 0,
        uiOps: 0, warpOps: 0, held: 0,
        timeMs: 0, uiTimeMs: 0, warpTimeMs: 0,
        stepSelf: 0, stepDirty: -1,
        opCounts: new Uint32Array(st.opNames.length)
      };
      prof.recs.push(rec);
      return rec;
    };
    const recIdFor = (prof, sprite, name) => {
      const key = sprite + ' :: ' + name;
      let id = prof.byKey[key];
      if (id === undefined) {
        id = newRec(prof, sprite, name).id;
        prof.byKey[key] = id;
      }
      return id;
    };
    // Block ids are only unique *within* a target: goboscript reuses one id for
    // identical blocks in different sprites, so any block-id cache must be keyed by
    // target as well. Clones share their sprite's name (intended aggregation).
    const blockKey = (target, blockId) => spriteLabel(target) + '\u0001' + blockId;
    const callIdFor = (prof, target, blockId, block) => {
      const key = blockKey(target, blockId);
      let id = prof.byCallBlock[key];
      if (id === undefined) {
        const code = (block.mutation && block.mutation.proccode) || 'call';
        id = recIdFor(prof, spriteLabel(target), code);
        prof.byCallBlock[key] = id;
      }
      return id;
    };
    const topIdFor = (prof, thread) => {
      const blockId = thread.topBlock;
      if (!blockId) return -1;
      const key = blockKey(thread.target, blockId);
      let id = prof.byTopBlock[key];
      if (id === undefined) {
        let opcode = 'top-level';
        try {
          const block = thread.target.blocks.getBlock(blockId);
          if (block) opcode = block.opcode;
        } catch (error) {}
        id = recIdFor(prof, spriteLabel(thread.target), opcode);
        prof.byTopBlock[key] = id;
      }
      return id;
    };
    const warpOf = thread => {
      try {
        const frame = thread.peekStackFrame ? thread.peekStackFrame() : null;
        return !!(frame && frame.warpMode);
      } catch (error) { return false; }
    };
    const edgeFor = (prof, callerId, calleeId) => {
      const key = callerId * 65536 + calleeId;
      let edge = prof.edgeMap.get(key);
      if (!edge) {
        edge = { caller: callerId, callee: calleeId, calls: 0, inclusive: 0 };
        prof.edgeMap.set(key, edge);
        prof.edgeList.push(edge);
      }
      return edge;
    };
    // Per-thread cache of the active path, rebuilt only at call boundaries (depth,
    // target, or "entering a call" state changed), pre-resolving ids, inclusive
    // identities and edge records so each operation is pure arithmetic.
    const pathFor = (prof, thread, opName) => {
      const stack = thread.stack;
      const len = stack.length;
      const entering = opName === 'procedures_call';
      let cache = thread.__gsProf;
      // Ids are interned per report, so a cache is only reusable inside the report
      // that built it: profileReset() installs a new report whose recs start empty,
      // and reusing stale ids would read the wrong record (or throw).
      if (cache && cache.prof === prof && cache.len === len
          && cache.target === thread.target && cache.entering === entering) {
        return cache;
      }
      const blocks = thread.target.blocks;
      const ids = [];
      const limit = entering ? len - 1 : len;
      for (let i = 0; i < limit; i++) {
        const blockId = stack[i];
        if (!blockId) continue;
        const block = blocks.getBlock(blockId);
        if (block && block.opcode === 'procedures_call') {
          ids.push(callIdFor(prof, thread.target, blockId, block));
        }
      }
      const topId = topIdFor(prof, thread);
      if (topId >= 0) ids.unshift(topId);
      let calleeId = -1;
      if (entering && len) {
        const blockId = stack[len - 1];
        const block = blockId ? blocks.getBlock(blockId) : null;
        if (block && block.opcode === 'procedures_call') {
          calleeId = callIdFor(prof, thread.target, blockId, block);
        }
      }
      const incIds = [];
      const edgeRecs = [];
      for (let i = 0; i < ids.length; i++) {
        if (incIds.indexOf(ids[i]) < 0) incIds.push(ids[i]);
        if (i + 1 < ids.length) edgeRecs.push(edgeFor(prof, ids[i], ids[i + 1]));
      }
      cache = thread.__gsProf = {
        prof, len, target: thread.target, entering, ids, incIds, edgeRecs,
        calleeId,
        calleeEdge: (calleeId >= 0 && ids.length)
          ? edgeFor(prof, ids[ids.length - 1], calleeId) : null,
        selfId: ids.length ? ids[ids.length - 1] : -1,
        warp: warpOf(thread)
      };
      return cache;
    };
    const initProf = () => {
      st.prof = {
        enabled: true, steps: 0, stepOps: 0, totalOps: 0, unattributed: 0,
        startedAt: performance.now(),
        recs: [], byKey: Object.create(null), byCallBlock: Object.create(null),
        byTopBlock: Object.create(null), edgeMap: new Map(), edgeList: [],
        stepUiOps: 0, stepWarpOps: 0, stepTick: 0, dirty: [],
        sampleOps: 0, sampleEvery: 256, lastSampleAt: 0, lastSampleSelfId: -1,
        lastSampleWarp: false, timeMsTotal: 0, timeSamples: 0,
        uiTimeMs: 0, warpTimeMs: 0, unattributedTimeMs: 0,
        frames: [], framesTruncated: false,
        stepMsSum: 0, stepMsCount: 0, drawSum: 0, drawFrames: 0,
        lastStepMs: 0, drawStep: 0, series: [], perStepCapped: false,
        cap: STEP_CAP, budgetSteps: 0
      };
    };
    const attributeOp = (opIndex, opName, util) => {
      const prof = st.prof;
      if (!prof || !prof.enabled) return;
      prof.stepOps += 1;
      prof.totalOps += 1;
      const thread = util && util.thread;
      if (!thread || !thread.target || !thread.target.blocks || !thread.stack) {
        prof.unattributed += 1;
        return;
      }
      // For procedures_call the call block is the current top frame and the callee
      // has not been entered yet: the call itself belongs to the caller.
      const cache = pathFor(prof, thread, opName);
      if (cache.calleeId >= 0) {
        prof.recs[cache.calleeId].calls += 1;
        if (cache.calleeEdge) cache.calleeEdge.calls += 1;
      }
      const selfId = cache.selfId;
      if (selfId < 0) {
        prof.unattributed += 1;
      } else {
        const rec = prof.recs[selfId];
        rec.self += 1;
        if (opIndex >= 0 && opIndex < rec.opCounts.length) rec.opCounts[opIndex] += 1;
        if (cache.warp) { rec.warpOps += 1; prof.stepWarpOps += 1; } else {
          rec.uiOps += 1; prof.stepUiOps += 1;
        }
        rec.stepSelf += 1;
        if (rec.stepDirty !== prof.stepTick) {
          rec.stepDirty = prof.stepTick;
          prof.dirty.push(selfId);
        }
      }
      // Inclusive: each distinct identity on the path once per operation (recursion
      // collapses, so this stays a flat view rather than a call tree).
      for (let i = 0; i < cache.incIds.length; i++) {
        prof.recs[cache.incIds[i]].inclusive += 1;
      }
      for (let i = 0; i < cache.edgeRecs.length; i++) {
        cache.edgeRecs[i].inclusive += 1;
      }
      // Sampled time: one clock read per adaptive window, credited to the identity
      // that ran during the previous window (never crossing a step boundary).
      prof.sampleOps += 1;
      if (prof.sampleOps >= prof.sampleEvery) {
        const now = performance.now();
        if (prof.lastSampleAt) {
          const dt = now - prof.lastSampleAt;
          if (dt > 0 && dt <= 30) {
            prof.timeSamples += 1;
            prof.timeMsTotal += dt;
            if (prof.lastSampleWarp) prof.warpTimeMs += dt; else prof.uiTimeMs += dt;
            const last = prof.lastSampleSelfId;
            if (last >= 0) {
              const rec = prof.recs[last];
              rec.timeMs += dt;
              if (prof.lastSampleWarp) rec.warpTimeMs += dt; else rec.uiTimeMs += dt;
            } else {
              prof.unattributedTimeMs += dt;
            }
            if (dt < 1.0 && prof.sampleEvery < 4096) prof.sampleEvery *= 2;
            else if (dt > 4.0 && prof.sampleEvery > 32) {
              prof.sampleEvery = Math.max(32, Math.floor(prof.sampleEvery / 2));
            }
          }
        }
        prof.lastSampleAt = now;
        prof.lastSampleSelfId = selfId;
        prof.lastSampleWarp = cache.warp;
        prof.sampleOps = 0;
      }
    };
    const reportData = prof => {
      const names = st.opNames;
      const procedures = prof.recs.map(r => {
        const dominant = [];
        for (let i = 0; i < r.opCounts.length; i++) {
          if (r.opCounts[i]) dominant.push([names[i] || String(i), r.opCounts[i]]);
        }
        dominant.sort((a, b) => b[1] - a[1]);
        return {
          id: r.id, key: r.key, sprite: r.sprite, name: r.name, calls: r.calls,
          self: r.self, inclusive: r.inclusive,
          share: prof.totalOps ? r.self / prof.totalOps : 0,
          dominant: dominant.slice(0, 10),
          ui_ops: r.uiOps, warp_ops: r.warpOps,
          context: (r.warpOps && r.uiOps) ? 'mixed' : (r.warpOps ? 'all-at-once' : 'ui-thread'),
          held_steps: r.held,
          budget_pct: prof.steps ? 100 * r.held / prof.steps : 0,
          time_ms: r.timeMs,
          time_share: prof.timeMsTotal ? r.timeMs / prof.timeMsTotal : 0,
          ui_time_ms: r.uiTimeMs, warp_time_ms: r.warpTimeMs
        };
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
      const ids = {};
      for (const r of prof.recs) ids[r.id] = r.key;
      const edges = prof.edgeList.map(e => ({
        caller: (prof.recs[e.caller] || {}).key || String(e.caller),
        callee: (prof.recs[e.callee] || {}).key || String(e.callee),
        calls: e.calls, inclusive: e.inclusive
      })).sort((a, b) => b.inclusive - a.inclusive).slice(0, 500);
      return {
        schema: 1,
        instrumented: true,
        units: {
          blocks: 'executed primitive dispatches per VM step',
          step_ms: 'instrumented sequencer.stepThreads ms (inflated; compare shares)'
        },
        aggregated: 'clones by original sprite',
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
        work_time_ms: 0.75 * (runtime.currentStepTime || 33.333),
        budget_steps: prof.budgetSteps || 0,
        time_ms_total: prof.timeMsTotal, time_samples: prof.timeSamples,
        ui_time_ms: prof.uiTimeMs, warp_time_ms: prof.warpTimeMs,
        unattributed_time_ms: prof.unattributedTimeMs, sample_every: prof.sampleEvery,
        ids,
        frames: prof.frames.slice(),
        frames_truncated: prof.framesTruncated,
        edges,
        procedures
      };
    };
    const clearExecuteCache = () => {
      for (const target of runtime.targets) {
        if (target.blocks && target.blocks._cache) target.blocks._cache._executeCached = {};
      }
    };
    const installPrimitiveHooks = () => {
      const opNames = Object.keys(runtime._primitives);
      st.opNames = opNames;
      opNames.forEach((op, i) => {
        const original = runtime._primitives[op];
        if (typeof original !== 'function' || original.__gsdevProfile) return;
        const wrapped = function (args, util) {
          attributeOp(i, op, util);
          return original.call(this, args, util);
        };
        wrapped.__gsdevProfile = true;
        primitiveWraps.push({ obj: runtime._primitives, name: op, original });
        runtime._primitives[op] = wrapped;
      });
      clearExecuteCache();
    };
    const removePrimitiveHooks = () => {
      for (const item of primitiveWraps.splice(0)) {
        if (item.obj[item.name] && item.obj[item.name].__gsdevProfile) {
          item.obj[item.name] = item.original;
        }
      }
      st.opNames = [];
      clearExecuteCache();
    };
    // Per-draw pen counting: PenSkin.drawLine (covers drawPoint, which delegates).
    // Counts only (no clock reads), credited to the current window's drawStep.
    const wrapPenSkin = skin => {
      if (!skin || typeof skin.drawLine !== 'function' || skin.drawLine.__gsdevDraw) return;
      const o = skin.drawLine;
      const w = function (pa, x0, y0, x1, y1) {
        const prof = st.prof;
        if (!prof || !prof.enabled) return o.call(this, pa, x0, y0, x1, y1);
        const result = o.call(this, pa, x0, y0, x1, y1);
        prof.drawStep += 1;
        return result;
      };
      w.__gsdevDraw = true;
      drawWraps.push({ obj: skin, name: 'drawLine', original: o });
      skin.drawLine = w;
    };
    const installDrawHooks = () => {
      if (typeof renderer.createPenSkin === 'function' && !renderer.createPenSkin.__gsdevDraw) {
        const o = renderer.createPenSkin;
        const w = function () {
          const id = o.apply(this, arguments);
          wrapPenSkin(this._allSkins && this._allSkins[id]);
          return id;
        };
        w.__gsdevDraw = true;
        drawWraps.push({ obj: renderer, name: 'createPenSkin', original: o });
        renderer.createPenSkin = w;
      }
      for (const skin of (renderer._allSkins || [])) wrapPenSkin(skin);
    };
    const removeDrawHooks = () => {
      for (const item of drawWraps.splice(0)) {
        if (item.obj[item.name] && item.obj[item.name].__gsdevDraw) {
          item.obj[item.name] = item.original;
        }
      }
    };
    // Close the previous step's sample. Called at the start of `runtime._step`, so
    // it sees the step that just finished (frame counter already advanced).
    const closeStep = () => {
      const prof = st.prof;
      if (!prof || !prof.enabled) return;
      prof.steps += 1;
      prof.series.push({ frame: st.frame - 1, t: performance.now(), step_ms: prof.lastStepMs,
                         draws: prof.drawStep, blocks: prof.stepOps });
      if (prof.series.length > prof.cap) {
        prof.series.splice(0, prof.series.length - prof.cap);
        prof.perStepCapped = true;
      }
      const ops = [];
      for (const id of prof.dirty) {
        const rec = prof.recs[id];
        ops.push([id, rec.stepSelf]);
        rec.stepSelf = 0;
      }
      prof.dirty.length = 0;
      prof.stepTick += 1;
      prof.frames.push({ frame: st.frame - 1, step_ms: prof.lastStepMs,
                         draws: prof.drawStep, blocks: prof.stepOps,
                         ui: prof.stepUiOps, warp: prof.stepWarpOps, ops });
      if (prof.frames.length > FRAME_CAP) {
        prof.frames.shift();
        prof.framesTruncated = true;
      }
      // A step that consumed its work budget *and* was mostly screen-refresh
      // (non-warp) is the signature of a big loop sitting in a hat.
      const workTime = 0.75 * (runtime.currentStepTime || 33.333);
      const uiShare = prof.stepOps ? prof.stepUiOps / prof.stepOps : 0;
      if (prof.steps > 1 && prof.lastStepMs >= workTime * 0.9 && uiShare >= 0.5) {
        prof.budgetSteps = (prof.budgetSteps || 0) + 1;
        try {
          for (const thread of (runtime.threads || [])) {
            if (!thread || !thread.target || !thread.stack || !thread.stack.length) continue;
            const cache = pathFor(prof, thread, '');
            if (cache.warp) continue;
            if (cache.selfId >= 0) prof.recs[cache.selfId].held += 1;
          }
        } catch (error) {}
      }
      prof.stepOps = 0;
      prof.drawStep = 0;
      prof.stepUiOps = 0;
      prof.stepWarpOps = 0;
      prof.lastSampleAt = 0;
    };
    const installRuntimeHooks = () => {
      const seq = runtime.sequencer;
      if (seq && typeof seq.stepThreads === 'function' && !seq.stepThreads.__gsdevProfStep) {
        const o = seq.stepThreads;
        const w = function (...a) {
          const t0 = performance.now();
          const r = o.apply(this, a);
          const ms = performance.now() - t0;
          const prof = st.prof;
          if (prof && prof.enabled) {
            prof.stepMsSum += ms; prof.stepMsCount += 1; prof.lastStepMs = ms;
          }
          return r;
        };
        w.__gsdevProfStep = true;
        runtimeWraps.push({ obj: seq, name: 'stepThreads', original: o });
        seq.stepThreads = w;
      }
      if (typeof runtime._step === 'function' && !runtime._step.__gsdevProfStep) {
        const o = runtime._step;
        const w = function (...a) {
          st.frame += 1;
          closeStep();
          return o.apply(this, a);
        };
        w.__gsdevProfStep = true;
        runtimeWraps.push({ obj: runtime, name: '_step', original: o });
        runtime._step = w;
      }
      if (renderer && typeof renderer.draw === 'function' && !renderer.draw.__gsdevProfDraw) {
        const o = renderer.draw;
        const w = function (...a) {
          const r = o.apply(this, a);
          const prof = st.prof;
          if (prof && prof.enabled) { prof.drawSum += prof.drawStep; prof.drawFrames += 1; }
          return r;
        };
        w.__gsdevProfDraw = true;
        runtimeWraps.push({ obj: renderer, name: 'draw', original: o });
        renderer.draw = w;
      }
    };
    const removeRuntimeHooks = () => {
      for (const item of runtimeWraps.splice(0)) {
        if (item.obj[item.name] && (item.obj[item.name].__gsdevProfStep
            || item.obj[item.name].__gsdevProfDraw)) {
          item.obj[item.name] = item.original;
        }
      }
    };

    const setEnabled = on => {
      st.profileEnabled = !!on;
      removeRuntimeHooks();
      removePrimitiveHooks();
      removeDrawHooks();
      if (st.profileEnabled) {
        installRuntimeHooks();
        installPrimitiveHooks();
        installDrawHooks();
        initProf();
      } else {
        st.prof = null;
      }
      return st.profileEnabled;
    };
    const reset = () => {
      if (!st.profileEnabled) return false;
      // The runtime hooks and primitive wrappers stay; only the window is replaced.
      removeRuntimeHooks();
      removePrimitiveHooks();
      installRuntimeHooks();
      installPrimitiveHooks();
      initProf();
      return true;
    };
    const refresh = () => {
      if (!st.profileEnabled) return false;
      reset();
      return true;
    };
    const report = () => (st.prof ? reportData(st.prof) : DISABLED_REPORT());
    const steps = since => {
      const prof = st.prof;
      if (!prof || !prof.series) return { series_start: 0, steps: [] };
      const seriesStart = prof.steps - prof.series.length;
      const offset = Math.max(0, (Number(since) || 0) - seriesStart);
      return { series_start: seriesStart + offset, steps: prof.series.slice(offset) };
    };
    const value = name => {
      const r = report();
      switch (String(name || '').toLowerCase()) {
      case 'steps': return r.steps;
      case 'totalops': case 'blocks': return r.total_ops;
      case 'unattributed': case 'unknown': return r.unattributed;
      case 'selftotal': return r.self_total;
      case 'opsperstep': return r.ops_per_step_mean;
      default: return undefined;
      }
    };

    return {
      setEnabled, enabled: () => st.profileEnabled, reset, refresh,
      report, steps, value
    };
  };
})();
