"""Packaging checks for the host's audio support (no browser needed).

scratch-vm only plays sound once an AudioEngine is attached, so the host loads
scratch-audio + minilog + startaudiocontext + audio-context from the shared bundle
install and attaches it before loading a project (see tools/scratchhost/audio.js).
These checks catch the wiring regressions that are easy to miss without a browser:
a missing bundle entry, host.html not loading audio.js, audio.js dropping out of the
adopted-runtime file list, or the package.json no longer pulling the deps.

Run with the harness Python:

    python tools/test_audio_wiring.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

AUDIO_PACKAGES = {
    "scratch-audio": "dist.js",
    "minilog": "dist/minilog.js",
    "startaudiocontext": "StartAudioContext.js",
    "audio-context": "index.js",
}


def main() -> int:
    import bootstrap

    failures: list[str] = []
    for package, entry in AUDIO_PACKAGES.items():
        if bootstrap.ENTRY_FILES.get(package) != entry:
            failures.append(f"ENTRY_FILES is missing {package} -> {entry}")
        if package not in bootstrap.PACKAGES:
            failures.append(f"PACKAGES is missing {package}")

    host_html = (REPO / "tools" / "scratchhost" / "host.html").read_text(encoding="utf-8")
    if "./audio.js" not in host_html:
        failures.append("host.html does not load ./audio.js")
    if "/scratch-audio/dist.js" not in host_html:
        failures.append("host.html does not probe the scratch-audio bundle")

    host_js = (REPO / "tools" / "scratchhost" / "host.js").read_text(encoding="utf-8")
    for needle in ("__gsdevAudio.attach", "case 'audioplayback'", "case 'audiostate'",
                   "window.__host.audioCapture", "wavFromChannels"):
        if needle not in host_js:
            failures.append(f"host.js is missing {needle!r}")

    gsdev_js = (REPO / "tools" / "gsdev.py").read_text(encoding="utf-8")
    if "def cmd_audio" not in gsdev_js:
        failures.append("gsdev.py has no `audio` verb implementation")
    if '"audio", help="measure peak/rms' not in gsdev_js:
        failures.append("gsdev.py has no `audio` subparser")

    # The bridge must expose the same verb: shim function + CLI implementation.
    shim = (REPO / "tools" / "gsdevlib" / "bridge_shim.js").read_text(encoding="utf-8")
    for needle in ("const audioCapture =", "audioCapture,", "resetAudio",
                   "case 'audioplayback'"):
        if needle not in shim:
            failures.append(f"bridge_shim.js is missing {needle!r}")
    gsbridge_js = (REPO / "tools" / "gsbridge.py").read_text(encoding="utf-8")
    if "def cmd_audio" not in gsbridge_js:
        failures.append("gsbridge.py has no `audio` verb implementation")
    if "__bridge.audioCapture" not in gsbridge_js:
        failures.append("gsbridge.py does not call __bridge.audioCapture")

    runtime_files = (REPO / "tools" / "runtime-files.txt").read_text(encoding="utf-8")
    if "tools/scratchhost/audio.js" not in runtime_files:
        failures.append("runtime-files.txt does not ship tools/scratchhost/audio.js")

    package_json = json.loads(
        (REPO / "tools" / "scratchhost" / "package.json").read_text(encoding="utf-8"))
    for package, version in AUDIO_PACKAGES.items():
        if package not in (package_json.get("dependencies") or {}):
            failures.append(f"scratchhost/package.json does not depend on {package}")

    # The bundle install itself is machine state; report what is there without failing
    # on a checkout that has not run setup yet.
    installed = [pkg for pkg, entry in AUDIO_PACKAGES.items()
                 if (bootstrap.bundles_dir() / pkg / entry).is_file()]
    node = [pkg for pkg, entry in AUDIO_PACKAGES.items()
            if (bootstrap.node_modules_dir() / pkg / entry).is_file()]
    if len(installed) == len(AUDIO_PACKAGES):
        print("bundles: vendored audio packages present")
        if not bootstrap.host_audio_present():
            failures.append("host_audio_present() is false with a complete vendor install")
    elif len(node) == len(AUDIO_PACKAGES):
        print("bundles: node_modules audio packages present (vendor missing)")
        if not bootstrap.host_audio_present():
            failures.append("host_audio_present() is false with a complete npm install")
    elif not installed and not node:
        print("bundles: no audio packages installed (run `gsdev setup --only vendor`)")
    else:
        failures.append(f"audio install incomplete: vendor={installed} node_modules={node}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("ok: host audio wiring is complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
