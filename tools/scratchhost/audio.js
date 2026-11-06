/* Audio engine for the scratch-vm host.
   scratch-vm plays no sound until an AudioEngine is attached — the Scratch GUI
   does this, a bare host does not, so `music`'s extension failed to load with
   "No Audio Context Detected" and every sound block was silent. Attach
   scratch-audio's engine here, before any project loads, and count playback so
   `@audioplayback` can prove sound actually started.

   scratch-audio ships a CommonJS webpack bundle (dist.js) that requires
   `audio-context`, `minilog` and `startaudiocontext`; the host page mounts those
   packages at /vendor/ (or node_modules) as plain files, so this loader fetches
   the sources and evaluates them with a small CJS shim — the same approach the
   scratch GUI's webpack build performs at build time. */
(() => {
  const state = {
    ready: false,
    error: null,
    Engine: null,
    engine: null,
    context: null,
    started: 0,
    lastStartAt: 0,
    lastDuration: null,
  };

  const cjs = (source, resolve) => {
    const module = { exports: {} };
    // eslint-disable-next-line no-new-func
    new Function("module", "exports", "require", source)(module, module.exports, resolve);
    return module.exports;
  };

  const fetchText = async url => {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
    return response.text();
  };

  window.__gsdevAudio = {
    state,
    // Load and evaluate the audio bundles. Called by host.html once the asset base
    // (vendor or node_modules) is known, before host.js creates the VM.
    prepare: async base => {
      try {
        if (!(window.AudioContext || window.webkitAudioContext)) {
          throw new Error("this browser has no WebAudio AudioContext");
        }
        const [audio, minilog, start] = await Promise.all([
          fetchText(`${base}/scratch-audio/dist.js`),
          fetchText(`${base}/minilog/dist/minilog.js`),
          fetchText(`${base}/startaudiocontext/StartAudioContext.js`),
        ]);
        const Minilog = new Function(`${minilog}\nreturn Minilog;`)();
        const deps = {
          "audio-context": window.AudioContext || window.webkitAudioContext,
          minilog: Minilog,
          startaudiocontext: cjs(start, () => {
            throw new Error("startaudiocontext requires a module");
          }),
        };
        state.Engine = cjs(audio, name => {
          if (!(name in deps)) throw new Error(`scratch-audio requires unknown module ${name}`);
          return deps[name];
        });
        state.ready = true;
      } catch (error) {
        state.error = String((error && error.message) || error);
      }
    },
    // Attach a fresh engine to the VM. Returns the engine, or null when audio is
    // unavailable (the host then runs visuals only, as it did before).
    attach: vm => {
      if (!state.ready || state.engine) return state.engine;
      try {
        const engine = new state.Engine();
        const context = engine.audioContext;
        // Count playback starts: every Scratch sound becomes a buffer source.
        const createSource = context.createBufferSource.bind(context);
        context.createBufferSource = () => {
          const node = createSource();
          const start = node.start.bind(node);
          node.start = (...args) => {
            state.started += 1;
            state.lastStartAt = performance.now();
            state.lastDuration = node.buffer ? node.buffer.duration : null;
            return start(...args);
          };
          return node;
        };
        vm.attachAudioEngine(engine);
        state.engine = engine;
        state.context = context;
        // Autoplay policy may leave the context suspended until a gesture; gsdev
        // launches with --autoplay-policy=no-user-gesture-required, so resume now.
        if (context.resume) context.resume().catch(() => {});
        return engine;
      } catch (error) {
        state.error = String((error && error.message) || error);
        return null;
      }
    },
    status: () => ({
      ready: state.ready,
      attached: !!state.engine,
      context: state.context ? state.context.state : "none",
      sampleRate: state.context ? state.context.sampleRate : 0,
      started: state.started,
      lastDuration: state.lastDuration,
      error: state.error,
    }),
    // `@audioplayback` is "since this project loaded"; a warm host reloads a new
    // project into the same page, so reset the counter when that happens.
    reset: () => {
      state.started = 0;
      state.lastStartAt = 0;
      state.lastDuration = null;
    },
  };
})();
