# scratchhost

Static host page (and pinned `@scratch/*` deps) that runs a goboscript `.sb3`
against **upstream Scratch's VM** (`@scratch/scratch-vm`) in a plain browser — no
Electron, no scratch-gui. `tools/gsdev.py` drives it: it serves this directory,
launches Chrome/Edge, loads the project, green-flags it, streams goboscript logs,
and exposes live `get`/`set`/`watch` plus input/pixel/screenshot over one CDP
connection. This package is not a CLI of its own.

## Install (one time)

The `@scratch/*` packages are **AGPL-3.0-only**, so they are installed locally and
gitignored; they are never vendored into the repository.

```powershell
npm install --prefix tools/scratchhost
```

## Use

Drive the host from the project root (there is no separate `scratchvm.py`):

```powershell
python tools/gsdev.py run --headless --duration 3
python tools/gsdev.py get main.seconds
python tools/gsdev.py set main.tx 120        # live edit, no rebuild
python tools/gsdev.py pixel 0 0
python tools/gsdev.py screenshot --out debug/stage.png
python tools/gsdev.py close
```

Requirements: Node/npm (for the install) and Chrome/Edge/Chromium
(`GSDEV_BROWSER` overrides the executable).

## Fidelity notes

- Semantics are upstream Scratch's (interpreter, no compiler, no TurboWarp
  options) — that is the point.
- **Rendering uses the real GPU by default**, including `--headless`, so
  performance is representative. `--software` (or a GPU-less box such as CI)
  forces ANGLE/SwiftShader, where rendering is much slower (~22–26 fps,
  rendertime ~44–90 ms in the original measurements) — trust behavior there, not
  performance.
- The audio engine is not attached; projects that use sound may need
  `scratch-audio`.
