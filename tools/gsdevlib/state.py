"""Selector parsing, variable/list read/write JS, batch writes and watch sampling.

The in-page side is `window.__host`; `ns` lets gsbridge point the same builders at
its injected `window.__bridge` shim. `emit` is injected so the callers keep their
own log format.
"""

from __future__ import annotations

import json
import time


def parse_selector(selector: str) -> tuple[str, str, int | None]:
    """Split ``sprite.variable[index]`` into target, name, and optional index.

    A bare name refers to a Stage (global) variable, matching how goboscript
    exposes globals. The index is 1-based, like Scratch lists. The sprite part
    may carry a ``#N`` clone suffix (``main#1.variable``), resolved by the
    host's ``findTarget``.
    """
    if selector.startswith("@"):
        return "@perf", selector[1:], None
    index = None
    base = selector
    if selector.endswith("]") and "[" in selector:
        head, _, tail = selector[:-1].rpartition("[")
        try:
            index = int(tail)
            base = head
        except ValueError:
            base = selector
    if "." in base:
        target, name = base.split(".", 1)
        return target or "stage", name, index
    return "stage", base, index


_UNSET = object()


def var_js(target: str, name: str, index: int | None = None, value=_UNSET,
            ns: str = "window.__host") -> str:
    """JS for reading (or writing, when `value` is given) one variable/list element.

    `ns` is the page-side object to use, so gsbridge's injected shim
    (`window.__bridge`) reuses this exactly instead of keeping a second implementation.
    It must expose `findTarget(name)` (a Scratch target) and `perfValue(name)`.
    """
    if target == "@perf":
        if value is not _UNSET:
            return "({error: 'read-only: @' + " + json.dumps(name) + "})"
        return (
            "(() => {"
            " const _host = " + ns + ";"
            " if (!_host || !_host.perfValue) return {error: 'no host (open the project with run first)'};"
            " const _want = " + json.dumps(name) + ";"
            " const _out = _host.perfValue(_want);"
            " if (typeof _out === 'undefined') return {error: 'unknown perf metric: ' + _want};"
            " return {value: _out, target: '@perf', name: _want, index: null};"
            "})()"
        )
    idx = "null" if index is None else str(int(index))
    if value is _UNSET:
        mutate = ""
    else:
        literal = json.dumps(value)
        mutate = (
            " if (_idx === null) { _v.value = %s; }"
            " else if (Array.isArray(_v.value)) { _v.value[_idx - 1] = %s; }"
            " else { return {error: 'not a list: ' + %s}; }"
        ) % (literal, literal, json.dumps(name))
    return (
        "(() => {"
        " const _host = " + ns + ";"
        " if (!_host || !_host.findTarget) return {error: 'no host (open the project with run first)'};"
        " const _want = " + json.dumps(target) + ";"
        " const _t = _host.findTarget(_want);"
        " if (!_t) return {error: 'target not found: ' + _want};"
        " let _v = null;"
        " for (const _k in _t.variables) { if (_t.variables[_k].name === "
        + json.dumps(name) + ") { _v = _t.variables[_k]; break; } }"
        " if (!_v) return {error: 'variable not found: ' + " + json.dumps(name) + "};"
        " const _idx = " + idx + ";"
        + mutate +
        " const _out = (_idx === null)"
        "   ? _v.value"
        "   : (Array.isArray(_v.value) ? _v.value[_idx - 1] : undefined);"
        " return {value: _out, target: _want, name: " + json.dumps(name) + ", index: _idx};"
        "})()"
    )


