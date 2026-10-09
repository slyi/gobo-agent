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
# The newest release (3.2.1) carries the goboscript#158 negative-literal
# regression, which is fixed only on main and has not been released. gobo-agent
# therefore hosts its own pinned prebuilt (see
# .github/workflows/goboscript-prebuilt.yml). When the tag below is set it is
# preferred over the upstream v{GOBOSCRIPT_VERSION} assets; clear it to fall back.
GOBOSCRIPT_PREBUILT_REPO = "slyi/gobo-agent"
GOBOSCRIPT_PREBUILT_TAG = "goboscript-87014c61"
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

# --- sb2gs (Scratch -> goboscript importer), installed non-optionally ---------
#
# sb2gs is pure Python but needs >=3.14 and four third-party packages. The
# embeddable Python has no pip, so instead of installing a package manager we
# fetch pinned wheels from PyPI, verify their SHA256, and unpack them (wheels are
# zips) into .tools/sb2gs/site-packages alongside the pinned sb2gs source. No pip,
# no uv, no admin. Pillow is the only compiled dependency, so it is the only
# platform-specific wheel; everything else is ``py3-none-any``.
SB2GS_COMMIT = "8cb5dabdb5ffaa42209643469d1a53722ac762aa"
SB2GS_ZIP_URL = f"https://github.com/aspizu/sb2gs/archive/{SB2GS_COMMIT}.zip"
SB2GS_ZIP_SHA256 = "53a6e562244fbeae6cae4bf2f1c17b8c3ff9366e4c6a97769525db3084532d14"

# name -> (filename, url, sha256); all universal (py3-none-any) wheels.
SB2GS_WHEELS = {
    "httpx": ("httpx-0.28.1-py3-none-any.whl",
              "https://files.pythonhosted.org/packages/2a/39/e50c7c3a983047577ee07d2a9e53faf5a69493943ec3f6a384bdc792deb2/httpx-0.28.1-py3-none-any.whl",
              "d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad"),
    "httpcore": ("httpcore-1.0.9-py3-none-any.whl",
                 "https://files.pythonhosted.org/packages/7e/f5/f66802a942d491edb555dd61e3a9961140fd64c90bce1eafd741609d334d/httpcore-1.0.9-py3-none-any.whl",
                 "2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55"),
    "h11": ("h11-0.16.0-py3-none-any.whl",
            "https://files.pythonhosted.org/packages/04/4b/29cac41a4d98d144bf5f6d33995617b185d14b22401f75ca86f384e87ff1/h11-0.16.0-py3-none-any.whl",
            "63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86"),
    "idna": ("idna-3.20-py3-none-any.whl",
             "https://files.pythonhosted.org/packages/58/a2/bb081bab032533a855d44de1d56f8e8426114ff1ba5d1f07a438a0a654f8/idna-3.20-py3-none-any.whl",
             "ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c"),
    "certifi": ("certifi-2026.7.22-py3-none-any.whl",
                "https://files.pythonhosted.org/packages/0b/a7/71ac2cff56fec219ed242bb11b8efb69fcc4bec75db06fb7bfe35de520e6/certifi-2026.7.22-py3-none-any.whl",
                "62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775"),
    "anyio": ("anyio-4.15.1-py3-none-any.whl",
              "https://files.pythonhosted.org/packages/12/b8/4bd346e22b28902df4d651910f5242c28d84e4a5c2435ca5c3f797ed7e2e/anyio-4.15.1-py3-none-any.whl",
              "6152fdbbf9a77fdec97731721bebf7c4c44f7c29b424b0065826173efc7ed101"),
    "typing_extensions": ("typing_extensions-4.16.0-py3-none-any.whl",
                          "https://files.pythonhosted.org/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/typing_extensions-4.16.0-py3-none-any.whl",
                          "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8"),
    "rich": ("rich-15.0.0-py3-none-any.whl",
             "https://files.pythonhosted.org/packages/82/3b/64d4899d73f91ba49a8c18a8ff3f0ea8f1c1d75481760df8c68ef5235bf5/rich-15.0.0-py3-none-any.whl",
             "33bd4ef74232fb73fe9279a257718407f169c09b78a87ad3d296f548e27de0bb"),
    "pygments": ("pygments-2.21.0-py3-none-any.whl",
                 "https://files.pythonhosted.org/packages/71/46/17f022dd3e953bf20a04a028a21ec746d942f8d2af30fa0f124fa0e6a684/pygments-2.21.0-py3-none-any.whl",
                 "2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9"),
    "markdown_it_py": ("markdown_it_py-4.2.0-py3-none-any.whl",
                       "https://files.pythonhosted.org/packages/b3/81/4da04ced5a082363ecfa159c010d200ecbd959ae410c10c0264a38cac0f5/markdown_it_py-4.2.0-py3-none-any.whl",
                       "9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a"),
    "mdurl": ("mdurl-0.1.2-py3-none-any.whl",
              "https://files.pythonhosted.org/packages/b3/38/89ba8ad64ae25be8de66a6d463314cf1eb366222074cfda9ee839c56a4b4/mdurl-0.1.2-py3-none-any.whl",
              "84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8"),
    "tomlkit": ("tomlkit-0.15.1-py3-none-any.whl",
                "https://files.pythonhosted.org/packages/13/bc/8c13eb66537dce1d2bd3a57132902f38d0e7f5bb46fa9f4daed9fe9d76ee/tomlkit-0.15.1-py3-none-any.whl",
                "177a05aece5a8ca5266fd3c448abb47b8d352f09d477d3ca8332db4d89b24304"),
}

