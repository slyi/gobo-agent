#!/usr/bin/env python3
"""Cross-tool verb parity guard.

gsdev and gsbridge are two front ends for the same verbs, so their *command syntax must
not drift*. This module is the single source of truth for what is shared and which
differences are deliberate:

* `SHARED`      — verbs whose usage string must be byte-identical in both tools
                  (whitespace/wrapping normalised).
* `INTENTIONAL` — verbs whose syntax legitimately differs, with the reason. These are
                  the only allowed divergences.
* `PENDING_*`   — verbs one tool still lacks (the Phase 3 worklist). Informational, but
                  a verb that is in neither list is reported as unexpected drift, so a
                  newly added verb cannot silently skip the other tool.

`gsdev selftest` calls `compare()`; run this file directly for the full table:

    python tools/verb_parity.py
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent

# Verbs both tools must accept with the same usage string.
SHARED = (
    "click",
    "close",
    "broadcast",
    "broadcast_wait",
    "clones",
    "errors",
    "expect_no_errors",
    "frame",
    "get",
    "gpu",
    "inspect",
    "key",
    "logs",
    "mouse",
    "pause",
    "perf",
    "pixel",
    "profilereset",
    "prop",
    "props",
    "record",
    "render",
    "restart",
    "resume",
    "set",
    "set_batch",
    "setprofiling",
    "stop_record",
    "step",
    "targets",
    "test",
    "stop",
    "trace",
    "until",
    "watch",
    "wait_frame",
    "wait_pixel",
    "wait_stopped",
    "wait_until",
)

# Verbs whose syntax deliberately differs (the bridge drives a foreign live page, which
# has no local build/reload/mode or session-bundle concept).
INTENTIONAL = {
    "run": "bridge adds --target/--url/--project-id/--browser/--no-start/--ready-timeout",
    "screenshot": "bridge captures a live page (no --mode/--no-build/--sb3/--delay)",
    "profile": "bridge has no build/reload/warmup window or --mode",
    "profiling": "report flags differ (bridge has no --steps/--children/--sort/--hats-min)",
    "session": "bridge session ops are a subset (no --json/--events/--bundle/--artifacts)",
}

# Verbs gsdev has that the bridge does not yet (Phase 3 worklist).
PENDING_IN_BRIDGE = ()

# Verbs the bridge has that gsdev does not yet.
PENDING_IN_GSDEV = ()

# gsdev-only by design: they build/install/profile a *local* project, which the bridge
# (a live-site client) has no business doing.
GSDEV_ONLY = (
    "build",
    "doctor",
    "init",
    "preflight",
    "sb2gs",
    "selftest",
    "setup",
    "status",
    "tasks",
)


def _subparsers(parser: argparse.ArgumentParser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def usage(sub: argparse.ArgumentParser) -> str:
    """A subparser's usage string, normalised (program name and wrapping dropped)."""
    text = sub.format_usage()
    text = re.sub(r"^\s*usage:\s*\S+\s*", "", text)
    return " ".join(text.split())


def _load(name: str):
    os.environ.setdefault("GSDEV_PROJECT", str(REPO))
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    return importlib.import_module(name)


def compare() -> tuple[list[str], int]:
    """Return (problems, shared_count). An empty problem list means parity holds."""
    gsdev = _load("gsdev")
    gsbridge = _load("gsbridge")
    g = _subparsers(gsdev.build_parser())
    b = _subparsers(gsbridge.build_parser())
    problems: list[str] = []

    for verb in SHARED:
        for label, table in (("gsdev", g), ("gsbridge", b)):
            if verb not in table:
                problems.append(f"{verb}: missing from {label}")
    for verb in sorted(set(g) & set(b)):
        if verb in INTENTIONAL or verb in SHARED:
            continue
        problems.append(
            f"{verb}: in both tools but declared neither SHARED nor INTENTIONAL"
        )
    for verb in SHARED:
        if verb in g and verb in b and usage(g[verb]) != usage(b[verb]):
            problems.append(
                f"{verb}: usage differs\n"
                f"    gsdev   : {usage(g[verb])}\n"
                f"    gsbridge: {usage(b[verb])}"
            )
    declared = set(SHARED) | set(INTENTIONAL) | set(PENDING_IN_BRIDGE) \
        | set(PENDING_IN_GSDEV) | set(GSDEV_ONLY)
    for label, table in (("gsdev", g), ("gsbridge", b)):
        for verb in sorted(set(table) - declared):
            problems.append(f"{verb}: present in {label} but not declared in verb_parity")
    return problems, len(SHARED)


def report() -> int:
    gsdev = _load("gsdev")
    gsbridge = _load("gsbridge")
    g = _subparsers(gsdev.build_parser())
    b = _subparsers(gsbridge.build_parser())
    shared = sorted(set(g) & set(b))
    identical = [v for v in shared if v in SHARED and usage(g[v]) == usage(b[v])]
    print(f"gsdev {len(g)} verbs, gsbridge {len(b)} verbs, {len(shared)} shared by name")
    print(f"  identical usage (SHARED): {len(identical)}/{len(SHARED)}")
    print(f"  intentional differences  : {sorted(INTENTIONAL)}")
    print(f"  pending in bridge        : {list(PENDING_IN_BRIDGE)}")
    print(f"  pending in gsdev         : {list(PENDING_IN_GSDEV)}")
    print(f"  gsdev-only by design     : {list(GSDEV_ONLY)}")
    for verb in sorted(set(shared) - set(SHARED) - set(INTENTIONAL)):
        print(f"  UNDECLARED shared verb   : {verb}")
        print(f"      gsdev   : {usage(g[verb])}")
        print(f"      gsbridge: {usage(b[verb])}")
    problems, count = compare()
    if problems:
        print(f"\nPARITY PROBLEMS ({len(problems)}):")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"\nparity OK ({count} shared verbs byte-identical)")
    return 0


if __name__ == "__main__":
    raise SystemExit(report())
