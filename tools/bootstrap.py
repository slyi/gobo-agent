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


def run_sb2gs(argv: list[str]) -> int:
    if not sb2gs_present():
        raise SetupError("sb2gs is not installed; run `gsdev setup` first")
    if sys.version_info[:2] < (3, 14):
        raise SetupError(
            f"sb2gs needs Python 3.14+ (found {platform.python_version()}); run gsdev "
            "with the portable Python or a 3.14+ interpreter"
        )
    return subprocess.run([sys.executable, str(sb2gs_run_py()), *argv]).returncode


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
