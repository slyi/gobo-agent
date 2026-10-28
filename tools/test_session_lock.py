"""Single-session lock regression tests (maintainer-only).

Run: python tools/test_session_lock.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "debug" / "session-lock-test"

CHILD = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, "tools/gsdevlib")
sys.path.insert(0, "tools")
import host

root = Path(sys.argv[1])
attach = sys.argv[2] == "attach"
port = int(sys.argv[3]) if len(sys.argv) > 3 else 9999
tool = sys.argv[4] if len(sys.argv) > 4 else "gsdev"
host.configure(SESSION_ROOT=root, HOST_PROFILE_ROOT=root / "profiles")
while not (root / "go").exists():
    time.sleep(0.005)
try:
    host.acquire_session(port, root / "profiles" / "local", tool, attach=attach)
    print("OK", flush=True)
    time.sleep(2.0)
except BaseException as exc:
    print(f"ERR {type(exc).__name__} {exc}", flush=True)
"""


def _start(root: Path, mode: str = "launch", port: int = 9999,
           tool: str = "gsdev") -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(root), mode, str(port), tool],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _run_pair(root: Path, mode: str = "launch") -> list[str]:
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    procs = [_start(root, mode), _start(root, mode)]
    time.sleep(0.3)
    (root / "go").write_text("go", encoding="utf-8")
    outputs = [proc.communicate(timeout=10)[0].strip() for proc in procs]
    return outputs


def main() -> int:
    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        if not condition:
            failures += 1
        print(f"[{'ok' if condition else 'FAIL':4}] {label}"
              f"{(' - ' + detail) if detail else ''}")

    try:
        outputs = _run_pair(ROOT)
        check("fresh simultaneous claim has one winner",
              sum(out.startswith("OK") for out in outputs) == 1, str(outputs))

        shutil.rmtree(ROOT, ignore_errors=True)
        ROOT.mkdir(parents=True)
        (ROOT / "session.json").write_text(json.dumps({
            "pid": 999999, "port": 9999,
            "profile": str(ROOT / "profiles" / "local"),
            "tool": "gsdev", "started": 0,
        }), encoding="utf-8")
        outputs = _run_pair(ROOT)
        check("stale simultaneous claim has one winner",
              sum(out.startswith("OK") for out in outputs) == 1, str(outputs))

        # Explicit isolated session roots (tests/CI) may run independently.
        a, b = ROOT / "a", ROOT / "b"
        for path in (a, b):
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True)
        pa, pb = _start(a), _start(b)
        time.sleep(0.3)
        (a / "go").write_text("go", encoding="utf-8")
        (b / "go").write_text("go", encoding="utf-8")
        outputs = [pa.communicate(timeout=10)[0].strip(),
                   pb.communicate(timeout=10)[0].strip()]
        check("separate session roots do not collide",
              all(out.startswith("OK") for out in outputs), str(outputs))

        # A warm host may be attached by a second command, but a second launch
        # must still be rejected.
        root = ROOT / "attach"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        holder = _start(root)
        time.sleep(0.3)
        (root / "go").write_text("go", encoding="utf-8")
        attach = _start(root, "attach")
        launch = _start(root, "launch")
        attach_out = attach.communicate(timeout=10)[0].strip()
        launch_out = launch.communicate(timeout=10)[0].strip()
        holder_out = holder.communicate(timeout=10)[0].strip()
        check("holder acquired", holder_out.startswith("OK"), holder_out)
        check("attach to warm host accepted", attach_out.startswith("OK"), attach_out)
        check("second launch rejected", launch_out.startswith("ERR"), launch_out)

        # A different project/port/tool still shares the user-level session lock.
        root = ROOT / "different"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        holder = _start(root, port=9999, tool="gsdev")
        time.sleep(0.3)
        (root / "go").write_text("go", encoding="utf-8")
        challenger = _start(root, port=10000, tool="gsbridge")
        holder_out = holder.communicate(timeout=10)[0].strip()
        challenger_out = challenger.communicate(timeout=10)[0].strip()
        check("different project/port/tool rejected",
              holder_out.startswith("OK") and challenger_out.startswith("ERR"),
              f"{holder_out} / {challenger_out}")
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
        shutil.rmtree(ROOT / "alternate", ignore_errors=True)

    print(f"\nFAILURES: {'<none>' if not failures else failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
