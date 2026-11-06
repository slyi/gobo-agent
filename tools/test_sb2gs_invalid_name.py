"""Regression test for the bundled sb2gs sprite-filename warning.

A sprite is written as `<name>.gs`. On Windows an unusable name does not fail
loudly: `blank :).gs` is read as the alternate data stream `).gs` of a file named
`blank `, so the write "succeeds" into an empty file with the source hidden and
the sprite is silently lost. The patched sb2gs warns and skips that sprite instead
(see SB2GS_SPRITE_NAME_PATCHES in tools/bootstrap.py).

Windows-only: `:` is a legal filename character on POSIX.
Run with the harness Python:

    python tools/test_sb2gs_invalid_name.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GSDEV = REPO / "tools" / "gsdev.py"
sys.path.insert(0, str(REPO / "tools"))

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

BAD_NAME = "bad:name"


def costume(name: str, md5ext: str) -> dict:
    return {
        "name": name,
        "assetId": md5ext.split(".")[0],
        "md5ext": md5ext,
        "dataFormat": "png",
        "rotationCenterX": 0,
        "rotationCenterY": 0,
        "bitmapResolution": 1,
        "size": 1,
    }


def target(name: str, is_stage: bool, md5ext: str) -> dict:
    blocks = {
        "hat": {"opcode": "event_whenflagclicked", "next": None, "parent": None,
                "inputs": {}, "fields": {}, "shadow": False, "topLevel": True,
                "x": 0, "y": 0},
    }
    data = {
        "isStage": is_stage,
        "name": name,
        "variables": {},
        "lists": {},
        "broadcasts": {},
        "blocks": blocks,
        "comments": {},
        "currentCostume": 0,
        "costumes": [costume("costume1", md5ext)],
        "sounds": [],
        "volume": 100,
        "layerOrder": 0 if is_stage else 1,
        "visible": True,
        "x": 0,
        "y": 0,
        "size": 100,
        "direction": 90,
        "draggable": False,
        "rotationStyle": "all around",
    }
    if is_stage:
        data.update({"tempo": 60, "videoTransparency": 50, "videoState": "off",
                     "textToSpeechLanguage": None})
    return data


def build_sb3(path: Path) -> None:
    targets = [target("Stage", True, "stage.png"),
               target("Actor", False, "actor.png"),
               target(BAD_NAME, False, "bad.png")]
    project = {
        "targets": targets,
        "monitors": [],
        "extensions": [],
        "meta": {"semver": "3.0.0", "vm": "0"},
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.json", json.dumps(project))
        for name in ("stage.png", "actor.png", "bad.png"):
            archive.writestr(name, PNG)


def gsdev_command() -> list[str]:
    """Run gsdev through the repo launcher: it resolves an interpreter that can
    import sb2gs's own dependencies, which a bare `sys.executable` may not."""
    if os.name == "nt" and (REPO / "tools" / "gsdev.ps1").is_file():
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(REPO / "tools" / "gsdev.ps1")]
    launcher = REPO / "tools" / "gsdev"
    if launcher.is_file():
        return [str(launcher)]
    return [sys.executable, str(GSDEV)]


def run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(gsdev_command() + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env={**os.environ, **(env or {})})


def main() -> int:
    import bootstrap

    if os.name != "nt":
        print("SKIP: the invalid-filename check is Windows-specific")
        return 0
    if not bootstrap.sb2gs_present():
        print("SKIP: sb2gs is not installed (run `gsdev setup --only sb2gs`)")
        return 0

    # A fixed workspace path rather than tempfile: some sandboxed harnesses create
    # temp dirs with permissions the test process cannot write into.
    scratch = REPO / "debug" / "sb2gs-name-tests"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    sb3 = scratch / "bad-name.sb3"
    out = scratch / "out"
    build_sb3(sb3)

    proc = run(["sb2gs", str(sb3), str(out)])
    output = proc.stdout + proc.stderr
    failures: list[str] = []
    if proc.returncode != 0:
        failures.append("sb2gs exited non-zero")
    if "cannot be written" not in output:
        failures.append("no warning about the unusable sprite name")
    if BAD_NAME not in output or "Rename the sprite" not in output:
        failures.append("the warning does not name the sprite and the fix")
    if not (out / "Actor.gs").is_file():
        failures.append("the valid sprite was not decompiled")
    stray = [p.name for p in out.iterdir() if p.name.startswith("bad")]
    if stray:
        failures.append(f"a file was still written for the bad name: {stray}")

    # The skipped sprite stays in goboscript.toml's layer list, so the build fails
    # with a message that names it rather than silently producing a lesser project.
    build = run(["build"], env={"GSDEV_PROJECT": str(out)})
    if build.returncode == 0:
        failures.append("the build succeeded with a sprite file missing")
    elif BAD_NAME not in (build.stdout + build.stderr):
        failures.append("the build failure does not name the skipped sprite")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print("--- sb2gs output")
        print(output[-1500:])
        return 1
    print("ok: an unusable sprite filename warns and is skipped, not silently lost")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
