"""Regression test for the bundled sb2gs variable/list name-clash rename.

GoboScript rejects a `var x;` next to a `list x;`, so sb2gs renames the later
entry (`x` -> `x list` -> identifier `x_list`). Declarations come from the renamed
table entry, while every reference resolves through an id -> name map, so a rename
that does not also update that map leaves references spelling the *other* entity's
identifier: a sprite reading a stage list then emitted `add ... to x` (the
variable) and the build failed with "unrecognized list".

Builds a project with a stage variable and a stage list both named `x`, referenced
from a sprite, and checks both the generated source and that it builds.
Run with the harness Python:

    python tools/test_sb2gs_clash_refs.py
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

VAR_ID = "clash-var"
LIST_ID = "clash-list"


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


def script(prefix: str, block_id: str, block: dict) -> dict:
    """A flag hat linked to one statement block."""
    hat_id = f"{prefix}hat"
    block["parent"] = hat_id
    return {
        hat_id: {"opcode": "event_whenflagclicked", "next": block_id, "parent": None,
                 "inputs": {}, "fields": {}, "shadow": False, "topLevel": True,
                 "x": 0, "y": 0},
        block_id: block,
    }


def add_to_list(block_id: str, item: str) -> dict:
    return {
        "opcode": "data_addtolist", "next": None, "parent": None,
        "inputs": {"ITEM": [1, [10, item]]},
        "fields": {"LIST": ["x", LIST_ID]}, "shadow": False, "topLevel": False,
    }


def set_variable(block_id: str, value: str) -> dict:
    return {
        "opcode": "data_setvariableto", "next": None, "parent": None,
        "inputs": {"VALUE": [1, [10, value]]},
        "fields": {"VARIABLE": ["x", VAR_ID]}, "shadow": False, "topLevel": False,
    }


def target(name: str, blocks: dict, is_stage: bool, costumes: list[str]) -> dict:
    data = {
        "isStage": is_stage,
        "name": name,
        "variables": {VAR_ID: ["x", "0"]} if is_stage else {},
        "lists": {LIST_ID: ["x", ["a", "b"]]} if is_stage else {},
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
    stage_blocks.update(script("stageAdd", "stageAddBlock", add_to_list("stageAddBlock", "Hello")))
    stage_blocks.update(script("stageVar", "stageSetBlock", set_variable("stageSetBlock", "1")))
    actor_blocks: dict = {}
    actor_blocks.update(script("actorAdd", "actorAddBlock", add_to_list("actorAddBlock", "Hi")))
    actor_blocks.update(script("actorVar", "actorSetBlock", set_variable("actorSetBlock", "2")))
    project = {
        "targets": [target("Stage", stage_blocks, True, ["backdrop1"]),
                    target("Actor", actor_blocks, False, ["costume1"])],
        "monitors": [],
        "extensions": [],
        "meta": {"semver": "3.0.0", "vm": "0"},
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.json", json.dumps(project))
        archive.writestr("Stage-0.png", PNG)
        archive.writestr("Actor-0.png", PNG)


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

    if not bootstrap.sb2gs_present():
        print("SKIP: sb2gs is not installed (run `gsdev setup --only sb2gs`)")
        return 0

    # A fixed workspace path rather than tempfile: some sandboxed harnesses create
    # temp dirs with permissions the test process cannot write into.
    scratch = REPO / "debug" / "sb2gs-clash-tests"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    sb3 = scratch / "clash.sb3"
    out = scratch / "out"
    build_sb3(sb3)

    proc = run(["sb2gs", str(sb3), str(out)])
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        print("FAIL: sb2gs exited non-zero")
        return 1

    stage = (out / "stage.gs").read_text(encoding="utf-8")
    actor = (out / "Actor.gs").read_text(encoding="utf-8")
    failures: list[str] = []
    if "var x;" not in stage and "var x " not in stage:
        failures.append("stage.gs does not declare the variable x")
    if "list x_list" not in stage:
        failures.append("stage.gs does not declare the renamed list x_list")
    if "list x;" in stage:
        failures.append("stage.gs declares a list named x (the clash was not resolved)")
    # The sprite reads the *global* list and variable through their ids; the list
    # reference has to carry the renamed identifier.
    if 'add "Hi" to x_list;' not in actor:
        failures.append("Actor.gs does not add to the renamed list x_list")
    if "x = 2;" not in actor:
        failures.append("Actor.gs does not set the variable x to 2")

    build = run(["build"], env={"GSDEV_PROJECT": str(out)})
    if build.returncode != 0:
        failures.append("the decompiled project does not build")
        print(build.stdout[-1500:])
        print(build.stderr[-1500:])

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print("--- stage.gs")
        print(stage[:1500])
        print("--- Actor.gs")
        print(actor[:1500])
        return 1
    print("ok: a variable/list name clash keeps its references consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
