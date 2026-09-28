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

Usage: ``hostserver.py DIR PORT [TOKEN]``
"""

from __future__ import annotations

import json
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_TOKEN = ""
_DIRECTORY = ""


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
                {"token": _TOKEN, "pid": os.getpid(), "dir": _DIRECTORY},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args) -> None:
        pass


def main(argv: list[str]) -> int:
    global _TOKEN, _DIRECTORY
    if len(argv) not in (3, 4):
        print("usage: hostserver.py DIR PORT [TOKEN]", file=sys.stderr)
        return 2
    directory, port = argv[1], int(argv[2])
    _DIRECTORY = directory
    _TOKEN = argv[3] if len(argv) == 4 else os.environ.get("GSDEV_HOST_TOKEN", "")
    handler = partial(NoStoreHandler, directory=directory)
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
