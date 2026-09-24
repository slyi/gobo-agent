/* Minimal headless host for upstream @scratch/scratch-vm.
   Phase 0 spike: prove the VM + renderer + storage can run a local .sb3 with
   embedded assets, expose the VM for get/set, and capture goboscript logs. */
(() => {
  const marks = (window.__marks = { start: performance.now() });
  try {
    const vm = new window.VirtualMachine();

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
            const state = window.__gsdev;
            if (state) {
              state.errors = [];
              state.logErrors = 0;
              state.stopped = false;
              state.logs = [];
              state.seen = [];
            }
          });
        }
        return promise;
      },
      start: () => {
        vm.start();
        vm.greenFlag();
      },
      stop: () => vm.stopAll(),
      state: () => ({
        targets: vm.runtime.targets.map(t => t.getName()),
        threads: vm.runtime.threads.length
      })
    };

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

    // Runtime error capture. scratch-vm fires no error event and has no
    // try/catch in _step or stepThreads, so a throwing thread escapes to the
    // setInterval callback. Wrapping _step captures it and lets the runtime keep
    // going; page-level errors (window.onerror / unhandledrejection) land in the
    // same list. Bounded so a thread that throws every frame cannot grow forever.
    const MAX_ERRORS = 200;
    const recordError = entry => {
      if (gs.errors.length < MAX_ERRORS) gs.errors.push(entry);
    };
    const originalStep = runtime._step;
    runtime._step = function (...args) {
      gs.frame += 1;
      gs.frameEvents += 1;
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
      const result = originalDraw.apply(this, args);
      gs.rendered = gs.frame;
      gs.renderEvents += 1;
      try {
        window.dispatchEvent(new CustomEvent('gsdev:render', {
          detail: { frame: gs.frame, rendered: gs.rendered }
        }));
      } catch (error) {}
      return result;
    };

    window.__host.frame = () => gs.frame;
    window.__host.rendered = () => gs.rendered;
    window.__host.events = () => ({
      frame: gs.frame, rendered: gs.rendered,
      frameEvents: gs.frameEvents, renderEvents: gs.renderEvents
    });
    window.__host.pause = () => {
      clearInterval(runtime._steppingInterval);
      runtime._steppingInterval = null;
    };
    window.__host.resume = () => runtime.start();
    window.__host.step = count => {
      for (let i = 0; i < count; i++) runtime._step();
      return gs.frame;
    };
    window.__host.restart = () => {
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
      const target = window.__host.findTarget(spec.target);
      if (!target) return { error: 'target not found: ' + spec.target };
      let variable = null;
      for (const key in target.variables) {
        if (target.variables[key].name === spec.name) { variable = target.variables[key]; break; }
      }
      if (!variable) return { error: 'variable not found: ' + spec.name };
      return { variable };
    };
    const readVar = (variable, index) => (index === null || index === undefined)
      ? variable.value
      : (Array.isArray(variable.value) ? variable.value[index - 1] : undefined);
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
      });
    } catch (error) {
      g.error = String(error);
    }
  } catch (error) {
    marks.fatal = String((error && error.stack) || error);
  }
  marks.ready = performance.now();
})();
