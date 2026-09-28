"""Zero-admin setup for gobo-agent.

Downloads the *prebuilt* tools the dev loop needs into user-writable locations, so
a Windows (or macOS/Linux) machine with no admin rights never needs Rust, MSVC, an
MSYS2 toolchain, or Node/npm:

  * goboscript  - the release binary from GitHub, downloaded only when the user
    does not already have one (GSDEV_GOBOSCRIPT or PATH wins; no Rust/MSVC/MSYS2)
  * host bundles - the four pinned @scratch/* browser bundles from the npm registry
    (no Node/npm; `dist/web` is already a self-contained browser build)

Everything lands under ``GSDEV_TOOLS`` (default ``<repo>/.tools``) and
``tools/scratchhost/vendor/``; both are gitignored. The AGPL-3.0 @scratch/*
packages are downloaded at setup time and never committed, exactly as
``npm install`` would.

Stdlib only. Run directly or via ``python tools/gsdev.py setup``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent


def _configure_stdio() -> None:
    """Print as UTF-8 and never crash on an un-encodable path.

    A Japanese install directory printed through a CP1252 console used to raise
    UnicodeEncodeError partway through setup; backslashreplace keeps it going even
    if the terminal cannot render the characters.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            pass


_configure_stdio()

GOBOSCRIPT_VERSION = "3.2.1"
# Pinned to match tools/scratchhost/package.json.
PACKAGES = {
    "@scratch/scratch-vm": "15.1.1",
    "@scratch/scratch-render": "15.1.1",
    "@scratch/scratch-storage": "15.1.1",
    "@scratch/scratch-svg-renderer": "15.1.1",
}
# The host page loads these; used both as the "is it installed" probe and to keep
# the node_modules layout and the vendored layout in sync (host.html prefers
# vendor, falls back to node_modules).
ENTRY_FILES = {
    "@scratch/scratch-vm": "dist/web/scratch-vm.js",
    "@scratch/scratch-render": "dist/web/scratch-render.min.js",
    "@scratch/scratch-storage": "dist/web/scratch-storage.min.js",
    "@scratch/scratch-svg-renderer": "dist/web/scratch-svg-renderer.js",
}

USER_AGENT = f"gobo-agent-setup/{GOBOSCRIPT_VERSION}"
TIMEOUT = 120.0


def tools_root() -> Path:
    override = os.environ.get("GSDEV_TOOLS")
    return Path(override).expanduser().resolve() if override else (REPO_ROOT / ".tools").resolve()


def goboscript_dir() -> Path:
    return tools_root() / "goboscript"


def goboscript_binary_name() -> str:
    return "goboscript.exe" if os.name == "nt" else "goboscript"


def bundled_goboscript() -> Path | None:
    path = goboscript_dir() / goboscript_binary_name()
    return path if path.exists() else None


def _probe_goboscript(binary) -> str | None:
    """Version line if the binary runs, else None (missing / non-zero / not exec)."""
    import subprocess

    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0].strip() if output else ""


def system_goboscript() -> str | None:
    """A goboscript the user already has: GSDEV_GOBOSCRIPT, else one on PATH.

    The PATH candidate must actually run, so a broken shim does not shadow the
    bundled fallback.
    """
    override = os.environ.get("GSDEV_GOBOSCRIPT")
    if override:
        return override if Path(override).exists() else None
    candidate = shutil.which("goboscript")
    if candidate and _probe_goboscript(candidate) is not None:
        return candidate
    return None


def resolve_goboscript() -> str | None:
    """The compiler to use: the user's own install first, then the bundled one.

    A user-provided goboscript (``GSDEV_GOBOSCRIPT`` or on ``PATH``) always wins;
    the pinned bundle downloaded by ``setup`` is only a fallback for machines
    that do not have one.
    """
    system = system_goboscript()
    if system is not None:
        return system
    bundled = bundled_goboscript()
    return str(bundled) if bundled is not None else None


def vendor_dir() -> Path:
    return TOOLS_DIR / "scratchhost" / "vendor"


def node_modules_dir() -> Path:
    return TOOLS_DIR / "scratchhost" / "node_modules"


def _entry_ok(base: Path) -> bool:
    return all((base / pkg / rel).exists() for pkg, rel in ENTRY_FILES.items())


def vendor_present() -> bool:
    return _entry_ok(vendor_dir())


def node_modules_present() -> bool:
    return _entry_ok(node_modules_dir())


def host_bundles_present() -> bool:
    """True when the host page has a loadable set of @scratch/* bundles."""
    return vendor_present() or node_modules_present()


class SetupError(RuntimeError):
    pass


def _log(message: str) -> None:
    print(f"[setup] {message}", flush=True)


