#!/usr/bin/env python3
"""Package a built goboscript binary (+ its MIT LICENSE) into the release asset.

Used by .github/workflows/goboscript-prebuilt.yml. Writes ``out/<asset>``.
"""

from __future__ import annotations

import argparse
import pathlib
import tarfile
import zipfile


def find_binary(release_dir: pathlib.Path) -> pathlib.Path:
    for name in ("goboscript.exe", "goboscript"):
        candidate = release_dir / name
        if candidate.exists():
            return candidate
    matches = [p for p in release_dir.glob("goboscript*") if p.is_file()]
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(f"no goboscript binary in {release_dir}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--kind", choices=["zip", "tar"], required=True)
    args = parser.parse_args()

    source = pathlib.Path("goboscript")
    release_dir = source / "target" / args.target / "release"
    binary = find_binary(release_dir)
    license_path = source / "LICENSE"

    out = pathlib.Path("out")
    out.mkdir(parents=True, exist_ok=True)
    archive = out / args.asset

    if args.kind == "zip":
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            handle.write(binary, binary.name)
            if license_path.exists():
                handle.write(license_path, "LICENSE")
    else:
        if archive.exists():
            archive.unlink()
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(binary, arcname=binary.name)
            if license_path.exists():
                handle.add(license_path, arcname="LICENSE")

    print(f"packaged {binary} -> {archive} ({archive.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
