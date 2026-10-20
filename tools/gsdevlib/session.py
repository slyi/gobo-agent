"""Session language interpreter: one CDP connection, one line per verb.

The interpreter is deliberately data-driven: every project/page primitive it needs
is supplied by `SessionAPI` (built in gsdev.py), so this module has no gsdev or
config dependency.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from cdp import CDPError


@dataclass
class SessionAPI:
    log: Callable[[str], None]
    parse_selector: Callable[[str], tuple]
    parse_value: Callable[[str], Any]
    var_js: Callable[..., str]
    apply_batch: Callable[..., None]
    watch_run: Callable[..., dict]
    mouse_js: Callable[..., str]
    key_js: Callable[..., str]
    input_key_name: Callable[[str], str]
    select_js: Callable[[str], str]
    compare: Callable[..., bool]
    capture_js: Callable[..., str]
    assert_capture_js: Callable[..., str]
    spec: Callable[[str], dict]
    pixel_js: Callable[..., str]
    current_frame: Callable[..., int]
    pause_js: Callable[[], str]
    resume_js: Callable[[], str]
    events_js: Callable[[], str]
    inspect_js: Callable[[str], str]
    broadcast_wait_js: Callable[..., str]
    get_errors: Callable[..., dict]
    errors_ok: Callable[[dict], bool]
    stop_watch_js: Callable[..., str]
    read_timeout: float
    schema: int


def run_session_lines(cdp, lines, quiet: bool = False,
                      event_sink=None, *, api) -> dict:
    """Run session lines over one open CDP connection.

    Prints the same ``[ASSERT ...]`` / ``[gsdev] ...`` output as the ``session``
    command and returns ``{"asserts": [{"ok", "msg"}], "failures": N}`` where
    ``failures`` counts every failed assertion and every timeout/error verb.

    ``event_sink`` (a callable) receives one versioned structured record per
    assertion plus a final ``session_end``, written as the run proceeds, so a parent
    can assemble a failure bundle without scraping stdout or re-reading the page.

    A ``capture SELECTOR...`` line declares extra state to read together with the
    following ``expect``, in the same synchronous in-page evaluation, so a bundle
    shows assertion-time state rather than a later (drifting) re-read.
    """
    failures = 0
    asserts = []
    watches = []
    captures = []
    started = time.time()
    current_line = 0

    def emit(rec: dict) -> None:
        if event_sink is None:
            return
        rec.setdefault("schema", api.schema)
        try:
            event_sink(rec)
        except (OSError, TypeError, ValueError):
            pass

    def record(ok: bool, message: str, **extra) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        if not quiet:
            print(f"[ASSERT {'ok' if ok else 'FAIL'}] {message}", flush=True)
        asserts.append({"ok": ok, "msg": message})
        rec = {"kind": "assert", "ok": ok, "msg": message, "ts": time.time(),
               "seq": len(asserts)}
        rec.update(extra)
        rec.setdefault("line", current_line)
        emit(rec)

    try:
        for lineno, raw in enumerate(lines, 1):
            current_line = lineno
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            op = parts[0].lower()
            if op == "capture" and len(parts) >= 2:
                for selector in parts[1:]:
                    if selector not in captures:
                        captures.append(selector)
                api.log(json.dumps({"capture": captures}))
            elif op == "set" and len(parts) >= 3:
                target, name, index = api.parse_selector(parts[1])
                value = api.parse_value(" ".join(parts[2:]))
                result = cdp.evaluate(api.var_js(target, name, index, value), timeout=api.read_timeout)
                api.log(json.dumps({"set": parts[1], **result}))
            elif op == "set_batch" and len(parts) >= 3:
                try:
                    api.apply_batch(cdp, parts[1:])
                except SystemExit as error:
                    api.log(f"session: {error}")
            elif op == "get" and len(parts) >= 2:
                result = {}
                for selector in parts[1:]:
                    target, name, index = api.parse_selector(selector)
                    result[selector] = cdp.evaluate(api.var_js(target, name, index))
                api.log(json.dumps(result))
            elif op == "sleep" and len(parts) >= 2:
                time.sleep(float(parts[1]) / 1000.0)
            elif op == "watch" and len(parts) >= 3:
                watches.append(api.watch_run(cdp, parts[2:], float(parts[1])))
            elif op == "mouse" and len(parts) >= 3:
                x, y = float(parts[1]), float(parts[2])
                mode = parts[3].lower() if len(parts) > 3 else "move"
                is_down = {"down": True, "up": False}.get(mode)
                cdp.evaluate(api.mouse_js(x, y, is_down))
                api.log(json.dumps({"mouse": [x, y], "down": is_down}))
            elif op == "click" and len(parts) >= 3:
                x, y = float(parts[1]), float(parts[2])
                cdp.evaluate(api.mouse_js(x, y, None))
                cdp.evaluate(api.mouse_js(x, y, True))
                cdp.evaluate("window.__host.waitFrames(1, 1000)", await_promise=True, timeout=2) or {}
                cdp.evaluate(api.mouse_js(x, y, False))
                api.log(json.dumps({"click": [x, y]}))
            elif op == "key" and len(parts) >= 2:
                key = api.input_key_name(parts[1])
                mode = parts[2].lower() if len(parts) > 2 else "press"
                if mode == "down":
                    cdp.evaluate(api.key_js(key, True))
                elif mode == "up":
                    cdp.evaluate(api.key_js(key, False))
                else:
                    cdp.evaluate(api.key_js(key, True))
                    cdp.evaluate("window.__host.waitFrames(1, 1000)", await_promise=True, timeout=2) or {}
                    cdp.evaluate(api.key_js(key, False))
                api.log(json.dumps({"key": key, "mode": mode}))
            elif op == "expect" and len(parts) >= 4:
                selector, operator = parts[1], parts[2]
                expected = api.parse_value(" ".join(parts[3:]))
                extra_selectors = [s for s in captures if s != selector]
                if extra_selectors:
                    # One synchronous evaluation: assertion + declared state together.
                    res = cdp.evaluate(
                        api.assert_capture_js(selector, extra_selectors), timeout=api.read_timeout) or {}
                    actual = (res.get("actual") or {}).get("value")
                    capture = {"source": res.get("actual")}
                    capture.update(res.get("capture") or {})
                else:
                    actual = cdp.evaluate(api.select_js(selector), timeout=api.read_timeout).get("value")
                    capture = None
                record(
                    api.compare(actual, operator, expected),
                    f"{selector} {operator} {expected!r} (actual {actual!r})",
                    selector=selector, operator=operator, expected=expected,
                    actual=actual, capture=capture,
                )
            elif op == "pixel" and len(parts) >= 3:
                result = json.loads(
                    cdp.evaluate(api.pixel_js(float(parts[1]), float(parts[2])), await_promise=True, timeout=30)
                )
                api.log(json.dumps(result))
            elif op == "expectpixel" and len(parts) >= 4:
                expected = parts[3].lower()
                actual = json.loads(
                    cdp.evaluate(api.pixel_js(float(parts[1]), float(parts[2])), await_promise=True, timeout=30)
                ).get("hex", "")
                ok = actual.lower() == expected
                capture = None
                atomic = None
                if not ok and captures:
                    # A pixel read is a promise, so pixel-failure captures are read
                    # separately; label that they are not simultaneous.
                    capture = cdp.evaluate(api.capture_js(captures), timeout=api.read_timeout) or {}
                    atomic = False
                record(
                    ok,
                    f"pixel {parts[1]} {parts[2]} == {expected} (actual {actual})",
                    selector=f"pixel {parts[1]} {parts[2]}", expected=expected,
                    actual=actual, capture=capture, capture_atomic=atomic,
                )
            elif op == "frame" and len(parts) == 1:
                api.log(json.dumps({"frame": api.current_frame(cdp)}))
            elif op == "pause":
                cdp.evaluate(api.pause_js())
                api.log(json.dumps({"paused": True, "frame": api.current_frame(cdp)}))
            elif op == "resume":
                cdp.evaluate(api.resume_js())
                api.log(json.dumps({"resumed": True, "frame": api.current_frame(cdp)}))
            elif op == "restart":
                cdp.evaluate("window.__host.restart()")
                api.log(json.dumps({"restarted": True, "frame": api.current_frame(cdp)}))
            elif op == "step" and len(parts) >= 2:
                cdp.evaluate(api.pause_js())
                before = api.current_frame(cdp)
                frame = int(cdp.evaluate(f"window.__host.step({int(parts[1])})"))
                api.log(json.dumps({"stepped": frame - before, "frame": frame}))
            elif op == "waitframe" and len(parts) >= 2:
                count = int(parts[1])
                timeout = float(parts[2]) if len(parts) > 2 else 5000.0
                result = cdp.evaluate(
                    f"window.__host.waitFrames({count}, {int(timeout)})",
                    await_promise=True, timeout=timeout / 1000.0 + 5,
                )
                if result.get("ok"):
                    api.log(json.dumps({"waited": count, "frame": result.get("frame")}))
                else:
                    record(False, f"waitframe {count} timed out")
            elif op == "render":
                api.log(json.dumps(cdp.evaluate(api.events_js())))
            elif op == "record" and len(parts) >= 2:
                specs = [api.spec(selector) for selector in parts[1:]]
                result = cdp.evaluate(f"window.__host.record({json.dumps(specs)}, 100000)")
                api.log(json.dumps({"record": result}))
            elif op == "trace":
                api.log(json.dumps(cdp.evaluate("window.__host.trace(false)")))
            elif op == "stoprecord":
                api.log(json.dumps(cdp.evaluate("window.__host.stopRecord()")))
            elif op == "until" and len(parts) >= 4:
                spec = api.spec(parts[1])
                value = api.parse_value(parts[3])
                pause = len(parts) > 4 and parts[4].lower() == "pause"
                maxf = int(parts[5]) if len(parts) > 5 else 100000
                script = (
                    f"window.__host.until({json.dumps(spec)}, {json.dumps(parts[2])}, "
                    f"{json.dumps(value)}, {'true' if pause else 'false'}, {maxf})"
                )
                api.log(json.dumps({"until": cdp.evaluate(script)}))
            elif op == "broadcast" and len(parts) >= 2:
                result = cdp.evaluate(
                    f"window.__host.broadcast({json.dumps(parts[1])})") or {}
                api.log(json.dumps({
                    "broadcast": parts[1], "threads": result.get("threads", 0)
                }))
            elif op == "broadcast_wait" and len(parts) >= 2:
                timeout = float(parts[2]) if len(parts) > 2 else 5000.0
                result = cdp.evaluate(
                    api.broadcast_wait_js(parts[1], int(timeout)),
                    await_promise=True, timeout=timeout / 1000.0 + 5,
                ) or {}
                if result.get("ok"):
                    api.log(json.dumps({
                        "broadcast": parts[1],
                        "threads": result.get("threads", 0),
                        "waitedFrames": result.get("waitedFrames"),
                    }))
                else:
                    record(False, f"broadcast_wait {parts[1]} timed out")
            elif op == "inspect":
                target = parts[1] if len(parts) > 1 else ""
                api.log(json.dumps(cdp.evaluate(api.inspect_js(target), timeout=api.read_timeout)))
            elif op == "props" and len(parts) >= 2:
                result = cdp.evaluate(f"window.__host.props({json.dumps(parts[1])})", timeout=api.read_timeout) or {}
                api.log(json.dumps(result))
                if result.get("error"):
                    record(False, f"props {parts[1]}: {result['error']}")
            elif op == "prop" and len(parts) >= 3:
                if len(parts) >= 4:
                    value = api.parse_value(" ".join(parts[3:]))
                    script = (
                        f"window.__host.setProp({json.dumps(parts[1])}, "
                        f"{json.dumps(parts[2])}, {json.dumps(value)})"
                    )
                else:
                    script = (
                        f"window.__host.getProp({json.dumps(parts[1])}, "
                        f"{json.dumps(parts[2])})"
                    )
                result = cdp.evaluate(script) or {}
                api.log(json.dumps({"target": parts[1], "prop": parts[2], **result}))
                if result.get("error"):
                    record(False, f"prop {parts[1]} {parts[2]}: {result['error']}")
            elif op == "clones":
                api.log(json.dumps({"clones": cdp.evaluate("window.__host.clones()", timeout=api.read_timeout)}))
            elif op == "perf":
                api.log(json.dumps(cdp.evaluate("window.__host.perf()")))
            elif op == "errors":
                api.log(json.dumps(api.get_errors(cdp)))
            elif op == "expect_no_errors":
                info = api.get_errors(cdp)
                record(
                    api.errors_ok(info),
                    f"expect_no_errors (vm/page {info.get('count', 0)}, "
                    f"log errors {info.get('logErrors', 0)})",
                )
            elif op == "waituntil" and len(parts) >= 4:
                timeout = float(parts[4]) if len(parts) > 4 else 5000.0
                spec = api.spec(parts[1])
                value = api.parse_value(parts[3])
                result = cdp.evaluate(
                    f"window.__host.waitUntil({json.dumps(spec)}, {json.dumps(parts[2])}, "
                    f"{json.dumps(value)}, {int(timeout)})",
                    await_promise=True, timeout=timeout / 1000.0 + 5,
                ) or {}
                if result.get("ok"):
                    api.log(json.dumps({
                        "waituntil": parts[1], "value": result.get("value"),
                        "frame": result.get("frame"),
                    }))
                else:
                    record(False, f"waituntil {parts[1]} {parts[2]} {value} timed out")
            elif op == "waitstopped":
                timeout = float(parts[1]) if len(parts) > 1 else 60000.0
                result = cdp.evaluate(
                    f"window.__host.waitStopped(0, {int(timeout)})",
                    await_promise=True, timeout=timeout / 1000.0 + 5,
                ) or {}
                if result.get("stopped"):
                    api.log(json.dumps({"waitstopped": result}))
                else:
                    record(False, f"waitstopped timed out after {int(timeout)}ms")
            elif op == "waitpixel" and len(parts) >= 4:
                timeout = float(parts[4]) if len(parts) > 4 else 5000.0
                result = cdp.evaluate(
                    f"window.__host.waitPixel({float(parts[1])!r}, {float(parts[2])!r}, "
                    f"{json.dumps(parts[3])}, {int(timeout)})",
                    await_promise=True, timeout=timeout / 1000.0 + 5,
                ) or {}
                if result.get("ok"):
                    api.log(json.dumps({
                        "waitpixel": [parts[1], parts[2]], "hex": result.get("hex"),
                        "frame": result.get("frame"),
                    }))
                else:
                    record(False, f"waitpixel {parts[1]} {parts[2]} == "
                                  f"{parts[3].lower()} timed out")
            else:
                api.log(f"session: unknown command {line!r}")
        emit({"kind": "session_end", "failures": failures, "asserts": len(asserts),
              "elapsed": round(time.time() - started, 3)})
    finally:
        try:
            cdp.evaluate(api.stop_watch_js())
        except (CDPError, TimeoutError, OSError):
            pass
    return {"asserts": asserts, "failures": failures, "watches": watches}