# pillow is compiled; one cp314 wheel per platform.
SB2GS_PILLOW = {
    "win_amd64": ("pillow-12.3.0-cp314-cp314-win_amd64.whl",
                  "https://files.pythonhosted.org/packages/f1/e0/492879f69d94f91f60fc8cd05ba03650e9520afebb2fb7aa12777d7c7f38/pillow-12.3.0-cp314-cp314-win_amd64.whl",
                  "fdafc9cce40277e0f7a0feabce0ee50dd2fa1800f3b38015e51296b5e814048d"),
    "win_arm64": ("pillow-12.3.0-cp314-cp314-win_arm64.whl",
                  "https://files.pythonhosted.org/packages/c9/ac/6b11f2875f1c2ac040d84e1bbf9cf22a88038f901ca1037898b280b38365/pillow-12.3.0-cp314-cp314-win_arm64.whl",
                  "e91206ee562682b51b98ef4b26a6ef48fd84e15fd4c4bc5ec768eb641d206838"),
    "macosx_arm64": ("pillow-12.3.0-cp314-cp314-macosx_11_0_arm64.whl",
                     "https://files.pythonhosted.org/packages/c7/da/32c752228ae345f489e3a42499d817b6c3996da7e8a3bc7a04fc806b243b/pillow-12.3.0-cp314-cp314-macosx_11_0_arm64.whl",
                     "e158cb00350dc278f3b91551101aa7d12415a66ebf2c91d8d5ac14e56ddd3ad0"),
    "macosx_x86_64": ("pillow-12.3.0-cp314-cp314-macosx_10_15_x86_64.whl",
                      "https://files.pythonhosted.org/packages/85/e2/73c77d218410b14f5f2d565e8a998d5317b7b9c75368d29985139f7a46f0/pillow-12.3.0-cp314-cp314-macosx_10_15_x86_64.whl",
                      "ba54cfebe86920a559a7c4d6b9050791c20513650a1952ebe3368c7dc70306f8"),
    "manylinux_x86_64": ("pillow-12.3.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                         "https://files.pythonhosted.org/packages/5c/44/c85361f65dbe00eea8576ee467c768d25129989efb76e94f205e9ca9bb46/pillow-12.3.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                         "251bf95b67017e27b13d82f5b326234ca62d70f9cf4c2b9032de2358a3b12c7b"),
    "manylinux_aarch64": ("pillow-12.3.0-cp314-cp314-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl",
                          "https://files.pythonhosted.org/packages/b1/9d/8b2c807dbef61a5197c047afe99823787eb66f63daf9fb2432f91d6f0462/pillow-12.3.0-cp314-cp314-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl",
                          "e9aeb04d6aef139de265b29683e119b638208f88cf73cdd1658aa07221165321"),
}


def default_home() -> Path:
    """Shared per-user install root so tools are downloaded once per machine.

    ``GSDEV_HOME`` overrides it; otherwise a per-user cache
    (``%LOCALAPPDATA%\\gobo-agent`` on Windows, ``$XDG_CACHE_HOME/gobo-agent`` or
    ``~/.cache/gobo-agent`` on POSIX). ``GSDEV_TOOLS`` still overrides the whole
    tools root (CI uses it to stay repo-local).
    """
    override = os.environ.get("GSDEV_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return (Path(base) / "gobo-agent").resolve()


def tools_root() -> Path:
    override = os.environ.get("GSDEV_TOOLS")
    return Path(override).expanduser().resolve() if override else default_home()


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


def bundles_dir() -> Path:
    # Shared across projects (tools root), so the ~35 MB @scratch bundles are
    # downloaded once per machine and mounted at /vendor/ by the host server.
    return tools_root() / "bundles"


def node_modules_dir() -> Path:
    return TOOLS_DIR / "scratchhost" / "node_modules"


def _entry_ok(base: Path) -> bool:
    return all((base / pkg / rel).exists() for pkg, rel in ENTRY_FILES.items())


def vendor_present() -> bool:
    return _entry_ok(bundles_dir())


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


def _goboscript_release_label() -> str:
    return GOBOSCRIPT_PREBUILT_TAG or f"v{GOBOSCRIPT_VERSION}"


def _goboscript_release_base() -> str:
    """Release base URL: gobo-agent's pinned prebuilt, else upstream's release."""
    if GOBOSCRIPT_PREBUILT_TAG:
        return (
            f"https://github.com/{GOBOSCRIPT_PREBUILT_REPO}/releases/download/"
            f"{GOBOSCRIPT_PREBUILT_TAG}"
        )
    return (
        "https://github.com/aspizu/goboscript/releases/download/"
        f"v{GOBOSCRIPT_VERSION}"
    )


def _goboscript_checksums_name() -> str:
    return (
        "goboscript_checksums.txt"
        if GOBOSCRIPT_PREBUILT_TAG
        else f"goboscript_{GOBOSCRIPT_VERSION}_checksums.txt"
    )


def _latest_checksums(offline: bool) -> dict[str, str]:
    if offline:
        raise SetupError("cannot verify the download without the release checksums (offline)")
    url = f"{_goboscript_release_base()}/{_goboscript_checksums_name()}"
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
    label = _goboscript_release_label()
    expected = checksums.get(asset)
    if expected is None:
        raise SetupError(f"release {label} has no asset named {asset}")
    url = f"{_goboscript_release_base()}/{asset}"

    data = _download(url, f"goboscript {label}")
    actual = _sha256(data)
    if actual != expected:
        raise SetupError(f"checksum mismatch for {asset}: expected {expected}, got {actual}")

    target = _extract_goboscript(data, kind)
    _log(f"goboscript {label} -> {target}")
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

    destination = bundles_dir() / package
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
            # would otherwise escape bundles_dir().
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
    return bundles_dir() / ".versions.json"


def install_vendor(force: bool = False, offline: bool = False) -> None:
    if vendor_present() and not force:
        try:
            recorded = json.loads(_versions_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recorded = {}
        if recorded.get("packages") == PACKAGES:
            _log(f"host bundles already installed: {bundles_dir()}")
            return
    if offline:
        raise SetupError(
            "host bundles are not installed and --offline was given; expected "
            f"{bundles_dir()}"
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

    _ensure_dir(bundles_dir())
    _versions_path().write_text(
        json.dumps({"packages": PACKAGES}, indent=2) + "\n", encoding="utf-8"
    )
    _log(f"host bundles -> {bundles_dir()}")


def sb2gs_root() -> Path:
    return tools_root() / "sb2gs"


def sb2gs_source_dir() -> Path:
    return sb2gs_root() / "source"


def sb2gs_site_dir() -> Path:
    return sb2gs_root() / "site-packages"


def sb2gs_run_py() -> Path:
    return sb2gs_root() / "run.py"


def sb2gs_marker() -> Path:
    return sb2gs_root() / ".installed.json"


def system_sb2gs() -> str | None:
    """A user-installed sb2gs to prefer over our bundled copy.

    ``GSDEV_SB2GS`` (a path to the command) or an ``sb2gs`` on PATH. When present
    we run the user's own install instead of downloading ours.
    """
    override = os.environ.get("GSDEV_SB2GS")
    if override:
        return override if Path(override).expanduser().exists() else None
    return shutil.which("sb2gs")


def sb2gs_present() -> bool:
    # Source + runner + marker are always written; the wheels are optional when
    # the interpreter already provides sb2gs's packages (see install_sb2gs).
    return (
        (sb2gs_source_dir() / "sb2gs" / "__init__.py").is_file()
        and sb2gs_run_py().is_file()
        and sb2gs_marker().is_file()
    )


def _sb2gs_pillow_key() -> str | None:
    machine = platform.machine().lower()
    if os.name == "nt":
        return "win_arm64" if machine in ("arm64", "aarch64") else "win_amd64"
    if sys.platform == "darwin":
        return "macosx_arm64" if machine in ("arm64", "aarch64") else "macosx_x86_64"
    if sys.platform.startswith("linux"):
        return "manylinux_aarch64" if machine in ("aarch64", "arm64") else "manylinux_x86_64"
    return None


def _sb2gs_system_packages_ok(source: Path) -> bool:
    """True when the running interpreter can import sb2gs with no bundled wheels.

    Tries the pinned sb2gs source with only its directory on sys.path; a full
    Python that already has httpx/pillow/rich/tomlkit (and their deps) imports it,
    so the wheel download is skipped.
    """
    code = "import sys; sys.path.insert(0, sys.argv[1]); import sb2gs"
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(source)],
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# Inserted before the pinned sb2gs source and wheels on sys.path, then delegate to
# sb2gs's own ``main`` (there is no ``__main__.py`` upstream).
SB2GS_RUNNER = """\
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here / "source"))
sys.path.insert(0, str(_here / "site-packages"))

from sb2gs import main

raise SystemExit(main())
"""


# gobo-agent patch for sb2gs's `--id` downloader. Upstream fetches every asset
# sequentially with httpx's short default timeout and no retry, so large projects
# (hundreds of assets) are slow and abort on a single hiccup. This patch:
#   - reuses one pooled httpx.Client (one TLS handshake per host, not per asset);
#   - downloads concurrently (GSDEV_SB2GS_WORKERS, default 16);
#   - caches assets by md5ext on disk (content-addressed; GSDEV_SB2GS_CACHE,
#     disable with GSDEV_SB2GS_NO_CACHE), so repeat imports are offline;
#   - retries with backoff, accepts the project-data endpoint's zip-wrapped
#     response as well as raw JSON, and normalizes the sb3 (fills sb2gs-required
#     defaults and a missing md5ext). Same public signature.
SB2GS_DOWNLOADER_PATCH = '''\
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from zipfile import ZipFile

import httpx

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_RETRIES = 4
_WORKERS = max(1, int(os.environ.get("GSDEV_SB2GS_WORKERS", "16") or "16"))
_client = httpx.Client(
    timeout=_TIMEOUT,
    follow_redirects=True,
    limits=httpx.Limits(
        max_connections=max(_WORKERS * 2, 32),
        max_keepalive_connections=max(_WORKERS, 16),
    ),
)


def _asset_cache():
    if os.environ.get("GSDEV_SB2GS_NO_CACHE"):
        return None
    override = os.environ.get("GSDEV_SB2GS_CACHE")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "gobo-agent" / "asset-cache"


def _get(url: str):
    last = None
    for attempt in range(_RETRIES):
        try:
            response = _client.get(url)
            response.raise_for_status()
            return response
        except Exception as error:  # retry any transient failure
            last = error
            time.sleep(min(2 ** attempt, 8))
    raise last


def _md5ext(asset: dict) -> str:
    # Some projects omit md5ext (newer saves); it is assetId + "." + dataFormat.
    return asset.get("md5ext") or f"{asset['assetId']}.{asset['dataFormat']}"


def _asset_bytes(md5ext: str) -> bytes:
    cache = _asset_cache()
    path = (cache / md5ext) if cache else None
    if path is not None and path.is_file():
        return path.read_bytes()
    data = _get(f"https://assets.scratch.mit.edu/internalapi/asset/{md5ext}/get/").content
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".part")
            part.write_bytes(data)
            os.replace(part, path)
        except OSError:
            pass
    return data


def _target_defaults() -> dict:
    # Newer saves omit empty/default target keys that sb2gs dereferences directly
    # (lists, comments, layerOrder, volume, ...). Fill the sb3 defaults.
    return {
        "variables": {}, "lists": {}, "broadcasts": {}, "blocks": {}, "comments": {},
        "costumes": [], "sounds": [], "currentCostume": 0, "volume": 100,
        "layerOrder": 0, "visible": True, "x": 0, "y": 0, "size": 100,
        "direction": 90, "draggable": False, "rotationStyle": "all around",
    }


def download_sb3(id: str, outfile: str | Path) -> None:
    token = _get(f"https://api.scratch.mit.edu/projects/{id}").json()["project_token"]
    response = _get(f"https://projects.scratch.mit.edu/{id}?token={token}")
    # The data endpoint returns raw JSON for most projects but a small zip whose
    # only entry is project.json for others (e.g. newer/svg projects); handle both.
    if response.content[:2] == b"PK":
        with ZipFile(io.BytesIO(response.content)) as bundle:
            data = json.loads(bundle.read("project.json"))
    else:
        data = response.json()
    for target in data["targets"]:
        for key, value in _target_defaults().items():
            target.setdefault(key, value)
        for asset in (*target["costumes"], *target["sounds"]):
            # sb2gs requires md5ext even when the saved project omitted it.
            asset.setdefault("md5ext", _md5ext(asset))
        for block in target["blocks"].values():
            if isinstance(block, dict):
                block.setdefault("next", None)
                block.setdefault("parent", None)
                block.setdefault("inputs", {})
                block.setdefault("fields", {})
                block.setdefault("shadow", False)
                block.setdefault("topLevel", False)
    assets = {
        *(
            costume["md5ext"]
            for target in data["targets"]
            for costume in target["costumes"]
        ),
        *(
            sound["md5ext"]
            for target in data["targets"]
            for sound in target["sounds"]
        ),
    }
    cache = _asset_cache()
    cached = sum(1 for md5ext in assets if cache and (cache / md5ext).is_file())
    label = f" ({cached} cached)" if cached else ""
    print(f"downloading {len(assets)} asset(s){label}...", flush=True)
    with ZipFile(outfile, "w") as archive:
        archive.writestr("project.json", json.dumps(data))
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            pending = {pool.submit(_asset_bytes, md5ext): md5ext for md5ext in assets}
            for done, future in enumerate(as_completed(pending), 1):
                archive.writestr(pending[future], future.result())
                if done % 25 == 0 or done == len(assets):
                    print(f"  {done}/{len(assets)}", flush=True)
'''


# sb2gs maps the pen color-parameter dropdown as a block *field*, but it lives in
# a menu input (`pen_menu_colorParam`), so `field="COLOR_PARAM"` never matches and
# every `pen_setPenColorParamTo`/`...By` collapses to the default `*_hue` block
# (brightness/saturation/transparency are lost). Flattening the menu first makes
# the overload apply. Applied to the installed sb2gs in install_sb2gs.
SB2GS_PEN_FIELD_OLD = 'field="COLOR_PARAM"'
SB2GS_PEN_FIELD_NEW = 'menu="COLOR_PARAM", field="colorParam"'

# sb2gs assumes every costume has rotationCenterX/Y, but newer saves omit them
# (Scratch's VM then uses the rendered skin's centre). Dereferencing the missing
# field raises AttributeError and the import fails, so treat a missing pivot as
# centred (i.e. skip the rewrite). Applied to the installed sb2gs.
SB2GS_CENTER_PATCHES = (
    (
        """    if (
        float(root.attrib.get("width", "0")) / 2 == costume.rotationCenterX
        and float(root.attrib.get("height", "0")) / 2 == costume.rotationCenterY
    ):
        return""",
        """    rcx = costume._.get("rotationCenterX")
    rcy = costume._.get("rotationCenterY")
    if rcx is None or rcy is None:
        return  # absent pivot means centred (newer saves omit it)
    if (
        abs(float(root.attrib.get("width", "0")) / 2 - rcx) < 0.5
        and abs(float(root.attrib.get("height", "0")) / 2 - rcy) < 0.5
    ):
        return  # near-centred (rounding); VM centring is close enough""",
    ),
    (
        """    if (
        costume.rotationCenterX == img.width // 2
        and costume.rotationCenterY == img.height // 2
    ):
        return""",
        """    rcx = costume._.get("rotationCenterX")
    rcy = costume._.get("rotationCenterY")
    if rcx is None or rcy is None:
        return  # absent pivot means centred (newer saves omit it)
    if (
        abs(rcx - img.width // 2) <= 1
        and abs(rcy - img.height // 2) <= 1
    ):
        return  # near-centred (rounding); VM centring is close enough""",
    ),
)

# sb2gs emits initial state as top-level sprite-init statements, but goboscript
# only accepts x/y/size/direction/volume/rotation-style there. It ignores
# `currentCostume` entirely, and for a draggable sprite it emits a nonexistent
# `set_draggable;` (the real block is `set_drag_mode_draggable`). Emit both as
# green-flag scripts instead. Applied to the installed sb2gs
# (decompile_sprite.py).
SB2GS_INITIAL_STATE_PATCHES = (
    (
        """        self.blocks: dict[str, Block] = target.blocks._
        self.volume: float = target.volume""",
        """        self.blocks: dict[str, Block] = target.blocks._
        self.volume: float = target.volume
        self.current_costume: int = target._.get("currentCostume", 0) or 0""",
    ),
    (
        """    decompile_rotation_style(ctx)
    if ctx.draggable:
        ctx.iprintln("set_draggable;")""",
        """    decompile_rotation_style(ctx)""",
    ),
    (
        """def decompile_sprite(ctx: Ctx) -> None:
    _ast.transform(ctx)
    decompile_properties(ctx)
    decompile_costumes(ctx)
    decompile_sounds(ctx)
    decompile_variables(ctx)
    decompile_lists(ctx)
    decompile_events(ctx)""",
        """def decompile_initial_state(ctx: Ctx) -> None:
    # Neither the saved current costume nor drag mode is a valid top-level
    # sprite-init statement in goboscript, so run them on green flag.
    if not ctx.is_stage and ctx.draggable:
        ctx.iprintln("onflag {")
        with ctx.indent():
            ctx.iprintln("set_drag_mode_draggable;")
        ctx.iprintln("}")
    index = ctx.current_costume
    if not ctx.is_stage and ctx.costumes and 0 < index < len(ctx.costumes):
        ctx.iprintln("onflag {")
        with ctx.indent():
            ctx.iprintln("switch_costume ", syntax.string(ctx.costumes[index].name), ";")
        ctx.iprintln("}")


def decompile_sprite(ctx: Ctx) -> None:
    _ast.transform(ctx)
    decompile_properties(ctx)
    decompile_costumes(ctx)
    decompile_sounds(ctx)
    decompile_variables(ctx)
    decompile_lists(ctx)
    decompile_initial_state(ctx)
    decompile_events(ctx)""",
    ),
)

# sb2gs ignores the project-level `monitors` array, so on-stage variable/list
# readouts are lost. goboscript can toggle monitor visibility (`show`/`hide`, not
# positions), so append a green-flag script per target. Applied to the installed
# sb2gs (decompile.py).
SB2GS_MONITOR_PATCHES = (
    (
        """from . import costumes
""",
        """from . import costumes, syntax
""",
    ),
    (
        """def decompile(input: Path, output: Path) -> None:
    shutil.rmtree(output, ignore_errors=True)""",
        """def monitor_script(project: JSONObject, target: JSONObject) -> str:
    \"\"\"Restore the initial visibility of the target's variable/list monitors.

    Scratch keeps monitor visibility in a project-level `monitors` array that
    sb2gs ignores; goboscript can only toggle visibility (not positions), so run
    it on green flag.
    \"\"\"
    owner = None if target.isStage else target.name
    lines: list[str] = []
    seen: set[str] = set()
    for monitor in project._.get("monitors") or []:
        if (monitor._.get("spriteName") or None) != owner:
            continue
        table = target.lists if monitor._.get("opcode") == "data_listcontents" else target.variables
        for var_id, entry in table._.items():
            if var_id != monitor._.get("id"):
                continue
            name = entry[0]
            if name not in seen:
                seen.add(name)
                verb = "show" if monitor._.get("visible", True) else "hide"
                lines.append(f"    {verb} {syntax.identifier(name)};")
            break
    if not lines:
        return ""
    return "onflag {\\n" + "\\n".join(lines) + "\\n}\\n"


def decompile(input: Path, output: Path) -> None:
    shutil.rmtree(output, ignore_errors=True)""",
    ),
    (
        """    ctx = Ctx(stage, assets)
    with output.joinpath("stage.gs").open("w") as file:
        decompile_sprite(ctx)
        file.write(str(ctx))""",
        """    ctx = Ctx(stage, assets)
    with output.joinpath("stage.gs").open("w") as file:
        decompile_sprite(ctx)
        file.write(str(ctx))
        file.write(monitor_script(project, stage))""",
    ),
    (
        """        with output.joinpath(f"{target.name}.gs").open("w") as file:
            decompile_sprite(ctx)
            file.write(str(ctx))""",
        """        with output.joinpath(f"{target.name}.gs").open("w") as file:
            decompile_sprite(ctx)
            file.write(str(ctx))
            file.write(monitor_script(project, target))""",
    ),
)

# sb2gs has no decompiler for control_for_each and drops its body. The VM treats
# it as a counted loop: it sets VARIABLE to the 1-based iteration index and runs
# Number(VALUE) times, so lower it to `set var = 0; repeat VALUE { var += 1; body }`.
# Applied to the installed sb2gs (decompile_stmt.py).
SB2GS_FOREACH_PATCHES = (
    (
        """    decompile_stack(ctx, inputs.block_id(block.inputs._.get("SUBSTACK")))


def decompile_procedures_call(ctx: Ctx, block: Block) -> None:""",
        """    decompile_stack(ctx, inputs.block_id(block.inputs._.get("SUBSTACK")))


def decompile_control_for_each(ctx: Ctx, block: Block) -> None:
    variable = syntax.identifier(block.fields.VARIABLE[0])
    ctx.iprintln(variable, " = 0;")
    ctx.iprint("repeat ")
    decompile_input(ctx, "VALUE", block)
    ctx.print(" ")
    body = inputs.block_id(block.inputs._.get("SUBSTACK"))
    if body is None:
        ctx.println("{}")
        return
    ctx.println("{")
    with ctx.indent():
        ctx.iprintln(variable, " += 1;")
        child = body
        while child:
            decompile_stmt(ctx, ctx.blocks[child])
            child = ctx.blocks[child].next
    ctx.iprintln("}")


def decompile_procedures_call(ctx: Ctx, block: Block) -> None:""",
    ),
)

# goboscript rejects both `var x;` and `list x;` on one target, and sb2gs's
# identifier cache maps two identical source names to a single identifier, so a
# variable and list sharing a name collide. Rename the list (in its table and
# every block field/input that references its id) before decompiling. Applied to
# the installed sb2gs (decompile.py).
SB2GS_CLASH_PATCHES = (
    (
        """        project = json.load(f, object_hook=JSONObject)
        assets = get_asset_names(project, "costumes")""",
        """        project = json.load(f, object_hook=JSONObject)
        fix_name_clashes(project)
        assets = get_asset_names(project, "costumes")""",
    ),
    (
        """def decompile(input: Path, output: Path) -> None:
    shutil.rmtree(output, ignore_errors=True)""",
        """def fix_name_clashes(project: JSONObject) -> None:
    \"\"\"Make variable/list names unique within each target.

    goboscript rejects a duplicate `var x;` or `list x;`, and sb2gs's identifier
    cache maps two identical source names to one identifier, so a variable and
    list sharing a name (or two same-named variables/lists) collide. Keep the
    first entry's name and rename the later ones, in their tables and every block
    field/input that references their id.
    \"\"\"
    for target in project.targets:
        entries = [("var", vid, entry) for vid, entry in target.variables._.items()]
        entries += [("list", lid, entry) for lid, entry in target.lists._.items()]
        used = {entry[0] for _, _, entry in entries}
        seen: set[str] = set()
        for kind, eid, entry in entries:
            if entry[0] not in seen:
                seen.add(entry[0])
                continue
            new_name = f"{entry[0]} {kind}"
            while new_name in used:
                new_name += " x"
            used.add(new_name)
            entry[0] = new_name
            for block in target.blocks._.values():
                fields = getattr(block, "fields", None)
                if fields is not None:
                    for field in fields._.values():
                        if isinstance(field, list) and len(field) >= 2 and field[1] == eid:
                            field[0] = new_name
                inputs = getattr(block, "inputs", None)
                if inputs is not None:
                    for inp in inputs._.values():
                        if isinstance(inp, list) and len(inp) >= 3 and inp[0] in (12, 13) and inp[2] == eid:
                            inp[1] = new_name


def decompile(input: Path, output: Path) -> None:
    shutil.rmtree(output, ignore_errors=True)""",
    ),
)


# goboscript emits no `monitors` array, so monitor positions and slider modes are
# lost. sb2gs records each monitor (with the compiled variable/list name) in a
# `monitors.json` sidecar; gobo-agent re-injects the array into the built sb3 (see
# gsdev.inject_monitors). Applied to the installed sb2gs (decompile.py).
SB2GS_MONITOR_SIDECAR_PATCHES = (
    (
        """    write_config(decompile_config(project), output)""",
        """    write_monitors(project, output)
    write_config(decompile_config(project), output)""",
    ),
    (
        """def decompile(input: Path, output: Path) -> None:
    shutil.rmtree(output, ignore_errors=True)""",
        """def write_monitors(project: JSONObject, output: Path) -> None:
    \"\"\"Record each monitor's settings with the compiled variable/list name.\"\"\"
    targets = {None: next((t for t in project.targets if t.isStage), None)}
    for target in project.targets:
        if not target.isStage:
            targets[target.name] = target
    records = []
    for monitor in project._.get("monitors") or []:
        owner = monitor._.get("spriteName") or None
        target = targets.get(owner)
        if target is None:
            continue
        is_list = monitor._.get("opcode") == "data_listcontents"
        table = (target.lists._ if is_list else target.variables._)
        mid = monitor._.get("id")
        name = next((entry[0] for vid, entry in table.items() if vid == mid), None)
        if name is None:
            continue
        records.append({
            "target": owner,
            "kind": "list" if is_list else "variable",
            "name": syntax.identifier(name),
            "mode": monitor._.get("mode", "default"),
            "x": monitor._.get("x", 0),
            "y": monitor._.get("y", 0),
            "visible": bool(monitor._.get("visible", False)),
            "sliderMin": monitor._.get("sliderMin", 0),
            "sliderMax": monitor._.get("sliderMax", 100),
            "isDiscrete": monitor._.get("isDiscrete", True),
        })
    output.joinpath("monitors.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8"
    )


def decompile(input: Path, output: Path) -> None:
    shutil.rmtree(output, ignore_errors=True)""",
    ),
)


# Preserve costumes byte-for-byte (no lossy pivot rewrites) and record their
# source rotationCenterX/Y/bitmapResolution in a `costumes.json` sidecar; gobo-agent
# sets them on the compiled costumes at build time (gsdev.inject_costumes). Applied
# to the installed sb2gs (costumes.py + decompile.py).
SB2GS_CENTER_DISABLE_PATCHES = (
    (
        """def fix_center(costume: JSONObject, path: Path, fixed: set[str]) -> None:
    if costume.md5ext in fixed:
        return
    fixed.add(costume.md5ext)
    if costume.dataFormat == "svg":
        fix_vector_center(costume, path)
    else:
        fix_bitmap_center(costume, path)""",
        """def fix_center(costume: JSONObject, path: Path, fixed: set[str]) -> None:
    # gobo-agent preserves costumes byte-for-byte and re-injects the source
    # rotationCenterX/Y/bitmapResolution into the built sb3 (gsdev.inject_costumes),
    # so the lossy 480x360 / re-canvas rewrites are disabled.
    return""",
    ),
)

SB2GS_COSTUME_SIDECAR_PATCHES = (
    (
        """    write_config(decompile_config(project), output)""",
        """    write_costumes(project, output)
    write_config(decompile_config(project), output)""",
    ),
    (
        """def decompile(input: Path, output: Path) -> None:
    shutil.rmtree(output, ignore_errors=True)""",
        """def write_costumes(project: JSONObject, output: Path) -> None:
    \"\"\"Record each costume's pivot/bitmapResolution (by target and order) so a
    later build can set them on the compiled costumes.\"\"\"
    records = []
    for target in project.targets:
        owner = None if target.isStage else target.name
        for index, costume in enumerate(target.costumes):
            records.append({
                "target": owner,
                "index": index,
                "name": costume._.get("name"),
                "rotationCenterX": costume._.get("rotationCenterX"),
                "rotationCenterY": costume._.get("rotationCenterY"),
                "bitmapResolution": costume._.get("bitmapResolution", 1),
            })
    output.joinpath("costumes.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8"
    )


def decompile(input: Path, output: Path) -> None:
    shutil.rmtree(output, ignore_errors=True)""",
    ),
)


# `syntax.scratch_number` (patch[0]) mirrors Scratch's arithmetic coercion
# (non-numeric -> 0) and is used by the constant-switch fold below. Patch[1] emits
# the coerced number for the *arithmetic operators* whose operands Scratch coerces
# (so `1 / ""` -> `1 / 0`, and `"last" + ""` -> `0 + 0`, which goboscript otherwise
# rejects as string arithmetic). It is guarded to those operators only: sb2gs also
# packs menu values and list specials ("last"/"random"/"any") into MATH_NUM inputs,
# and coercing *those* would blank `key_pressed("w")`, `start_sound "x"`,
# `distance_to(...)` and `x_list["last"]` to 0 and corrupt the project.
SB2GS_NUMBER_PATCHES = (
    (
        """def value(text: float | str) -> str:
    if isinstance(text, (int, float)):
        return number(text)
    if is_goboscript_literal(text):
        return text
    return string(text)""",
        """def value(text: float | str) -> str:
    if isinstance(text, (int, float)):
        return number(text)
    if is_goboscript_literal(text):
        return text
    return string(text)


def scratch_number(text: float | str) -> float:
    # Scratch coerces arithmetic inputs to numbers (non-numeric -> 0).
    if isinstance(text, (int, float)):
        return text
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return 0
    return int(parsed) if parsed.is_integer() else parsed""",
    ),
    (
        """    if input_type in {InputType.VAR, InputType.LIST}:
        ctx.print(syntax.identifier(input_value))
        return
    ctx.print(syntax.value(input_value))""",
        """    if input_type in {InputType.VAR, InputType.LIST}:
        ctx.print(syntax.identifier(input_value))
        return
    if input_type in {InputType.MATH_NUM, InputType.POSITIVE_NUM, InputType.WHOLE_NUM,
                      InputType.INTEGER_NUM, InputType.ANGLE_NUM} and block.opcode in {
            "operator_add", "operator_subtract", "operator_multiply",
            "operator_divide", "operator_mod", "operator_round", "operator_mathop",
            "operator_random",
    }:
        # Scratch coerces arithmetic operands (non-numeric -> 0) and goboscript
        # rejects string arithmetic. Only these operators are coerced, so menu
        # values and list specials packed into MATH_NUM inputs stay strings.
        ctx.print(syntax.number(syntax.scratch_number(input_value)))
        return
    ctx.print(syntax.value(input_value))""",
    ),
)

# goboscript has no Infinity/NaN literal, so sb2gs's json.dumps emits the bare
# words "Infinity"/"NaN" (Python's json accepts them) and is_goboscript_literal
# then treats them as literals, which goboscript rejects as an unknown variable.
# Emit a runtime expression instead (Scratch's `/` yields Infinity/NaN), and route
# numeric literals through number() so a numeric input holding "Infinity"/"NaN"/
# "-Infinity" (a common `set size to (Infinity)` hack) is handled too. Both are
# parenthesised so they are safe inside a larger expression. Applied to the
# installed sb2gs (syntax.py), after SB2GS_NUMBER_PATCHES[:1].
SB2GS_NONFINITE_PATCHES = (
    (
        """def number(value: float) -> str:
    return json.dumps(value)""",
        """def number(value: float) -> str:
    if isinstance(value, float):
        if value != value:
            return "(0 / 0)"
        if value == float("inf"):
            return "(1 / 0)"
        if value == float("-inf"):
            return "(-1 / 0)"
    return json.dumps(value)""",
    ),
    (
        """def value(text: float | str) -> str:
    if isinstance(text, (int, float)):
        return number(text)
    if is_goboscript_literal(text):
        return text
    return string(text)""",
        """def value(text: float | str) -> str:
    if isinstance(text, (int, float)):
        return number(text)
    if is_goboscript_literal(text):
        return number(json.loads(text))
    return string(text)""",
    ),
)

# goboscript's switch_costume/switch_backdrop accept a costume/backdrop *name* only
# (there is no index or relative form). When a project switches by a constant number
# (e.g. `("last" + "")` -> 0 -> last costume), fold it to the name. Applied to the
# installed sb2gs (decompile_stmt.py).
SB2GS_SWITCH_FOLD_PATCHES = (
    (
        """from . import _ast, custom_blocks, inputs, syntax""",
        """import math

from . import _ast, custom_blocks, inputs, syntax""",
    ),
    (
        """def decompile_block(ctx: Ctx, block: Block) -> None:
    signature = deepcopy(BLOCKS[block.opcode])""",
        """_ARITH_OPS = {
    "operator_add": lambda a, b: a + b,
    "operator_subtract": lambda a, b: a - b,
    "operator_multiply": lambda a, b: a * b,
    "operator_divide": lambda a, b: (a / b) if b else 0,
    "operator_mod": lambda a, b: (a % b) if b else 0,
}


def _fold_input_number(ctx: Ctx, block: Block, name: str):
    raw = block.inputs._.get(name)
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    value = raw[1]
    if isinstance(value, str) and value in ctx.blocks:
        return _fold_block_number(ctx, ctx.blocks[value])
    if isinstance(value, list) and value and value[0] in (4, 5, 6, 7, 8):
        return syntax.scratch_number(value[1])
    return None


def _fold_block_number(ctx: Ctx, block: Block):
    if block.opcode == "operator_round":
        inner = _fold_input_number(ctx, block, "NUM")
        return round(inner) if inner is not None else None
    function = _ARITH_OPS.get(block.opcode)
    if function is None:
        return None
    left = _fold_input_number(ctx, block, "NUM1")
    right = _fold_input_number(ctx, block, "NUM2")
    if left is None or right is None:
        return None
    return function(left, right)


def _indexed_name(costumes, index):
    if not costumes:
        return None
    total = len(costumes)
    position = math.floor(index)
    if position < 1:
        position += total
    if 1 <= position <= total:
        return costumes[position - 1].name
    return None


def _decompile_constant_switch(ctx: Ctx, block: Block) -> bool:
    if block.opcode == "looks_switchcostumeto":
        keyword, input_name = "switch_costume", "COSTUME"
    elif block.opcode == "looks_switchbackdropto":
        keyword, input_name = "switch_backdrop", "BACKDROP"
    else:
        return False
    index = _fold_input_number(ctx, block, input_name)
    if index is None:
        return False
    name = _indexed_name(getattr(ctx, "costumes", None), index)
    if name is None:
        return False
    ctx.iprintln(keyword, " ", syntax.string(name), ";")
    return True


def decompile_block(ctx: Ctx, block: Block) -> None:
    if _decompile_constant_switch(ctx, block):
        return
    signature = deepcopy(BLOCKS[block.opcode])""",
    ),
)


def _unzip_into(data: bytes, destination: Path) -> None:
    import io

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            if member.filename.endswith("/"):
                continue
            target = destination / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))


def install_sb2gs(force: bool = False, offline: bool = False) -> None:
    system = system_sb2gs()
    if system and not force:
        _log(f"sb2gs already available: {system} (skipping download)")
        return
    if sb2gs_present() and not force:
        try:
            recorded = json.loads(sb2gs_marker().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recorded = {}
        if recorded.get("commit") == SB2GS_COMMIT:
            _log(f"sb2gs already installed: {sb2gs_source_dir()}")
            return
    if offline:
        raise SetupError(
            f"sb2gs is not installed and --offline was given; expected {sb2gs_root()}"
        )
    if sys.version_info[:2] < (3, 14):
        # sb2gs uses 3.13+/3.14 syntax and cp314 pillow wheels. The core harness
        # still runs on 3.10+, so warn rather than failing setup on older Python.
        _log(f"sb2gs needs Python 3.14+; skipping (found {platform.python_version()})")
        return
    source = sb2gs_source_dir()
    if source.exists():
        shutil.rmtree(source)
    source.mkdir(parents=True, exist_ok=True)
    zip_bytes = _download(SB2GS_ZIP_URL, f"sb2gs {SB2GS_COMMIT[:8]}")
    if hashlib.sha256(zip_bytes).hexdigest() != SB2GS_ZIP_SHA256:
        raise SetupError("sb2gs source checksum mismatch")
    import io

    prefix = f"sb2gs-{SB2GS_COMMIT}/src/sb2gs/"
    written = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for member in archive.infolist():
            if member.is_dir() or not member.filename.startswith(prefix):
                continue
            target = source / "sb2gs" / member.filename[len(prefix):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
            written += 1
    if not written:
        raise SetupError("sb2gs source archive had no src/sb2gs files")
    # Patched downloader: robust `sb2gs --id` for large projects (retry, timeout,
    # concurrent asset downloads). See SB2GS_DOWNLOADER_PATCH.
    (source / "sb2gs" / "sb3_downloader.py").write_text(
        SB2GS_DOWNLOADER_PATCH, encoding="utf-8"
    )
    # Fix the pen color-parameter menu (see SB2GS_PEN_FIELD_*): without it,
    # brightness/saturation/transparency collapse to set_pen_hue.
    stmt = source / "sb2gs" / "decompile_stmt.py"
    if stmt.is_file():
        text = stmt.read_text(encoding="utf-8")
        if text.count(SB2GS_PEN_FIELD_OLD) == 2:
            stmt.write_text(
                text.replace(SB2GS_PEN_FIELD_OLD, SB2GS_PEN_FIELD_NEW),
                encoding="utf-8",
            )
    # Treat a missing costume pivot as centred (see SB2GS_CENTER_PATCHES): without
    # it, newer saves that omit rotationCenterX/Y fail to import.
    cos = source / "sb2gs" / "costumes.py"
    if cos.is_file():
        text = cos.read_text(encoding="utf-8")
        for old, new in SB2GS_CENTER_PATCHES:
            if old in text:
                text = text.replace(old, new, 1)
        cos.write_text(text, encoding="utf-8")
    # Preserve the saved initial costume and drag mode (see
    # SB2GS_INITIAL_STATE_PATCHES).
    sprite = source / "sb2gs" / "decompile_sprite.py"
    if sprite.is_file():
        text = sprite.read_text(encoding="utf-8")
        for old, new in SB2GS_INITIAL_STATE_PATCHES:
            if old in text:
                text = text.replace(old, new, 1)
        sprite.write_text(text, encoding="utf-8")
    # Restore monitor visibility (see SB2GS_MONITOR_PATCHES).
    decomp = source / "sb2gs" / "decompile.py"
    if decomp.is_file():
        text = decomp.read_text(encoding="utf-8")
        for old, new in SB2GS_MONITOR_PATCHES:
            if old in text:
                text = text.replace(old, new, 1)
        decomp.write_text(text, encoding="utf-8")
    # Add a control_for_each decompiler (see SB2GS_FOREACH_PATCHES).
    stmts = source / "sb2gs" / "decompile_stmt.py"
    if stmts.is_file():
        text = stmts.read_text(encoding="utf-8")
        for old, new in SB2GS_FOREACH_PATCHES:
            if old in text:
                text = text.replace(old, new, 1)
        stmts.write_text(text, encoding="utf-8")
    # Rename same-named variables/lists (see SB2GS_CLASH_PATCHES).
    if decomp.is_file():
        text = decomp.read_text(encoding="utf-8")
        for old, new in SB2GS_CLASH_PATCHES:
            if old in text:
                text = text.replace(old, new, 1)
        decomp.write_text(text, encoding="utf-8")
    # Record monitors for post-build re-injection (see SB2GS_MONITOR_SIDECAR_PATCHES).
    if decomp.is_file():
        text = decomp.read_text(encoding="utf-8")
        for old, new in SB2GS_MONITOR_SIDECAR_PATCHES:
            if old in text:
                text = text.replace(old, new, 1)
        decomp.write_text(text, encoding="utf-8")
    # Record costume pivots (see SB2GS_COSTUME_SIDECAR_PATCHES).
    if decomp.is_file():
        text = decomp.read_text(encoding="utf-8")
        for old, new in SB2GS_COSTUME_SIDECAR_PATCHES:
            if old in text:
                text = text.replace(old, new, 1)
        decomp.write_text(text, encoding="utf-8")
    # Disable the lossy costume rewrites (see SB2GS_CENTER_DISABLE_PATCHES).
    cos = source / "sb2gs" / "costumes.py"
    if cos.is_file():
        text = cos.read_text(encoding="utf-8")
        for old, new in SB2GS_CENTER_DISABLE_PATCHES:
            if old in text:
                text = text.replace(old, new, 1)
        cos.write_text(text, encoding="utf-8")
    # Coerce arithmetic operands and fold constant costume/backdrop switches (see
    # SB2GS_NUMBER_PATCHES / SB2GS_SWITCH_FOLD_PATCHES).
    for filename, patches in (("decompile_input.py", SB2GS_NUMBER_PATCHES),
                              ("decompile_stmt.py", SB2GS_SWITCH_FOLD_PATCHES)):
        target = source / "sb2gs" / filename
        if target.is_file():
            text = target.read_text(encoding="utf-8")
            for old, new in patches:
                if old in text:
                    text = text.replace(old, new, 1)
            target.write_text(text, encoding="utf-8")
    syn = source / "sb2gs" / "syntax.py"
    if syn.is_file():
        text = syn.read_text(encoding="utf-8")
        for old, new in SB2GS_NUMBER_PATCHES[:1] + SB2GS_NONFINITE_PATCHES:
            if old in text:
                text = text.replace(old, new, 1)
        syn.write_text(text, encoding="utf-8")

    site = sb2gs_site_dir()
    key: str | None = None
    wheels: list[tuple[str, str, str]] = []
    if _sb2gs_system_packages_ok(source):
        # A full interpreter already provides httpx/pillow/rich/tomlkit: skip the
        # wheels and import them from its own site-packages.
        if site.exists():
            shutil.rmtree(site)
        mode = "system"
        _log("sb2gs: interpreter already has its packages (skipping wheels)")
    else:
        key = _sb2gs_pillow_key()
        if key is None or key not in SB2GS_PILLOW:
            raise SetupError(
                f"no pinned pillow wheel for {sys.platform}/{platform.machine()}; "
                "sb2gs cannot be installed on this platform"
            )
        if site.exists():
            shutil.rmtree(site)
        wheels = [*SB2GS_WHEELS.values(), SB2GS_PILLOW[key]]
        for filename, url, sha in wheels:
            data = _download(url, filename)
            if hashlib.sha256(data).hexdigest() != sha:
                raise SetupError(f"checksum mismatch for {filename}")
            _unzip_into(data, site)
        mode = "bundled"
    detail = f"{len(wheels)} wheels" if wheels else "system packages"
    _log(f"sb2gs {SB2GS_COMMIT[:8]}: {written} source files, {detail}")

    sb2gs_run_py().write_text(SB2GS_RUNNER, encoding="utf-8")
    sb2gs_marker().write_text(
        json.dumps(
            {"commit": SB2GS_COMMIT, "mode": mode, "pillow": key,
             "packages": sorted(SB2GS_WHEELS)},
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    _log(f"sb2gs -> {source}")


def sb2gs_env() -> dict:
    """Environment for the sb2gs process.

    Puts the pinned goboscript on PATH so sb2gs's ``--verify`` (which shells out
    to ``goboscript``) works without a system compiler.
    """
    env = dict(os.environ)
    env["PATH"] = str(goboscript_dir()) + os.pathsep + env.get("PATH", "")
    # sb2gs opens output files without an explicit encoding; UTF-8 mode keeps
    # non-ASCII sprite/layer names working regardless of the OS locale.
    env["PYTHONUTF8"] = "1"
    return env


def run_sb2gs(argv: list[str]) -> int:
    if not sb2gs_present():
        raise SetupError("sb2gs is not installed; run `gsdev setup` first")
    if sys.version_info[:2] < (3, 14):
        raise SetupError(
            f"sb2gs needs Python 3.14+ (found {platform.python_version()}); run gsdev "
            "with the portable Python or a 3.14+ interpreter"
        )
    raw = os.environ.get("GSDEV_SB2GS_TIMEOUT", "1800")
    try:
        timeout = float(raw)
    except ValueError:
        raise SetupError(f"GSDEV_SB2GS_TIMEOUT={raw!r} is not a number")
    try:
        return subprocess.run(
            [sys.executable, str(sb2gs_run_py()), *argv],
            timeout=timeout, env=sb2gs_env(),
        ).returncode
    except subprocess.TimeoutExpired:
        # Never hang (and strand a child): kill it and fail with guidance. The
        # patched downloader already retries/times out per request; this is the
        # overall guard.
        raise SetupError(
            f"sb2gs timed out after {int(timeout)}s (slow/stalled network?); "
            "download the .sb3 in a browser and pass the local file instead"
        )


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
        "vendor": str(bundles_dir()) if vendor_present() else None,
        "nodeModules": str(node_modules_dir()) if node_modules_present() else None,
        "sb2gs": system_sb2gs() or (str(sb2gs_source_dir()) if sb2gs_present() else None),
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
        if only in (None, "sb2gs"):
            install_sb2gs(force=force, offline=offline)
    except SetupError as error:
        _log(f"FAILED: {error}")
        return 1

    info = status()
    source = info.get("goboscriptSource")
    detail = info["goboscriptVersion"] or info["goboscript"] or "missing"
    _log(f"goboscript: {detail}{f' ({source})' if source else ''}")
    _log(f"host bundles: {info['vendor'] or info['nodeModules'] or 'missing'}")
    _log(f"sb2gs: {info['sb2gs'] or 'missing'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download the prebuilt gobo-agent tools (no admin).")
    parser.add_argument("--only", choices=["goboscript", "vendor", "sb2gs"], help="install one piece")
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
