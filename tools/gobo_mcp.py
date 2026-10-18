#!/usr/bin/env python3
"""gobo-agent MCP server: run the harness (browser included) from an MCP client.

The harness normally runs shell tools inside a process sandbox, and a browser
spawned from there inherits it and dies at startup. A stdio MCP server is
spawned by the *host* (dsh-mcp-client), outside that sandbox, so `gsdev run`
here can launch Edge/Chrome normally.

Transports:
    python tools/gobo_mcp.py                      # stdio (one JSON-RPC message per line)
    python tools/gobo_mcp.py --http 127.0.0.1:8770  # MCP Streamable HTTP at /mcp

Tools: gobo_gsdev (allowlisted argv), gobo_run, gobo_screenshot, gobo_session,
gobo_doctor, gobo_close. Resource: gobo://status.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER_NAME = "gobo-agent-mcp"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"
SCHEMA = 1
DEFAULT_TIMEOUT_MS = 300_000
MAX_TIMEOUT_MS = 1_800_000
MAX_OUTPUT_CHARS = 256 * 1024

# The full gsdev verb surface, including the toolchain verbs (setup, sb2gs,
# init) and harness helpers (selftest, tasks). This server is a host-side
# integration for a checkout owned by the same user, not a multi-tenant service.
ALLOWED_VERBS = {
    "build", "status", "doctor", "setup", "sb2gs", "selftest", "tasks", "init",
    "run", "screenshot", "preflight",
    "stop", "close", "restart", "wait_stopped",
    "get", "set", "set_batch", "watch", "session", "test",
    "mouse", "click", "key", "pixel",
    "frame", "wait_frame", "step", "pause", "resume",
    "render", "record", "trace", "stop_record", "until",
    "broadcast", "broadcast_wait",
    "inspect", "props", "prop", "clones", "targets",
    "logs", "perf", "gpu",
    "setprofiling", "profiling", "profilereset", "profile",
    "errors", "expect_no_errors", "wait_until", "wait_pixel",
}

# gsbridge talks to a live page (embed, editor, github, localhost). Expose its
# whole verb surface: input verbs align a capture with the local pointer state,
# and profile/profiling work the same way they do over gsdev.
BRIDGE_VERBS = {
    "run", "get", "set", "set_batch", "screenshot", "close", "session", "test",
    "targets", "props", "prop", "clones", "inspect",
    "frame", "wait_frame", "wait_until", "wait_pixel", "wait_stopped",
    "step", "pause", "resume", "restart", "stop",
    "render", "record", "trace", "stop_record", "until", "watch", "logs",
    "broadcast", "broadcast_wait",
    "mouse", "click", "key", "pixel",
    "errors", "expect_no_errors", "gpu", "perf",
    "setprofiling", "profilereset", "profiling", "profile",
}

TOOLS = [
    {
        "name": "gobo_gsdev",
        "description": (
            "Run one allowlisted gsdev command in a gobo-agent project and return its "
            "exit code, stdout and stderr. This is the same CLI the terminal uses. "
            "Examples: [\"get\",\"@fps\"], [\"session\",\"--file\",\"checks.txt\"], "
            "[\"set\",\"main.tx=120\"]."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project directory, relative to the server root (default: root)."},
                "args": {"type": "array", "items": {"type": "string"}, "description": "gsdev arguments, verbs first."},
                "timeoutMs": {"type": "integer", "description": "Kill the command after this many ms (default 300000, max 1800000)."},
            },
            "required": ["args"],
        },
    },
    {
        "name": "gobo_run",
        "description": (
            "Build the project, load it into a browser and run it. Defaults are the "
            "agent loop: headless, 6s, leave the host running for later calls."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "duration": {"type": "number", "description": "Seconds to run (default 6)."},
                "headless": {"type": "boolean", "description": "Headless browser (default true)."},
                "leaveRunning": {"type": "boolean", "description": "Keep the host warm (default true)."},
                "mode": {"type": "string", "enum": ["debug", "release"]},
                "noReload": {"type": "boolean", "description": "Keep the loaded project/state."},
                "timeoutMs": {"type": "integer"},
            },
        },
    },
    {
        "name": "gobo_screenshot",
        "description": "Capture the stage to a PNG (default: build and load first).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "out": {"type": "string", "description": "PNG path, relative to the project (default debug/shot.png)."},
                "headless": {"type": "boolean", "description": "Headless browser (default true)."},
                "noBuild": {"type": "boolean", "description": "Skip compiling (source unchanged)."},
                "noReload": {"type": "boolean", "description": "Capture the running project instead of reloading it (implies no build; keeps it running)."},
                "delay": {"type": "integer", "description": "Wait this many ms before capturing (default 0; a drawing project needs a few seconds)."},
                "timeoutMs": {"type": "integer"},
            },
        },
    },
    {
        "name": "gobo_session",
        "description": "Run a session file (input, waits, capture/expect) against the running project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "file": {"type": "string", "description": "Session file, relative to the project."},
                "headless": {"type": "boolean"},
                "bundle": {"type": "boolean", "description": "Write failure evidence under --artifacts."},
                "artifacts": {"type": "string", "description": "Artifacts directory, relative to the project."},
                "timeoutMs": {"type": "integer"},
            },
            "required": ["file"],
        },
    },
    {
        "name": "gobo_doctor",
        "description": "Check the toolchain (python, goboscript, host bundles, browser, build readiness).",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}, "timeoutMs": {"type": "integer"}},
        },
    },
    {
        "name": "gobo_close",
        "description": "Close the project's browser host (keeps the files).",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}, "timeoutMs": {"type": "integer"}},
        },
    },
    {
        "name": "gobo_gsbridge",
        "description": (
            "Run gsbridge against a live Scratch page — e.g. the real embed "
            "(run --target production --project-id N), the editor, github, or "
            "localhost. Every gsbridge verb is allowed (run, get, set, screenshot, "
            "session, profile, mouse/click/key, waits, …); same syntax and result "
            "shapes as gobo_gsdev, but the page is a live site."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project directory whose debug/ receives --out paths (default: server root)."},
                "args": {"type": "array", "items": {"type": "string"}, "description": "gsbridge arguments, verbs first."},
                "timeoutMs": {"type": "integer", "description": "Kill the command after this many ms (default 300000, max 1800000)."},
            },
            "required": ["args"],
        },
    },
]


class ToolError(Exception):
    pass


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[-MAX_OUTPUT_CHARS:]


def find_launcher(project: Path) -> list[str]:
    """The project's own launcher: adopted layout first, then the repo layout."""
    candidates = [
        project / "gobo-agent" / "runtime" / "tools" / "gsdev.ps1",
        project / "tools" / "gsdev.ps1",
    ]
    if os.name == "nt":
        # The MCP host may run this server with a scrubbed environment; resolve
        # PowerShell by absolute path when PATH does not carry it.
        powershell = shutil.which("powershell") or (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
            if Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe").is_file()
            else "powershell")
        for ps1 in candidates:
            if ps1.is_file():
                return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)]
    else:
        for sh in (project / "gobo-agent" / "runtime" / "tools" / "gsdev",
                   project / "tools" / "gsdev"):
            if sh.is_file():
                return [str(sh)]
    raise ToolError(f"no gobo-agent launcher under {project}")


