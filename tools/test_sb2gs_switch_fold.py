"""Regression test for the bundled sb2gs constant costume/backdrop switch folding.

A `switch costume to [...]` input normally points at a shadow `looks_costume` menu
block instead of an inline literal, and older saves store that menu's text as a
number ("2") rather than a costume name. goboscript has no index form, so the
decompiler has to resolve the number the way scratch-vm does: costume name first,
1-based index second. See SB2GS_SWITCH_FOLD_PATCHES in tools/bootstrap.py.

Builds that project in memory (no network, no browser) and checks the generated
goboscript. Run with the harness Python:

    python tools/test_sb2gs_switch_fold.py
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

# The sprite's costumes: the fourth is named "2", so a menu holding "2" is a *name*
# there while index 2 is "beta". The fifth is last, which is what a 0 index selects.
SPRITE_COSTUMES = ["alpha", "beta", "gamma", "2", "23"]


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


def script(prefix: str, opcode: str, input_name: str, menu_opcode: str, field: str,
           value: str) -> dict:
    """`when flag clicked` -> switch, with the menu as a shadow block reference."""
    hat, switch, menu = f"{prefix}h", f"{prefix}s", f"{prefix}m"
    return {
        hat: {"opcode": "event_whenflagclicked", "next": switch, "parent": None,
              "inputs": {}, "fields": {}, "shadow": False, "topLevel": True,
              "x": 0, "y": 0},
        switch: {"opcode": opcode, "next": None, "parent": hat,
                 "inputs": {input_name: [1, menu]}, "fields": {},
                 "shadow": False, "topLevel": False},
        menu: {"opcode": menu_opcode, "next": None, "parent": switch, "inputs": {},
               "fields": {field: [value, None]}, "shadow": True, "topLevel": False},
    }


def target(name: str, costumes: list[str], blocks: dict, is_stage: bool) -> dict:
    data = {
        "isStage": is_stage,
        "name": name,
        "variables": {},
        "lists": {},
        "broadcasts": {},
        "blocks": blocks,
        "comments": {},
        "currentCostume": 0,
        "costumes": [costume(n, f"{name}-{i}.png") for i, n in enumerate(costumes)],
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
    stage_blocks: dict = {}
    for i, value in enumerate(["2", "1", "99", "next backdrop"]):
        stage_blocks.update(script(f"b{i}", "looks_switchbackdropto", "BACKDROP",
                                   "looks_backdrops", "BACKDROP", value))
    sprite_blocks: dict = {}
    for i, value in enumerate(["2", "3", "0", "99", "beta"]):
        sprite_blocks.update(script(f"c{i}", "looks_switchcostumeto", "COSTUME",
                                    "looks_costume", "COSTUME", value))
    project = {
        "targets": [
            target("Stage", ["b0", "b1"], stage_blocks, True),
            target("Actor", SPRITE_COSTUMES, sprite_blocks, False),
        ],
        "monitors": [],
        "extensions": [],
        "meta": {"semver": "3.0.0", "vm": "0"},
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.json", json.dumps(project))
        for name, costumes in (("Stage", ["b0", "b1"]), ("Actor", SPRITE_COSTUMES)):
            for i in range(len(costumes)):
                archive.writestr(f"{name}-{i}.png", PNG)


def switches(source: str) -> list[str]:
    return [line.strip() for line in source.splitlines() if "switch_" in line]


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


def main() -> int:
    import bootstrap

    if not bootstrap.sb2gs_present():
        print("SKIP: sb2gs is not installed (run `gsdev setup --only sb2gs`)")
        return 0

    failures: list[str] = []
    # A fixed workspace path rather than tempfile: some sandboxed harnesses create
    # temp dirs with permissions the test process cannot write into.
    scratch = REPO / "debug" / "sb2gs-tests"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    sb3 = scratch / "switch-fold.sb3"
    out = scratch / "out"
    build_sb3(sb3)
    proc = subprocess.run(
        gsdev_command() + ["sb2gs", str(sb3), str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        print("FAIL: sb2gs exited non-zero")
        return 1
    actor = (out / "Actor.gs").read_text(encoding="utf-8")
    stage = (out / "stage.gs").read_text(encoding="utf-8")

    # A numeric menu text is a costume *name* when one carries it, and only falls
    # back to a 1-based index otherwise.
    expected_actor = [
        'switch_costume "2";',      # "2" names the fourth costume, not index 2
        'switch_costume "gamma";',  # "3" is index 3
        'switch_costume "23";',     # "0" / any non-positive index is the last costume
        'switch_costume "beta";',   # a plain name is emitted unchanged
    ]
    expected_stage = [
        'switch_backdrop "b1";',    # "2" is index 2
        'switch_backdrop "b0";',    # "1" is index 1
        "next_backdrop;",           # a relative title keeps its own statement
    ]
    for expected in expected_actor:
        if expected not in actor:
            failures.append(f"Actor.gs is missing {expected}")
    for expected in expected_stage:
        if expected not in stage:
            failures.append(f"stage.gs is missing {expected}")
    if any("99" in line for line in switches(actor) + switches(stage)):
        # goboscript rejects a constant selector no costume carries, and scratch-vm
        # leaves the costume alone, so the block must be dropped instead.
        failures.append("an out-of-range constant switch was emitted")
    if any("next backdrop" in line for line in switches(actor) + switches(stage)):
        failures.append("a relative backdrop title was not translated")
    if actor.count('switch_costume "2";') != 1:
        failures.append("the name/index split for a numeric menu is ambiguous")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print("--- Actor.gs")
        print(actor)
        return 1
    print("ok: constant costume/backdrop switches fold to names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