def _download(url: str, label: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            total = int(response.headers.get("Content-Length") or 0)
            chunks: list[bytes] = []
            seen = 0
            started = time.monotonic()
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                chunks.append(block)
                seen += len(block)
                if total and sys.stderr.isatty():
                    percent = seen * 100 // total
                    print(f"\r[setup] {label} ... {percent}%", end="", file=sys.stderr, flush=True)
            if total and sys.stderr.isatty():
                print("", file=sys.stderr, flush=True)
            elapsed = time.monotonic() - started
            data = b"".join(chunks)
            _log(f"{label}: {len(data) / 1e6:.1f} MB in {elapsed:.1f}s")
            return data
    except urllib.error.URLError as error:
        raise SetupError(f"could not download {label} ({url}): {error}") from error


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_integrity(data: bytes, integrity: str, label: str) -> None:
    algorithm, _, encoded = integrity.partition("-")
    if not encoded or algorithm not in {"sha512", "sha256", "sha1"}:
        raise SetupError(f"unrecognised integrity for {label}: {integrity!r}")
    digest = hashlib.new(algorithm, data).digest()
    if not hmac.compare_digest(base64.b64encode(digest).decode("ascii"), encoded):
        raise SetupError(f"integrity mismatch for {label}")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _latest_checksums(offline: bool) -> dict[str, str]:
    if offline:
        raise SetupError("cannot verify the download without the release checksums (offline)")
    url = (
        "https://github.com/aspizu/goboscript/releases/download/"
        f"v{GOBOSCRIPT_VERSION}/goboscript_{GOBOSCRIPT_VERSION}_checksums.txt"
    )
    text = _download(url, "goboscript checksums").decode("utf-8", "replace")
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            checksums[parts[1].lstrip("*")] = parts[0].lower()
    return checksums


def running_on_windows_arm() -> bool:
    return platform.system() == "Windows" and platform.machine().lower() in ("arm64", "aarch64")


def _goboscript_asset(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    """Return (asset filename, archive kind) for a given OS/arch.

    ``system``/``machine`` default to the running platform and are injectable so
    the mapping can be unit-tested for other platforms.
    """
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system == "Windows":
        # goboscript publishes no native Windows arm64 build; the x64 binary runs
        # under Windows 11's x64 emulation (see install_goboscript).
        return "goboscript_Windows_x86_64.zip", "zip"
    if system == "Darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
        return f"goboscript_Darwin_{arch}.tar.gz", "tar"
    if system == "Linux":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
        return f"goboscript_Linux_{arch}.tar.gz", "tar"
    raise SetupError(f"unsupported platform: {system} {machine}")


def install_goboscript(force: bool = False, offline: bool = False) -> Path:
    target = goboscript_dir() / goboscript_binary_name()
    system = system_goboscript()
    if system is not None and not force:
        _log(f"goboscript already available: {system} (skipping download)")
        return Path(system)
    if bundled_goboscript() is not None and not force:
        _log(f"goboscript already installed: {target}")
        return target
    if force and system is not None:
        _log(
            f"note: {system} is still preferred; set GSDEV_GOBOSCRIPT to choose a "
            "specific binary"
        )

    if offline:
        raise SetupError(
            f"goboscript is not installed and --offline was given; expected {target}"
        )
    checksums = _latest_checksums(offline)
    asset, kind = _goboscript_asset()
    if running_on_windows_arm():
        # Prefer a native arm64 build if a future release ever publishes one,
        # otherwise fall back to the x64 build under Windows 11 x64 emulation.
        if "goboscript_Windows_arm64.zip" in checksums:
            asset, kind = "goboscript_Windows_arm64.zip", "zip"
        else:
            _log(
                "note: no native Windows arm64 goboscript is published; using the "
                "x64 build (Windows 11 runs it under x64 emulation)"
            )
    expected = checksums.get(asset)
    if expected is None:
        raise SetupError(f"release {GOBOSCRIPT_VERSION} has no asset named {asset}")
    url = (
        "https://github.com/aspizu/goboscript/releases/download/"
        f"v{GOBOSCRIPT_VERSION}/{asset}"
    )

    data = _download(url, f"goboscript {GOBOSCRIPT_VERSION}")
    actual = _sha256(data)
    if actual != expected:
        raise SetupError(f"checksum mismatch for {asset}: expected {expected}, got {actual}")

    target = _extract_goboscript(data, kind)
    _log(f"goboscript {GOBOSCRIPT_VERSION} -> {target}")
    return target


def _clear_quarantine(path: Path) -> None:
    """Best-effort Gatekeeper quarantine removal for a downloaded binary.

    Files fetched by a browser or copied via Finder carry ``com.apple.quarantine``
    and macOS may refuse to execute them ("cannot be verified"). ``xattr`` is
    user-scoped, so this needs no admin rights, and failure is harmless: downloads
    made by urllib are normally not quarantined in the first place.
    """
    try:
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(path)],
                       check=False, capture_output=True)
    except OSError:
        pass