def validate_args(args: object, project: Path, verbs: set[str] = ALLOWED_VERBS) -> list[str]:
    if not isinstance(args, list) or not args:
        raise ToolError("args must be a non-empty array of strings")
    if len(args) > 64:
        raise ToolError("too many arguments")
    cleaned: list[str] = []
    for arg in args:
        if not isinstance(arg, str) or not arg:
            raise ToolError("args entries must be non-empty strings")
        if len(arg) > 4096 or any(ch in arg for ch in "\0\r\n"):
            raise ToolError("args entry is invalid")
        value = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
        path = Path(value)
        if path.is_absolute():
            resolved = path.resolve()
            if not resolved.is_relative_to(project):
                raise ToolError(f"path escapes the project: {value}")
        elif ".." in path.parts:
            raise ToolError(f"path escapes the project: {value}")
        cleaned.append(arg)
    if cleaned[0] not in verbs:
        raise ToolError(f"command not allowed: {cleaned[0]}")
    return cleaned


def find_bridge(project: Path) -> Path:
    """gsbridge ships with the checkout; an adopted project may also carry it."""
    for candidate in (
        project / "tools" / "gsbridge.py",
        project / "gobo-agent" / "runtime" / "tools" / "gsbridge.py",
        Path(__file__).resolve().parent / "gsbridge.py",
    ):
        if candidate.is_file():
            return candidate
    raise ToolError("no gsbridge.py under the project or this checkout")


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        else:
            proc.kill()
    except (OSError, subprocess.SubprocessError):
        pass


