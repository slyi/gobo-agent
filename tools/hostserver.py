#!/usr/bin/env python3
"""Static server for the scratch host page.

Same as ``python -m http.server``, but sends ``Cache-Control: no-store`` so an
edited ``host.js`` can never be served stale. The stdlib server allows
conditional 304s, which made a changed ``host.js`` appear missing after a reload.

It also advertises a per-session ownership token (argv[3], or ``GSDEV_HOST_TOKEN``)
via an ``X-Gsdev-Host`` response header and a ``GET /__gsdev`` JSON endpoint
(``{"token", "pid"}``). gsdev probes that before reusing or shutting down "the
server on port 8077", so a foreign application that happens to hold the port is
never mistaken for this harness (and is never killed).

Usage: ``hostserver.py DIR PORT [TOKEN] [FINGERPRINT] [BUNDLES]``. The @scratch
bundles are served from BUNDLES at ``/vendor/`` so one shared copy is reused by
every project.
"""

from __future__ import annotations

import json
import os
import posixpath
import sys
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_TOKEN = ""
_DIRECTORY = ""
_FINGERPRINT = ""
_BUNDLES = ""


class NoStoreHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header(
            "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"
        )
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if _TOKEN:
            self.send_header("X-Gsdev-Host", _TOKEN)
        super().end_headers()

    def do_GET(self) -> None:  # ownership probe
        if self.path.split("?", 1)[0] in ("/__gsdev", "/__gsdev/"):
            body = json.dumps(
                {"token": _TOKEN, "pid": os.getpid(), "dir": _DIRECTORY,
                 "fingerprint": _FINGERPRINT},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:
        # Mount the shared @scratch bundles at /vendor/ so the host page's
        # `./vendor/@scratch/...` resolves without copying them into each project.
        # Confine to the bundles dir (drop any ".."/absolute segments).
        if _BUNDLES:
            request = urllib.parse.urlparse(path).path
            clean = posixpath.normpath(urllib.parse.unquote(request))
            if clean == "/vendor" or clean.startswith("/vendor/"):
                relative = clean[len("/vendor/"):] if clean.startswith("/vendor/") else ""
                parts = [part for part in relative.split("/") if part not in ("", ".", "..")]
                return os.path.join(_BUNDLES, *parts)
        return super().translate_path(path)

    def log_message(self, *args) -> None:
        pass


def main(argv: list[str]) -> int:
    global _TOKEN, _DIRECTORY, _FINGERPRINT, _BUNDLES
    if len(argv) not in (3, 4, 5, 6):
        print("usage: hostserver.py DIR PORT [TOKEN] [FINGERPRINT] [BUNDLES]", file=sys.stderr)
        return 2
    directory, port = argv[1], int(argv[2])
    _DIRECTORY = directory
    _TOKEN = argv[3] if len(argv) >= 4 else os.environ.get("GSDEV_HOST_TOKEN", "")
    _FINGERPRINT = argv[4] if len(argv) >= 5 else os.environ.get("GSDEV_HOST_FINGERPRINT", "")
    _BUNDLES = argv[5] if len(argv) >= 6 else os.environ.get("GSDEV_BUNDLES", "")
    handler = partial(NoStoreHandler, directory=directory)
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
