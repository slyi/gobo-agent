# Shared Python resolution for the POSIX launchers (macOS/Linux).
#
# Source it from a repo-level shim:
#     . "$GSDEV_ROOT/tools/python-env.sh"
#
# Mirrors tools/python-env.ps1: the minimum supported version is 3.10 (the tools
# use match/case and parenthesised context managers), and every candidate is
# *run* rather than merely looked up, so a non-Python stub on PATH (for example
# a distro's `python3` placeholder) is rejected instead of trusted.
#
# Resolution order, identical in spirit to the PowerShell version:
#   1. $GSDEV_PYTHON
#   2. $GSDEV_PYTHON_DIR/bin/python3 (default: <tools-root>/python/bin/python3,
#      shared per user; see GSDEV_TOOLS/GSDEV_HOME)
#   3. python3, then python, on PATH

gsdev_python_is_310() {
    [ -n "${1:-}" ] || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
        >/dev/null 2>&1
}

# Shared per-user install root (goboscript, sb2gs, python). Mirrors
# bootstrap.default_home(): GSDEV_TOOLS, then GSDEV_HOME, then ~/.cache.
gsdev_tools_root() {
    if [ -n "${GSDEV_TOOLS:-}" ]; then
        printf '%s\n' "$GSDEV_TOOLS"
    elif [ -n "${GSDEV_HOME:-}" ]; then
        printf '%s\n' "$GSDEV_HOME"
    else
        printf '%s\n' "${XDG_CACHE_HOME:-$HOME/.cache}/gobo-agent"
    fi
}

gsdev_python_dir() {
    if [ -n "${GSDEV_PYTHON_DIR:-}" ]; then
        printf '%s\n' "$GSDEV_PYTHON_DIR"
    else
        printf '%s\n' "$(gsdev_tools_root)/python"
    fi
}

# Print the path to a working Python 3.10+, or return 1 when there is none.
gsdev_find_python() {
    if [ -n "${GSDEV_PYTHON:-}" ]; then
        if gsdev_python_is_310 "$GSDEV_PYTHON"; then
            printf '%s\n' "$GSDEV_PYTHON"
            return 0
        fi
        printf '[gsdev] GSDEV_PYTHON=%s is not Python 3.10+; ignoring it\n' \
            "$GSDEV_PYTHON" >&2
    fi

    portable="$(gsdev_python_dir)/bin/python3"
    if [ -x "$portable" ] && gsdev_python_is_310 "$portable"; then
        printf '%s\n' "$portable"
        return 0
    fi

    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && gsdev_python_is_310 "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

gsdev_python_hint() {
    cat >&2 <<'EOF'
[gsdev] Python 3.10+ not found.
  Install one (macOS: brew install python; Debian/Ubuntu: sudo apt install python3),
  point GSDEV_PYTHON at an interpreter, or place a portable interpreter under
  the tools root's python/ (or set GSDEV_PYTHON_DIR). The tools root is
  GSDEV_TOOLS, else GSDEV_HOME, else ~/.cache/gobo-agent.
EOF
}