def run_command(project: Path, command: list[str], args: list[str], timeout_ms: int) -> dict:
    timeout_ms = max(1000, min(int(timeout_ms), MAX_TIMEOUT_MS))
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.monotonic()
    proc = subprocess.Popen(
        [*command, *args], cwd=str(project), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_ms / 1000)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", "killed after timeout"
    return {
        "schema": SCHEMA,
        "project": str(project),
        "argv": args,
        "code": -1 if timed_out else (proc.returncode if proc.returncode is not None else -1),
        "timedOut": timed_out,
        "stdout": cap(stdout or ""),
        "stderr": cap(stderr or ""),
        "durationMs": int((time.monotonic() - started) * 1000),
        "finishedAt": iso_now(),
    }


def run_gsdev(project: Path, args: list[str], timeout_ms: int) -> dict:
    return run_command(project, find_launcher(project), args, timeout_ms)


def run_bridge(project: Path, args: list[str], timeout_ms: int) -> dict:
    return run_command(project, [sys.executable, str(find_bridge(project))], args, timeout_ms)


class Server:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.started_at = iso_now()
        self.last: dict | None = None
        self.lock = threading.Lock()
        self.sessions: dict[str, bool] = {}

    # -- project resolution -------------------------------------------------
    def project(self, name: object) -> Path:
        if name in (None, "", "."):
            project = self.root
        else:
            if not isinstance(name, str):
                raise ToolError("project must be a string")
            candidate = Path(name)
            project = (self.root / candidate) if not candidate.is_absolute() else candidate
        project = project.resolve()
        if not project.is_relative_to(self.root):
            raise ToolError(f"project is outside the server root {self.root}: {project}")
        if not project.is_dir():
            raise ToolError(f"no such project directory: {project}")
        return project

    def status_payload(self) -> dict:
        return {
            "schema": SCHEMA,
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "root": str(self.root),
            "startedAt": self.started_at,
            "last": self.last,
        }

    # -- tool dispatch ------------------------------------------------------
    def call_tool(self, name: str, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            arguments = {}
        timeout = arguments.get("timeoutMs", DEFAULT_TIMEOUT_MS)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ToolError("timeoutMs must be a positive number")
        if name == "gobo_gsdev":
            project = self.project(arguments.get("project"))
            argv = validate_args(arguments.get("args"), project)
        elif name == "gobo_run":
            project = self.project(arguments.get("project"))
            duration = arguments.get("duration", 6)
            if not isinstance(duration, (int, float)) or duration < 0:
                raise ToolError("duration must be a non-negative number")
            argv = ["run"]
            if arguments.get("mode"):
                if arguments["mode"] not in ("debug", "release"):
                    raise ToolError("mode must be debug or release")
                argv += ["--mode", arguments["mode"]]
            argv += ["--duration", str(duration)]
            if arguments.get("headless", True):
                argv.append("--headless")
            if arguments.get("leaveRunning", True):
                argv.append("--leave-running")
            if arguments.get("noReload"):
                argv.append("--no-reload")
        elif name == "gobo_screenshot":
            project = self.project(arguments.get("project"))
            out = arguments.get("out") or "debug/shot.png"
            if not isinstance(out, str):
                raise ToolError("out must be a string")
            argv = ["screenshot", "--out", out]
            if arguments.get("headless", True):
                argv.append("--headless")
            if arguments.get("noReload"):
                argv.append("--no-reload")
            elif arguments.get("noBuild"):
                argv.append("--no-build")
            delay = arguments.get("delay", 0)
            if not isinstance(delay, (int, float)) or delay < 0:
                raise ToolError("delay must be a non-negative number of milliseconds")
            if delay:
                argv += ["--delay", str(int(delay))]
            validate_args(argv, project)  # path containment for --out
        elif name == "gobo_session":
            project = self.project(arguments.get("project"))
            session_file = arguments.get("file")
            if not isinstance(session_file, str) or not session_file:
                raise ToolError("file is required")
            argv = ["session", "--file", session_file]
            if arguments.get("headless"):
                argv.append("--headless")
            if arguments.get("bundle"):
                argv.append("--bundle")
            if arguments.get("artifacts"):
                argv += ["--artifacts", str(arguments["artifacts"])]
            validate_args(argv, project)
        elif name == "gobo_doctor":
            project = self.project(arguments.get("project"))
            argv = ["doctor"]
        elif name == "gobo_close":
            project = self.project(arguments.get("project"))
            argv = ["close"]
        elif name == "gobo_gsbridge":
            project = self.project(arguments.get("project"))
            argv = validate_args(arguments.get("args"), project, BRIDGE_VERBS)
            result = run_bridge(project, argv, timeout)
            with self.lock:
                self.last = {k: result[k] for k in ("project", "argv", "code", "timedOut", "durationMs", "finishedAt")}
            return result
        else:
            raise ToolError(f"unknown tool: {name}")

        result = run_gsdev(project, argv, timeout)
        with self.lock:
            self.last = {k: result[k] for k in ("project", "argv", "code", "timedOut", "durationMs", "finishedAt")}
        return result

    # -- JSON-RPC -----------------------------------------------------------
    def handle(self, message: object) -> dict | None:
        if not isinstance(message, dict):
            return self.error(None, -32600, "invalid request")
        method = message.get("method")
        msg_id = message.get("id")
        if not isinstance(method, str):
            # A response to a server-initiated request; we send none.
            return None
        try:
            if method == "initialize":
                params = message.get("params") or {}
                requested = params.get("protocolVersion")
                protocol = requested if isinstance(requested, str) and len(requested) >= 8 else DEFAULT_PROTOCOL
                return self.result(msg_id, {
                    "protocolVersion": protocol,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"listChanged": False},
                    },
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "gobo-agent harness. Use gobo_run to build/run a project in a real "
                        "browser, then gobo_gsdev for get/set/session/profile, and gobo_close "
                        "when done. gobo_gsbridge runs the same verbs against a live Scratch "
                        "page (production/embed, editor, github, localhost). Project paths are "
                        "relative to the server root."
                    ),
                })
            if method in ("notifications/initialized", "notifications/cancelled"):
                return None
            if method == "ping":
                return self.result(msg_id, {})
            if method == "tools/list":
                return self.result(msg_id, {"tools": TOOLS})
            if method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                try:
                    payload = self.call_tool(name, arguments)
                    text = json.dumps(payload, ensure_ascii=False)
                    return self.result(msg_id, {
                        "content": [{"type": "text", "text": text}],
                        "isError": payload.get("code") != 0,
                    })
                except ToolError as error:
                    return self.result(msg_id, {
                        "content": [{"type": "text", "text": json.dumps({"error": str(error)})}],
                        "isError": True,
                    })
            if method == "resources/list":
                return self.result(msg_id, {"resources": [{
                    "uri": "gobo://status",
                    "name": "gobo-agent server status",
                    "mimeType": "application/json",
                }]})
            if method == "resources/read":
                params = message.get("params") or {}
                if params.get("uri") != "gobo://status":
                    return self.error(msg_id, -32602, f"unknown resource: {params.get('uri')}")
                return self.result(msg_id, {"contents": [{
                    "uri": "gobo://status",
                    "mimeType": "application/json",
                    "text": json.dumps(self.status_payload(), ensure_ascii=False),
                }]})
            return self.error(msg_id, -32601, f"method not found: {method}")
        except Exception as error:  # noqa: BLE001 - JSON-RPC must never crash the loop
            return self.error(msg_id, -32603, str(error))

    def result(self, msg_id: object, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def error(self, msg_id: object, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_batch(server: Server, message: object) -> object | None:
    if isinstance(message, list):
        responses = [r for r in (server.handle(m) for m in message) if r is not None]
        return responses or None
    return server.handle(message)


def serve_stdio(server: Server) -> int:
    stream = sys.stdin
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = None if line is None else server.error(None, -32700, "parse error")
            print(json.dumps(response), flush=True)
            continue
        response = handle_batch(server, message)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


class HttpHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        sys.stderr.write("[gobo-mcp] " + (fmt % args) + "\n")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in ("http://localhost", "http://127.0.0.1") or origin.startswith(
            ("http://localhost:", "http://127.0.0.1:"))

    def _send_json(self, payload: object, status: int = 200, session: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("MCP-Protocol-Version", DEFAULT_PROTOCOL)
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        if not self._origin_ok():
            self._send_json({"error": "forbidden origin"}, status=403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            message = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            return
        server: Server = self.server.gobo_server  # type: ignore[attr-defined]
        session = self.headers.get("Mcp-Session-Id")
        if isinstance(message, dict) and message.get("method") == "initialize":
            session = uuid.uuid4().hex
            server.sessions[session] = True
        if server.sessions and session not in server.sessions:
            self._send_json({"error": "unknown session"}, status=404)
            return
        response = handle_batch(server, message)
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            return
        self._send_json(response, session=session)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        # No server-initiated messages: the spec allows 405 here.
        self.send_response(405)
        self.send_header("Allow", "POST, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib signature
        session = self.headers.get("Mcp-Session-Id")
        server: Server = self.server.gobo_server  # type: ignore[attr-defined]
        if session:
            server.sessions.pop(session, None)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


def serve_http(server: Server, host: str, port: int) -> int:
    httpd = ThreadingHTTPServer((host, port), HttpHandler)
    httpd.gobo_server = server  # type: ignore[attr-defined]
    sys.stderr.write(f"[gobo-mcp] streamable-http on http://{host}:{port}/mcp\n")
    sys.stderr.flush()

    def stop(_signum, _frame) -> None:
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    httpd.serve_forever()
    httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    # MCP stdio is UTF-8; a Windows console default (cp1252) would corrupt
    # non-ASCII in tool output when the client redirects our streams.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="gobo_mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                        help="Directory projects resolve against (default: the repo root).")
    parser.add_argument("--http", metavar="HOST:PORT",
                        help="Serve MCP Streamable HTTP at /mcp instead of stdio.")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"gobo-mcp: not a directory: {root}", file=sys.stderr)
        return 2
    server = Server(root)

    if args.http:
        if ":" not in args.http:
            print("gobo-mcp: --http wants HOST:PORT", file=sys.stderr)
            return 2
        host, _, port_text = args.http.rpartition(":")
        return serve_http(server, host or "127.0.0.1", int(port_text))
    return serve_stdio(server)


if __name__ == "__main__":
    raise SystemExit(main())
