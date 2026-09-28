#!/bin/sh
# One-shot, admin-free setup for gobo-agent on macOS/Linux, mirroring setup.ps1.
#
# Finds a Python 3.10+ and then fetches the prebuilt tools (the official
# goboscript release binary and the @scratch/* browser bundles) through
# tools/bootstrap.py. No Rust, MSVC/MSYS2, Node/npm, installer, or administrator
# rights are needed: everything lands under the repository (.tools/) and the
# user's own directories.
#
#   ./setup.sh                        # install goboscript + host bundles
#   ./setup.sh --only vendor          # only the browser bundles
#   ./setup.sh --force --offline      # reinstall from the local cache
#
# A Chromium-based browser (Chrome/Chromium/Edge) is used as the test host; it is
# expected to already be installed, and Safari is out of scope.

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
GSDEV_ROOT=$here
export GSDEV_ROOT
. "$here/tools/python-env.sh"

if [ ! -f "$here/tools/bootstrap.py" ]; then
    echo "setup.sh: tools/bootstrap.py not found; run this from the repository root" >&2
    exit 1
fi

if ! python=$(gsdev_find_python); then
    gsdev_python_hint
    exit 1
fi

PYTHONUTF8=1
: "${PYTHONIOENCODING:=utf-8}"
export PYTHONUTF8 PYTHONIOENCODING

echo "[setup] using $python"
exec "$python" "$here/tools/bootstrap.py" "$@"
