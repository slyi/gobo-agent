"""Page/input primitives: JS for mouse, keys, pixels, frames, pause/step, broadcast.

`ns` selects the page namespace (`window.__host` locally, `window.__bridge` in
gsbridge), so both front ends share one implementation.
"""

from __future__ import annotations

import json

from cdp import CDP, CDPError


INPUT_KEY_ALIASES = {
    "space": " ",
    "enter": "Enter",
    "return": "Enter",
    "up": "ArrowUp",
    "up arrow": "ArrowUp",
    "down": "ArrowDown",
    "down arrow": "ArrowDown",
    "left": "ArrowLeft",
    "left arrow": "ArrowLeft",
    "right": "ArrowRight",
    "right arrow": "ArrowRight",
}


def input_key_name(name: str) -> str:
    return INPUT_KEY_ALIASES.get(name.strip().lower(), name)


def mouse_js(x: float, y: float, is_down: bool | None, ns: str = "window.__host") -> str:
    """Mouse move/press JS. `ns` needs `vm` and `stageRect()` (see host.js), so the
    coordinate math is shared with gsbridge's injected shim instead of duplicated."""
    down = "undefined" if is_down is None else ("true" if is_down else "false")
    return (
        "(() => {"
        f" const _h = {ns};"
        " const _r = _h.stageRect && _h.stageRect();"
        " if (!_r) return 'no-stage';"
        " const sw = _h.vm.runtime.constructor.STAGE_WIDTH || 480;"
        " const sh = _h.vm.runtime.constructor.STAGE_HEIGHT || 360;"
        f" const px = ({float(x)!r} / sw + 0.5) * _r.width;"
        f" const py = (0.5 - {float(y)!r} / sh) * _r.height;"
        " _h.vm.postIOData('mouse', {x: px, y: py, canvasWidth: _r.width,"
        f" canvasHeight: _r.height, isDown: {down}}});"
        " return 'ok'; })()"
    )


def key_js(key: str, is_down: bool, ns: str = "window.__host") -> str:
    pressed = "true" if is_down else "false"
    return (
        f"(() => {{ {ns}.vm.postIOData('keyboard', {{key: "
        + json.dumps(key) + ", isDown: " + pressed + "}); return 'ok'; })()"
    )


def hold_input(cdp: CDP, settle_ms: float, ns: str = "window.__host") -> None:
    """Keep mouse/key down and up apart without the CLI sleeping.

    Default is exactly one VM frame; an explicit --settle becomes a page-side timer
    (still no CLI sleep, and the duration is honoured exactly). `ns` selects the host
    page's namespace so gsbridge (window.__bridge) shares this implementation.
    """
    try:
        if settle_ms > 0:
            cdp.evaluate(
                f"new Promise(r => setTimeout(r, {int(settle_ms)}))",
                await_promise=True, timeout=settle_ms / 1000.0 + 3,
            )
        else:
            cdp.evaluate(f"{ns}.waitFrames(1, 2000)", await_promise=True, timeout=3)
    except (CDPError, TimeoutError, OSError):
        pass


def pixel_js(x: float, y: float) -> str:
    return (
        "(async () => {"
        " const vm = window.__host.vm;"
        " const renderer = vm.runtime.renderer;"
        " if (!renderer || typeof renderer.requestSnapshot !== 'function') {"
        "   return JSON.stringify({error: 'requestSnapshot unavailable'});"
        " }"
        " const url = await new Promise((resolve) => {"
        "   try { renderer.requestSnapshot((u) => resolve(u || null)); } catch (e) { resolve(null); }"
        " });"
        " if (!url) return JSON.stringify({error: 'snapshot failed'});"
        " const img = new Image();"
        " await new Promise((resolve, reject) => { img.onload = resolve; img.onerror = reject; img.src = url; });"
        " const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;"
        " const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0, img.width, img.height);"
        " const sw = vm.runtime.constructor.STAGE_WIDTH || 480;"
        " const sh = vm.runtime.constructor.STAGE_HEIGHT || 360;"
        f" const ix = Math.round(({float(x)!r} / sw + 0.5) * img.width);"
        f" const iy = Math.round((0.5 - {float(y)!r} / sh) * img.height);"
        " const px = ctx.getImageData(Math.min(ix, img.width - 1), Math.min(iy, img.height - 1), 1, 1).data;"
        " const hex = '#' + [px[0], px[1], px[2]].map(v => v.toString(16).padStart(2, '0')).join('');"
        f" return JSON.stringify({{rgba: [px[0], px[1], px[2], px[3]], hex: hex, x: {float(x)!r}, y: {float(y)!r}}});"
        " })()"
    )


def events_js(ns: str = "window.__host") -> str:
    return f"{ns}.events()"


def pause_js(ns: str = "window.__host") -> str:
    return f"{ns}.pause()"


def resume_js(ns: str = "window.__host") -> str:
    return f"{ns}.resume()"


def step_js(count: int, ns: str = "window.__host") -> str:
    return f"{ns}.step({int(count)})"


def broadcast_js(name: str, ns: str = "window.__host") -> str:
    return f"{ns}.broadcast({json.dumps(name)})"


def current_frame(cdp: CDP, js: str = "window.__gsdev.frame") -> int:
    # The counter is a property on the local host and a method on the bridge shim.
    return int(cdp.evaluate(js))

def broadcast_wait_js(name: str, timeout_ms: int, ns: str = "window.__host") -> str:
    return f"{ns}.broadcastWait({json.dumps(name)}, {int(timeout_ms)})"
