"""Build pipeline: goboscript invocation, source comment carriers, sb3 injection.

The compiler path, mode-include path and pre-build hook are injected by gsdev.py
(they depend on project config); everything here is filesystem/sb3 work.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path


# --- debug / release builds ---------------------------------------------------
# A harness-generated include (`lib/gsdev_mode.gs`) provides diagnostic statement
# macros. Debug expands them to real statements; release erases the statement *and
# its arguments* at compile time (verified empty macro expansion). A project opts in
# with `%include lib/gsdev_mode` and calls the macros as statements without a
# trailing `;` (the macro owns the terminator).
MODE_MACROS = {
    "debug": (
        "%define DBG_LOG(msg) log msg;\n"
        "%define DBG_SAY(msg) say msg;\n"
        "%define DBG_ADD(target, n) target += n;\n"
        "%define DBG_SET(target, v) target = v;\n"
    ),
    "release": (
        "%define DBG_LOG(msg)\n"
        "%define DBG_SAY(msg)\n"
        "%define DBG_ADD(target, n)\n"
        "%define DBG_SET(target, v)\n"
    ),
}
def resolve_mode(args=None) -> str:
    mode = getattr(args, "mode", None) or os.environ.get("GSDEV_MODE") or "debug"
    return mode if mode in MODE_MACROS else "debug"


# TurboWarp/forkphorus runtime-detection reporters have no goboscript spelling, so
# sb2gs writes this marker string where they were (see SB2GS_TW_DETECTION_PATCHES in
# bootstrap.py); the build turns it back into the original opcode.
TW_MARKER_PREFIX = "__gsdev_tw_op_"
TW_MARKER_SUFFIX = "__"


def _tw_marker_opcode(value: object) -> str | None:
    """The opcode a marker string names, if `value` is one."""
    if (isinstance(value, str) and value.startswith(TW_MARKER_PREFIX)
            and value.endswith(TW_MARKER_SUFFIX)):
        return value[len(TW_MARKER_PREFIX):-len(TW_MARKER_SUFFIX)] or None
    return None


def _tw_marker_in_input(value: object) -> str | None:
    """The opcode of a marker inside one block input.

    Covers a literal shadow (`[1, [10, "<marker>"]]`) and a `[3, block, fallback]`
    input, so both boolean and any/number/string positions are found.
    """
    if not isinstance(value, list) or len(value) < 2:
        return None
    payload = value[1]
    if isinstance(payload, list) and len(payload) >= 2:
        found = _tw_marker_opcode(payload[1])
        if found:
            return found
    if len(value) >= 3:
        return _tw_marker_in_input(value[2])
    return None


def inject_tw_blocks(sb3_path: Path, root: Path) -> None:
    """Turn marker placeholders in a built sb3 back into the TW detection reporters.

    goboscript has no syntax for `turbowarp_isCompiled` / `turbowarp_isTurboWarp` (nor
    the forkphorus equivalents), so sb2gs emits the marked string constant
    `"__gsdev_tw_op_<opcode>__"` in its place. In a boolean input goboscript compiles
    that to `<marker> == 1`, so the marker ends up inside a real `operator_equals`
    block: mutating that block into the original opcode keeps the wiring, so the
    enclosing condition holds the reporter again. In an any/number/string input the
    marker stays a shadow string, so the input is repointed at a new block instead.
    Best-effort and a no-op when no marker is present; runs after the build, like
    `inject_monitors`.
    """
    try:
        with zipfile.ZipFile(sb3_path) as archive:
            project = json.loads(archive.read("project.json"))
            members = [
                (name, archive.read(name))
                for name in archive.namelist()
                if name != "project.json"
            ]
    except (OSError, ValueError, zipfile.BadZipFile):
        return
    changed = False
    created = 0
    for target in project.get("targets", []):
        blocks = target.get("blocks")
        if not isinstance(blocks, dict):
            continue
        for block_id, block in list(blocks.items()):
            if not isinstance(block, dict):
                continue
            inputs = block.get("inputs")
            if not isinstance(inputs, dict):
                continue
            if block.get("opcode") == "operator_equals":
                opcode = None
                for value in inputs.values():
                    opcode = _tw_marker_in_input(value)
                    if opcode:
                        break
                if opcode:
                    block["opcode"] = opcode
                    block["inputs"] = {}
                    block["fields"] = {}
                    changed = True
                    continue
            for name, value in list(inputs.items()):
                opcode = _tw_marker_in_input(value)
                if not opcode:
                    continue
                created += 1
                new_id = f"gsdevtw{created}"
                blocks[new_id] = {
                    "opcode": opcode, "next": None, "parent": block_id,
                    "inputs": {}, "fields": {}, "shadow": False, "topLevel": False,
                }
                inputs[name] = [2, new_id] if value[0] == 1 else [3, new_id, [10, ""]]
                changed = True
    if not changed:
        return
    tmp = sb3_path.with_name(sb3_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("project.json", json.dumps(project, ensure_ascii=False))
            for name, data in members:
                archive.writestr(name, data)
        os.replace(tmp, sb3_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def inject_monitors(sb3_path: Path, root: Path) -> None:
    """Re-inject the source project's monitors into a built sb3.

    goboscript emits an empty `monitors` array, so monitor positions, slider
    modes and visibility are lost. sb2gs records them in `monitors.json` (with the
    compiled variable/list name); this maps each back to the compiled id and
    writes the array into the sb3. Best-effort: a missing/invalid sidecar is a
    no-op.
    """
    sidecar = root / "monitors.json"
    if not sidecar.is_file():
        return
    try:
        records = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(records, list) or not records:
        return
    try:
        with zipfile.ZipFile(sb3_path) as archive:
            project = json.loads(archive.read("project.json"))
            members = [
                (name, archive.read(name))
                for name in archive.namelist()
                if name != "project.json"
            ]
    except (OSError, ValueError, zipfile.BadZipFile):
        return
    targets: dict[str | None, dict] = {
        None: next((t for t in project["targets"] if t.get("isStage")), None)
    }
    for target in project["targets"]:
        if not target.get("isStage"):
            targets[target["name"]] = target
    monitors = []
    for record in records:
        target = targets.get(record.get("target"))
        if target is None:
            continue
        is_list = record.get("kind") == "list"
        name = record.get("name")
        table = target.get("lists" if is_list else "variables", {})
        var_id = next((vid for vid, entry in table.items() if entry[0] == name), None)
        if var_id is None:
            continue
        monitors.append({
            "id": var_id,
            "mode": record.get("mode", "default"),
            "opcode": "data_listcontents" if is_list else "data_variable",
            "params": {"LIST" if is_list else "VARIABLE": name},
            "spriteName": record.get("target"),
            "value": 0,
            "width": 0,
            "height": 0,
            "x": record.get("x", 0),
            "y": record.get("y", 0),
            "visible": bool(record.get("visible", False)),
            "sliderMin": record.get("sliderMin", 0),
            "sliderMax": record.get("sliderMax", 100),
            "isDiscrete": record.get("isDiscrete", True),
        })
    if not monitors:
        return
    project["monitors"] = monitors
    tmp = sb3_path.with_name(sb3_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("project.json", json.dumps(project, ensure_ascii=False))
            for name, data in members:
                archive.writestr(name, data)
        os.replace(tmp, sb3_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def inject_costumes(sb3_path: Path, root: Path) -> None:
    """Set the source rotation centers on a built sb3's costumes.

    sb2gs preserves costumes byte-for-byte, so the compiled costumes have no
    `rotationCenterX`/`rotationCenterY`/`bitmapResolution`; this restores them from
    the `costumes.json` sidecar (matched by target + costume name, else order).
    Best-effort.
    """
    sidecar = root / "costumes.json"
    if not sidecar.is_file():
        return
    try:
        records = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(records, list) or not records:
        return
    try:
        with zipfile.ZipFile(sb3_path) as archive:
            project = json.loads(archive.read("project.json"))
            members = [
                (name, archive.read(name))
                for name in archive.namelist()
                if name != "project.json"
            ]
    except (OSError, ValueError, zipfile.BadZipFile):
        return
    targets: dict[str | None, dict] = {
        None: next((t for t in project["targets"] if t.get("isStage")), None)
    }
    for target in project["targets"]:
        if not target.get("isStage"):
            targets[target["name"]] = target
    applied = 0
    for record in records:
        target = targets.get(record.get("target"))
        rcx = record.get("rotationCenterX")
        rcy = record.get("rotationCenterY")
        if target is None or rcx is None or rcy is None:
            continue
        costumes = target.get("costumes", [])
        name = record.get("name")
        matches = [c for c in costumes if c.get("name") == name]
        if len(matches) == 1:
            costume = matches[0]
        else:
            index = record.get("index")
            costume = (
                costumes[index]
                if isinstance(index, int) and 0 <= index < len(costumes)
                else None
            )
        if costume is None:
            continue
        costume["rotationCenterX"] = rcx
        costume["rotationCenterY"] = rcy
        costume["bitmapResolution"] = record.get("bitmapResolution", 1)
        applied += 1
    if not applied:
        return
    tmp = sb3_path.with_name(sb3_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("project.json", json.dumps(project, ensure_ascii=False))
            for name, data in members:
                archive.writestr(name, data)
        os.replace(tmp, sb3_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# A `// ...` line is not a goboscript comment (only `#` is) and `#` comments are
# dropped at compile time, so neither can carry a comment into the compiled project.
# A line-leading `//` is therefore rewritten for the duration of the build into a
# call to a stub proc that carries its text, and the built sb3 then has that stub's
# definition removed — an undefined call is inert — with every call relabelled to
# free text the VM does not interpret. Net effect: the editor shows the comment and
# the project pays nothing beyond the caller's own block dispatch. Building twice
# from the same source is idempotent; the source itself is restored afterwards.
_COMMENT_RE = re.compile(r"^([ \t]*)// ?(.*)$")
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')


def _brace_delta(line: str) -> int:
    """Net ``{``/``}`` on a line, ignoring braces inside string literals."""
    code = _STRING_RE.sub('""', line)
    return code.count("{") - code.count("}")
_COMMENT_PROC = "__gobo_comment"
_COMMENT_PROCCODE = f"{_COMMENT_PROC} text: %s"
_COMMENT_LABEL = "// %s"
# The body mentions the argument so the compiler does not warn about it being
# unused; `log` is a plain statement with no visual side effect, and the whole
# definition is stripped from the built project anyway.
_COMMENT_STUB = f"\nproc {_COMMENT_PROC} text {{\n    log $text;\n}}\n"


def rewrite_comments(root: Path) -> list[tuple[Path, bytes]]:
    """Rewrite ``//`` comments into calls to the carrier stub.

    A ``//`` inside a body (brace depth > 0) is kept where it is, whatever its
    indentation. A flush-left ``//`` at the top level belongs to the proc/hat opened
    by the next line, so it moves *inside* that body and follows the definition in
    the compiled project; any other top-level flush-left ``//`` (above ``costumes``,
    a ``var``/``list`` declaration, or the end of file) is ignored, since there is no
    script for it to live in.

    Returns the changed files with their original bytes so the caller can restore
    them byte-for-byte after the build (building must not change the working tree).
    """
    backups: list[tuple[Path, bytes]] = []
    for path in sorted(root.glob("*.gs")):
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if "//" not in text:
            continue
        nl = "\r\n" if "\r\n" in text else "\n"
        out: list[str] = []
        changed = False
        needs_stub = False
        pending: list[str] = []
        depth = 0

        def flush_as_comment() -> None:
            nonlocal changed
            for awaiting in pending:
                out.append(f"# {awaiting}{nl}")
                changed = True
            pending.clear()

        def emit_carrier(indent: str, body: str) -> None:
            nonlocal changed, needs_stub
            out.append(f"{indent}{_COMMENT_PROC} {json.dumps(body.strip())};{nl}")
            changed = True
            needs_stub = True

        for line in text.splitlines(keepends=True):
            stripped = line.rstrip("\r\n")
            match = _COMMENT_RE.match(stripped)
            if match is not None:
                indent, body = match.groups()
                if depth > 0:
                    # Inside a body: it stays there, so it survives as a block.
                    flush_as_comment()
                    emit_carrier(indent, body)
                elif indent:
                    # Indented but outside any body: nothing to attach it to.
                    flush_as_comment()
                    out.append(f"# {body}{nl}")
                    changed = True
                else:
                    pending.append(body)
                continue
            if stripped.lstrip().startswith("#"):
                out.append(line)
                continue
            if not stripped.strip():
                flush_as_comment()
                out.append(line)
                continue
            if pending and depth == 0 and stripped.rstrip().endswith("{"):
                out.append(line)
                for awaiting in pending:
                    emit_carrier("    ", awaiting)
                pending.clear()
                depth += _brace_delta(stripped)
                continue
            flush_as_comment()
            out.append(line)
            depth = max(0, depth + _brace_delta(stripped))
        flush_as_comment()
        if not changed:
            continue
        rewritten = "".join(out)
        if needs_stub and f"proc {_COMMENT_PROC}" not in rewritten:
            rewritten += _COMMENT_STUB.replace("\n", nl)
        backups.append((path, raw))
        path.write_bytes(rewritten.encode("utf-8"))
    return backups


def restore_sources(backups: list[tuple[Path, bytes]]) -> None:
    for path, raw in backups:
        path.write_bytes(raw)


def inject_comments(sb3_path: Path) -> None:
    """Drop the carrier's definition and relabel its calls to ``// %s``."""
    try:
        with zipfile.ZipFile(sb3_path) as archive:
            project = json.loads(archive.read("project.json"))
            members = [
                (name, archive.read(name))
                for name in archive.namelist()
                if name != "project.json"
            ]
    except (OSError, ValueError, zipfile.BadZipFile):
        return
    changed = False
    for target in project["targets"]:
        blocks = target.get("blocks")
        if not isinstance(blocks, dict):
            continue
        for block_id, block in list(blocks.items()):
            if not isinstance(block, dict):
                continue
            if block.get("opcode") != "procedures_definition":
                continue
            proto_id = (block.get("inputs") or {}).get("custom_block", [None, None])[1]
            proto = blocks.get(proto_id)
            if not isinstance(proto, dict):
                continue
            if (proto.get("mutation") or {}).get("proccode") != _COMMENT_PROCCODE:
                continue
            stack = {block_id, proto_id}
            next_id = block.get("next")
            while next_id:
                stack.add(next_id)
                next_id = (blocks.get(next_id) or {}).get("next")
            for member_id in stack:
                blocks.pop(member_id, None)
            comments = target.get("comments")
            if isinstance(comments, dict):
                for comment_id, comment in list(comments.items()):
                    if isinstance(comment, dict) and comment.get("blockId") in stack:
                        del comments[comment_id]
            changed = True
        for block in blocks.values():
            if not isinstance(block, dict):
                continue
            if block.get("opcode") != "procedures_call":
                continue
            mutation = block.get("mutation")
            if isinstance(mutation, dict) and mutation.get("proccode") == _COMMENT_PROCCODE:
                mutation["proccode"] = _COMMENT_LABEL
                changed = True
    if not changed:
        return
    tmp = sb3_path.with_name(sb3_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("project.json", json.dumps(project, ensure_ascii=False))
            for name, data in members:
                archive.writestr(name, data)
        os.replace(tmp, sb3_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def build_project_at(root: Path, mode: str = "debug", *, goboscript: str,
                     mode_file: Path, prebuild, emit=print,
                     json_mode: bool = False) -> Path:
    mode = mode if mode in MODE_MACROS else "debug"
    prebuild(root, mode)
    # The mode include is a checked-in project file (default `tools/gsdev_mode.gs`,
    # overridable via config `modeInclude`). Rewrite it for this build and restore
    # it afterwards, so building never changes the working tree. A project that does
    # not opt in (no such file) builds unchanged.
    original = mode_file.read_text(encoding="utf-8") if mode_file.is_file() else None
    if original is not None:
        mode_file.write_text(MODE_MACROS[mode], encoding="utf-8")
        emit(f"building {root} ({mode})")
    else:
        emit(f"building {root}")
    comment_backups = rewrite_comments(root)
    sb3_path = root / (root.name + ".sb3")
    # Rely on cwd for the input (3.2.1 uses -i/--input, older builds a positional).
    command = [goboscript, "build", "-o", str(sb3_path)]
    try:
        if json_mode:
            # Keep stdout clean for the JSON report; decode diagnostics as UTF-8 so a
            # CP932/Japanese Windows locale cannot corrupt them.
            result = subprocess.run(
                command, cwd=str(root), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            print((result.stdout or "") + (result.stderr or ""), end="",
                  file=sys.stderr, flush=True)
        else:
            result = subprocess.run(command, cwd=str(root))
    finally:
        if original is not None:
            mode_file.write_text(original, encoding="utf-8")
        restore_sources(comment_backups)
    if result.returncode != 0:
        raise SystemExit(f"goboscript build failed (exit {result.returncode}).")
    inject_comments(sb3_path)
    inject_monitors(sb3_path, root)
    inject_costumes(sb3_path, root)
    inject_tw_blocks(sb3_path, root)
    return sb3_path
