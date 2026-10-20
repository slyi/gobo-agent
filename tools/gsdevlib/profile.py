"""Profiler report formatting: summary table, frame/time shares, callees, sources.

Pure functions over the report dict; `log` is injected so the CLI keeps its
`[gsdev]` prefix. Extracted from gsdev.py; no browser or CDP dependency.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from pathlib import Path


def short_proc(name: str) -> str:
    """Elide typed-procedure argument signatures for the table (JSON keeps the key)."""
    idx = name.find(": %s")
    if idx < 0:
        return name
    head = name[:idx]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head) + "..."


def print_children(report: dict, name: str, top: int, log=print) -> None:
    """Print the callees of `name` from the call-graph edges."""
    steps = int(report.get("steps") or 0)
    edges = [e for e in (report.get("edges") or [])
             if name.lower() in str(e.get("caller", "")).lower()]
    if not edges:
        log(f"no callees recorded for {name!r} (is the name in the procedure table?)")
        return
    by_key = {str(p.get("key")): p for p in (report.get("procedures") or [])}
    parent_incl = 0
    parent_self = 0
    for p in (report.get("procedures") or []):
        if name.lower() in str(p.get("key", "")).lower():
            parent_incl = int(p.get("inclusive") or 0)
            parent_self = int(p.get("self") or 0)
            break
    if not parent_incl:
        parent_incl = sum(int(e.get("inclusive") or 0) for e in edges)
    log(f"callees of {name}: (share% is of the caller's inclusive; blocks/step is "
        f"edge-inclusive, i.e. the callee plus everything below it)")
    log(f"{'callee':40} {'share%':>7} {'blocks/step':>12} {'calls':>7} {'calls/step':>10}")
    for edge in sorted(edges, key=lambda e: -(e.get("inclusive") or 0))[: max(0, top)]:
        child = by_key.get(str(edge.get("callee"))) or {}
        calls = int(edge.get("calls") or 0)
        incl_ops = int(edge.get("inclusive") or 0)
        incl = (incl_ops / steps) if steps else 0.0
        share = (100.0 * incl_ops / parent_incl) if parent_incl else 0.0
        label = short_proc(str(edge.get("callee", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"{label:40} {share:6.1f}% {incl:12.2f} {calls:7d} "
            f"{(calls / steps if steps else 0):10.2f}")
    if parent_self and steps:
        log(f"(caller self: {parent_self / steps:.2f}/step of "
            f"{(parent_incl / steps) if steps else 0:.2f} inclusive)")


def proc_name_parts(name: str) -> tuple[str, list[str]]:
    """('emit_dashed_row', ['world_y', 'clipped_sx_start']) from a Scratch proccode."""
    proc = ""
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", name or "")
    if match:
        proc = match.group(1)
    params = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*%[sbn]", name or "")
    return proc, params


def attach_sources(report: dict, project_root) -> None:
    """Map each procedure row to its goboscript source, and record a file manifest.

    A goboscript sprite is one `.gs` file named after the sprite, and procedures are
    declared as `proc <name> ...`, so file and line come from scanning the project
    (the VM keeps no source information).
    """
    rows = report.get("procedures") or []
    for row in rows:
        proc, params = proc_name_parts(str(row.get("name", "")))
        row["proc"] = proc
        row["params"] = params
        row["kind"] = ("hat" if str(row.get("name", "")).startswith("event_")
                       else ("procedure" if proc else "internal"))
        row["resolved"] = False
    root = Path(str(project_root)) if project_root else None
    if not root or not root.is_dir():
        return
    files: list[dict] = []
    index: dict[str, tuple[str, int]] = {}
    skip = {"vendor", "node_modules", ".tools", ".git", "__pycache__", "debug"}
    for path in sorted(root.rglob("*.gs")):
        rel = path.relative_to(root).as_posix()
        if any(part in skip for part in Path(rel).parts):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        files.append({"file": rel, "lines": len(lines),
                      "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]})
        for number, line in enumerate(lines, 1):
            match = re.match(r"\s*proc\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if match:
                index.setdefault(match.group(1), (rel, number))
        if len(files) >= 200:
            break
    report["sources"] = {"project_dir": str(root), "files": files}
    for row in rows:
        hit = index.get(str(row.get("proc") or ""))
        if hit:
            row["file"], row["line"] = hit
            row["search"] = f'grep -n "proc {row["proc"]}" {hit[0]}'
            row["resolved"] = True
            row["kind"] = "procedure"


def print_per_frame(report: dict, order: str, top: int, log=print) -> None:
    """Per-frame share spread plus the slowest frames and their main contributors."""
    frames = report.get("frames") or []
    ids = report.get("ids") or {}
    if not frames:
        return
    n = len(frames)
    totals = [max(1, int(f.get("blocks") or 0)) for f in frames]
    by_id: dict[int, list[int]] = {}
    for index, frame in enumerate(frames):
        for entry in (frame.get("ops") or []):
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                counts = by_id.setdefault(int(entry[0]), [0] * n)
                counts[index] += int(entry[1])

    def key_for(rid) -> str:
        return str(ids.get(str(rid)) or ids.get(rid) or f"id{rid}")

    def stats(rid: int):
        counts = by_id.get(rid)
        if not counts:
            return None
        shares = [c / t for c, t in zip(counts, totals)]
        if n > 9:
            deciles = statistics.quantiles(shares, n=10)
            lo, hi = deciles[0], deciles[-1]
        else:
            lo, hi = min(shares), max(shares)
        return statistics.median(shares), lo, hi, 100.0 * sum(1 for c in counts if c) / n

    log(f"per-frame share ({n} frames; share of each frame's blocks, active% = frames "
        f"the row appears in):")
    log(f"{'procedure':40} {'median':>8} {'p10-p90':>15} {'active%':>8}")
    ranked = report.get("procedures") or []
    if order in ("total", "incl", "kids"):
        ranked = sorted(ranked, key=lambda r: -(int(r.get("inclusive") or 0)))
    shown = 0
    for row in ranked:
        outcome = stats(int(row.get("id", -1)))
        if not outcome:
            continue
        median, lo, hi, active = outcome
        label = short_proc(str(row.get("key", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"{label:40} {100 * median:7.1f}% {100 * lo:6.1f}-{100 * hi:5.1f}% "
            f"{active:7.0f}%")
        shown += 1
        if shown >= min(max(top, 5), 10):
            break
    if len(ranked) > shown:
        log(f"... {len(ranked) - shown} more in the JSON (frames matrix + ids table)")

    slowest = sorted(range(n), key=lambda i: -float(frames[i].get("step_ms") or 0))[:3]
    log("slowest frames (instrumented step ms; contributors by share of that frame):")
    for index in slowest:
        frame = frames[index]
        total = max(1, int(frame.get("blocks") or 0))
        parts = []
        for entry in sorted((frame.get("ops") or []), key=lambda e: -int(e[1]))[:3]:
            parts.append(f"{short_proc(key_for(entry[0])).split(' :: ')[-1][:24]} "
                         f"{100 * int(entry[1]) / total:.0f}%")
        log(f"  frame {frame.get('frame')}: {float(frame.get('step_ms') or 0):7.1f} ms, "
            f"{int(frame.get('blocks') or 0):6d} blocks, "
            f"{int(frame.get('draws') or 0):4d} draws -> " + ", ".join(parts))


def print_time_share(report: dict, top: int, log=print) -> None:
    """Sampled per-procedure time as a share of the profiled total (never ms).

    The clock is read once per adaptive window and credited to whichever identity ran
    during it, split into ui-thread (screen-refresh) and all-at-once (without screen
    refresh). Shares survive the profiler's inflation; the ms totals do not.
    """
    total = float(report.get("time_ms_total") or 0.0)
    samples = int(report.get("time_samples") or 0)
    if not total or not samples:
        return
    ranked = sorted(report.get("procedures") or [],
                    key=lambda r: -float(r.get("time_share") or 0.0))
    log(f"profiled time share ({samples} samples, {total:.0f} ms attributed, "
        f"~{int(report.get('sample_every') or 0)} blocks/sample; instrumented — "
        f"compare shares, not ms):")
    log(f"{'procedure':40} {'time%':>7} {'ui%':>6} {'all%':>6} {'blocks%':>8}")
    shown = 0
    for row in ranked:
        share = float(row.get("time_share") or 0.0)
        if share <= 0:
            continue
        ui = 100.0 * float(row.get("ui_time_ms") or 0.0) / total
        all_at_once = 100.0 * float(row.get("warp_time_ms") or 0.0) / total
        label = short_proc(str(row.get("key", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"{label:40} {100 * share:6.1f}% {ui:5.1f}% {all_at_once:5.1f}% "
            f"{100.0 * float(row.get('share') or 0.0):7.1f}%")
        shown += 1
        if shown >= min(max(top, 5), 10):
            break
    unattributed = float(report.get("unattributed_time_ms") or 0.0)
    if unattributed:
        log(f"  (unattributed {100.0 * unattributed / total:.1f}% of sampled time)")


def print_profile(report: dict, top: int, order: str = "self",
                   hats_min: float = 2.0, log=print) -> None:
    """Print the project summary and ranked procedure table.

    `order` picks the ranking: `self` (own body work, the default), `total` (whole
    subtree; `incl` is accepted as an alias), or `kids` (subtree below it). Top-level
    scripts (`event_*` hats) are containers, not optimisable code, so they are
    summarised in a footer and only listed when they hold at least `hats_min` percent
    of the counted work.
    """
    steps = int(report.get("steps") or 0)
    all_rows = report.get("procedures") or []
    total_ops = int(report.get("total_ops") or 0)

    def ops_pct(ops: int) -> float:
        return (100.0 * ops / total_ops) if total_ops else 0.0

    def is_hat(row: dict) -> bool:
        return str(row.get("name", "")).startswith("event_")

    def ctx(row: dict) -> str:
        return {"ui-thread": "ui", "all-at-once": "all", "mixed": "mix"}.get(
            str(row.get("context") or ""), "?")

    # Top-level scripts are containers rather than optimisable code, but they are also
    # where a screen-refresh (non-warp) loop lives, so the ones that carry real work are
    # ranked with everything else and only the small ones are summarised in the footer.
    hats = [r for r in all_rows if is_hat(r)]
    big_hats = [h for h in hats if ops_pct(int(h.get("inclusive") or 0)) >= hats_min]
    rows = [r for r in all_rows if not is_hat(r)] + big_hats

    def rank(row: dict) -> int:
        self_ops = int(row.get("self") or 0)
        incl_ops = int(row.get("inclusive") or 0)
        if order in ("total", "incl"):
            return -incl_ops
        if order == "kids":
            return -max(0, incl_ops - self_ops)
        return -self_ops

    rows = sorted(rows, key=rank)
    # self = ops executed with this procedure innermost (its own body only);
    # kids = its descendants; total = self + kids (the grouped subtree cost).
    # All three percentages share one denominator (all counted ops), so they add up
    # and are comparable across rows; blocks/step is the inclusive subtree cost.
    # ctx: ui = screen-refresh (non-warp, cut at the step's work budget),
    #      all = without-screen-refresh (warp), mix = both.
    log(f"steps={steps} | Step ms={float(report.get('step_ms_mean') or 0.0):.2f} | "
        f"Draws/step={float(report.get('draws_per_step') or 0.0):.1f} | "
        f"Blocks/step={float(report.get('ops_per_step_mean') or 0.0):.1f} | "
        f"total={total_ops} self={int(report.get('self_total') or 0)} "
        f"unattributed={int(report.get('unattributed') or 0)}")
    log(f"{'procedure':40} {'total%':>7} {'self%':>7} {'kids%':>7} {'ctx':>4} "
        f"{'blocks/step':>12} {'calls':>7} {'calls/step':>10}")
    shown_rows = sorted(rows[: max(0, top)],
                        key=lambda r: -int(r.get("inclusive") or 0))
    for row in shown_rows:
        calls = int(row.get("calls") or 0)
        calls_per_step = (calls / steps) if steps else 0.0
        self_ops = int(row.get("self") or 0)
        incl_ops = int(row.get("inclusive") or 0)
        kids_ops = max(0, incl_ops - self_ops)
        incl = (incl_ops / steps) if steps else 0.0
        if total_ops:
            self_pct = 100.0 * self_ops / total_ops
            kids_pct = 100.0 * kids_ops / total_ops
            incl_pct = 100.0 * incl_ops / total_ops
        else:
            self_pct = kids_pct = incl_pct = 0.0
        label = short_proc(str(row.get("key", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"{label:40} {incl_pct:6.1f}% {self_pct:6.1f}% {kids_pct:6.1f}% "
            f"{ctx(row):>4} {incl:12.2f} {calls:7d} {calls_per_step:10.2f}")
    if len(rows) > len(shown_rows):
        log(f"... {len(rows) - len(shown_rows)} more procedure(s) in the JSON report")
    # A top-level script that carries a large share must never be invisible just
    # because its own body is small (hats sort last when ranking by self).
    buried = [h for h in sorted(big_hats, key=lambda r: -int(r.get("inclusive") or 0))
              if h not in rows[: max(0, top)]]
    for hat in buried[:4]:
        label = short_proc(str(hat.get("key", "?")))
        if len(label) > 40:
            label = label[:37] + "..."
        log(f"top-level: {label:36} {ops_pct(int(hat.get('self') or 0)):5.1f}% self "
            f"{ops_pct(int(hat.get('inclusive') or 0)):5.1f}% total  {ctx(hat)}")

    def ops_pct(ops: int) -> float:
        return (100.0 * ops / total_ops) if total_ops else 0.0

    small_hats = [h for h in hats if h not in big_hats]
    if small_hats:
        ranked = sorted(small_hats, key=lambda h: -int(h.get("inclusive") or 0))
        log(f"({len(ranked)} top-level script(s) below {hats_min:g}% total omitted, "
            f"largest: {short_proc(str(ranked[0].get('key', '?')))})")

    # Screen-refresh (non-warp) work that is still running when the step's work budget
    # runs out is what starves a frame; blocks/step cannot show it because the loop is
    # cut at the budget, so call it out explicitly.
    budget_steps = int(report.get("budget_steps") or 0)
    work_time = float(report.get("work_time_ms") or 0.0)
    if budget_steps and steps:
        log(f"screen-refresh budget: {budget_steps}/{steps} steps used >=90% of the "
            f"{work_time:.1f} ms work budget")
        flagged = [r for r in all_rows
                   if float(r.get("budget_pct") or 0) >= 50.0
                   and str(r.get("context")) in ("ui-thread", "mixed")
                   and ops_pct(int(r.get("self") or 0)) >= 5.0]
        for row in sorted(flagged, key=lambda r: -float(r.get("budget_pct") or 0))[:6]:
            label = short_proc(str(row.get("key", "?")))
            if len(label) > 40:
                label = label[:37] + "..."
            log(f"  ! {label:38} held {float(row.get('budget_pct') or 0):5.0f}% of "
                f"budget-bound steps  {(int(row.get('self') or 0) / steps):9.0f} "
                f"blocks/step  {ctx(row)}")
        if not flagged:
            log("  (no single screen-refresh script dominated those steps)")

    print_per_frame(report, order, top, log)
    print_time_share(report, top, log)


def print_profile_steps(report: dict, count: int, log=print) -> None:
    """Print the last N per-step lines from the in-page series."""
    series = report.get("step_series") or []
    if not series or count <= 0:
        return
    tail = series[-count:]
    base = len(series) - len(tail)
    prev_t = float(series[base - 1].get("t") or 0.0) if base > 0 else None
    for row in tail:
        t = float(row.get("t") or 0.0)
        rate = (1000.0 / (t - prev_t)) if (prev_t and t > prev_t) else 0.0
        if t:
            prev_t = t
        log(f"frame {int(row.get('frame') or 0)} | VM steps/s={rate:.1f} | "
            f"Step ms={float(row.get('step_ms') or 0.0):.3f} | "
            f"Draws/step={float(row.get('draws') or 0.0):.0f} | "
            f"Blocks/step={int(row.get('blocks') or 0)}")