def parse_value(raw: str):
    """Parse a CLI value as JSON when possible, otherwise keep it a string."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def watch_install_js(specs: list[tuple], labels: list[str], interval_ms: int,
                      ns: str = "window.__host",
                      glob: str = "window.__gsdevWatch") -> str:
    spec_list = "[" + ",".join(
        "{target: %s, name: %s, label: %s, index: %s}"
        % (
            json.dumps(target),
            json.dumps(name),
            json.dumps(label),
            "null" if index is None else str(int(index)),
        )
        for (target, name, index), label in zip(specs, labels)
    ) + "]"
    return (
        "(() => {"
        " const _host = " + ns + ";"
        " if (!_host || !_host.findTarget) return {error: 'no host (open the project with run first)'};"
        " const _specs = " + spec_list + ";"
        " const _refs = [];"
        " for (const _s of _specs) {"
        "   if (_s.target === '@perf') { _refs.push({label: _s.label, ref: {__perf: _s.name}, index: null}); continue; }"
        "   const _t = _host.findTarget(_s.target);"
        "   if (!_t) return {error: 'target not found: ' + _s.target};"
        "   let _v = null;"
        "   for (const _k in _t.variables) { if (_t.variables[_k].name === _s.name) { _v = _t.variables[_k]; break; } }"
        "   if (!_v) return {error: 'variable not found: ' + _s.name};"
        "   _refs.push({label: _s.label, ref: _v, index: _s.index});"
        " }"
        " const _read = (_r) => (_r.ref.__perf)"
        "   ? " + ns + ".perfValue(_r.ref.__perf)"
        "   : ((_r.index === null)"
        "     ? _r.ref.value"
        "     : (Array.isArray(_r.ref.value) ? _r.ref.value[_r.index - 1] : undefined));"
        " const _fmt = (x) => {"
        "   if (Array.isArray(x)) { const s = JSON.stringify(x);"
        "     return s.length > 200 ? s.slice(0, 200) + '...(' + x.length + ')' : s; }"
        "   if (x !== null && typeof x === 'object') { try { return JSON.stringify(x); } catch (e) { return String(x); } }"
        "   return x; };"
        " const _prev = " + glob + ";"
        " if (_prev && _prev.timer) clearInterval(_prev.timer);"
        " const _W = " + glob + " = {on: true, samples: [], refs: _refs, t0: performance.now()};"
        " _W.timer = setInterval(() => { if (!_W.on) return;"
        "   const _row = [Math.round(performance.now() - _W.t0)];"
        "   for (const _r of _W.refs) _row.push(_fmt(_read(_r)));"
        "   _W.samples.push(_row);"
        "   if (_W.samples.length > 5000) _W.samples.splice(0, _W.samples.length - 2000);"
        " }, " + str(interval_ms) + ");"
        " return {ok: true, labels: _refs.map(r => r.label)};"
        "})()"
    )


def drain_watch_js(glob: str = "window.__gsdevWatch") -> str:
    return (f"JSON.stringify(({glob}"
            f" && {glob}.samples.splice(0, {glob}.samples.length)) || [])")


def stop_watch_js(glob: str = "window.__gsdevWatch") -> str:
    return (f"(() => {{ if ({glob}) {{ {glob}.on = false;"
            f" if ({glob}.timer) clearInterval({glob}.timer); }} return 'ok'; }})()")


def select_js(selector: str) -> str:
    target, name, index = parse_selector(selector)
    return var_js(target, name, index)


def apply_batch(cdp, tokens: list[str], ns: str = "window.__host", emit=None) -> None:
    """Set several variables/lists in ONE evaluate, so the VM cannot step between.

    ``tokens`` are ``selector=value`` (lists and indices supported). This is what
    removes the race where a multi-variable fixture is read with only one of the
    pair updated: a single evaluate runs as one JS task on the same thread as the
    VM step, so no thread can observe a half-applied update. `ns` lets gsbridge reuse
    this against its injected shim, and `log_fn` keeps the output in the caller's own
    format (gsbridge's log has no `[gsdev]` prefix).
    """
    emit = emit or print
    labels: list[str] = []
    exprs: list[str] = []
    for token in tokens:
        selector, sep, raw = token.partition("=")
        if not sep:
            raise SystemExit(f"bad assignment {token!r} (want selector=value)")
        target, name, index = parse_selector(selector)
        exprs.append(var_js(target, name, index, parse_value(raw), ns=ns))
        labels.append(selector)
    script = (
        "(() => { const out = [];"
        + "".join(f" out.push({expr});" for expr in exprs)
        + " return JSON.stringify(out); })()"
    )
    emit(json.dumps({"set_batch": labels, "results": json.loads(cdp.evaluate(script))}))


def watch_run(
    cdp, selectors: list[str], duration_ms: float, interval_ms: int = 20,
    ns: str = "window.__host", glob: str = "window.__gsdevWatch", emit=None,
    json_mode: bool = False,
) -> dict:
    emit = emit or print
    specs = [parse_selector(selector) for selector in selectors]
    labels = list(selectors)
    result = cdp.evaluate(
        watch_install_js(specs, labels, max(1, int(interval_ms)), ns=ns, glob=glob))
    if not result or result.get("error"):
        error = (result or {}).get("error", "unknown error")
        if not json_mode:
            emit(f"watch failed: {error}")
        return {"ok": False, "error": error, "labels": labels, "rows": [], "summary": []}
    rows: list[list] = []
    deadline = time.monotonic() + duration_ms / 1000.0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        for row in json.loads(cdp.evaluate(drain_watch_js(glob)) or "[]"):
            rows.append(row)
            if json_mode:
                continue
            values = " ".join(f"{labels[i]}={row[i + 1]}" for i in range(len(labels)))
            print(f"[WATCH +{row[0]}ms] {values}", flush=True)
    summary = []
    for i, label in enumerate(labels):
        numbers = [row[i + 1] for row in rows if isinstance(row[i + 1], (int, float))]
        if not numbers:
            continue
        mean = sum(numbers) / len(numbers)
        summary.append({
            "label": label, "n": len(numbers),
            "mean": mean, "min": min(numbers), "max": max(numbers),
        })
        if not json_mode:
            print(
                f"[WATCH summary] {label} n={len(numbers)} mean={mean:.4g} "
                f"min={min(numbers):.4g} max={max(numbers):.4g}",
                flush=True,
            )
    return {"ok": True, "labels": labels, "rows": rows, "summary": summary}



def spec(selector: str) -> dict:
    target, name, index = parse_selector(selector)
    return {"target": target, "name": name, "index": index, "label": selector}