def _extract_goboscript(data: bytes, kind: str) -> Path:
    import io

    buffer = io.BytesIO(data)
    name = goboscript_binary_name()
    members: dict[str, bytes]
    if kind == "zip":
        with zipfile.ZipFile(buffer) as archive:
            members = {Path(item.filename).name: archive.read(item) for item in archive.infolist()}
    else:
        with tarfile.open(fileobj=buffer, mode="r:gz") as archive:
            members = {
                Path(item.name).name: archive.extractfile(item).read()
                for item in archive.getmembers()
                if item.isfile()
            }
    if name not in members:
        raise SetupError(f"archive has no {name} (found: {', '.join(sorted(members)) or 'nothing'})")

    _ensure_dir(goboscript_dir())
    target = goboscript_dir() / name
    target.write_bytes(members[name])
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if platform.system() == "Darwin":
            _clear_quarantine(target)
    return target


def _registry_meta(package: str, version: str) -> dict:
    url = f"https://registry.npmjs.org/{package}/{version}"
    data = _download(url, f"{package} metadata")
    try:
        return json.loads(data)
    except json.JSONDecodeError as error:
        raise SetupError(f"bad registry metadata for {package}: {error}") from error


def _extract_dist_web(data: bytes, package: str) -> int:
    import io

    destination = vendor_dir() / package
    if destination.exists():
        shutil.rmtree(destination)
    prefix = "package/"
    base = destination
    root = base.resolve()
    written = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            name = member.name
            if not name.startswith(prefix):
                continue
            relative = name[len(prefix):]
            if not relative.startswith("dist/web/"):
                continue
            # Confine extraction to the package dir: a member such as
            # `package/dist/web/../../../evil.js` satisfies the prefixes above but
            # would otherwise escape vendor_dir().
            out = (base / relative).resolve()
            try:
                out.relative_to(root)
            except ValueError:
                raise SetupError(
                    f"{package} tarball member escapes the vendor directory: {name}"
                )
            out.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is not None:
                out.write_bytes(extracted.read())
                written += 1
    if not written:
        raise SetupError(f"{package} tarball has no dist/web files")
    return written


def _versions_path() -> Path:
    return vendor_dir() / ".versions.json"


def install_vendor(force: bool = False, offline: bool = False) -> None:
    if vendor_present() and not force:
        try:
            recorded = json.loads(_versions_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recorded = {}
        if recorded.get("packages") == PACKAGES:
            _log(f"host bundles already installed: {vendor_dir()}")
            return
    if offline:
        raise SetupError(
            "host bundles are not installed and --offline was given; expected "
            f"{vendor_dir()}"
        )

    for package, version in PACKAGES.items():
        meta = _registry_meta(package, version)
        dist = meta.get("dist") or {}
        tarball = dist.get("tarball")
        if not tarball:
            raise SetupError(f"registry metadata for {package} has no dist.tarball")
        data = _download(tarball, f"{package} {version}")
        integrity = dist.get("integrity")
        if not integrity:
            # Fail closed: an unsigned tarball from the network must not be extracted.
            raise SetupError(
                f"registry metadata for {package} {version} has no dist.integrity; "
                "refusing to extract an unverified tarball"
            )
        _check_integrity(data, integrity, f"{package} {version}")
        count = _extract_dist_web(data, package)
        _log(f"{package} {version}: {count} dist/web files")

    _ensure_dir(vendor_dir())
    _versions_path().write_text(
        json.dumps({"packages": PACKAGES}, indent=2) + "\n", encoding="utf-8"
    )
    _log(f"host bundles -> {vendor_dir()}")


def _goboscript_version(binary) -> str:
    return _probe_goboscript(binary) or ""


def status() -> dict:
    resolved = resolve_goboscript()
    bundled = bundled_goboscript()
    resolved_is_bundled = bool(
        resolved and bundled and Path(resolved).resolve() == bundled.resolve()
    )
    return {
        "toolsRoot": str(tools_root()),
        "goboscript": resolved,
        "goboscriptVersion": _goboscript_version(resolved) if resolved else None,
        "goboscriptSource": (
            "bundled" if resolved_is_bundled else ("system" if resolved else None)
        ),
        "vendor": str(vendor_dir()) if vendor_present() else None,
        "nodeModules": str(node_modules_dir()) if node_modules_present() else None,
    }


def run_setup(
    only: str | None = None,
    force: bool = False,
    offline: bool = False,
) -> int:
    try:
        if only in (None, "goboscript"):
            install_goboscript(force=force, offline=offline)
        if only in (None, "vendor"):
            install_vendor(force=force, offline=offline)
    except SetupError as error:
        _log(f"FAILED: {error}")
        return 1

    info = status()
    source = info.get("goboscriptSource")
    detail = info["goboscriptVersion"] or info["goboscript"] or "missing"
    _log(f"goboscript: {detail}{f' ({source})' if source else ''}")
    _log(f"host bundles: {info['vendor'] or info['nodeModules'] or 'missing'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download the prebuilt gobo-agent tools (no admin).")
    parser.add_argument("--only", choices=["goboscript", "vendor"], help="install one piece")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument("--offline", action="store_true", help="never touch the network")
    parser.add_argument("--check", action="store_true", help="print status and exit")
    args = parser.parse_args(argv)
    if args.check:
        print(json.dumps(status(), indent=2))
        return 0
    return run_setup(only=args.only, force=args.force, offline=args.offline)


if __name__ == "__main__":
    raise SystemExit(main())
