#!/usr/bin/env python3
"""Checks for tools/gobo_mcp.py, on both transports (stdio and Streamable HTTP).

    python tools/test_gobo_mcp.py

No browser: the calls under test are `doctor` and refusals. Stdio uses files for
the child's streams (this sandbox denies some pipe creation paths), and the HTTP
server is started on a free loopback port.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "tools" / "gobo_mcp.py"
PROJECT = "examples/project-1392704755"

failures = 0


def check(label: str, condition: bool, detail: object = "") -> None:
    global failures
    if condition:
        print(f"[ok  ] {label}")
    else:
        failures += 1
        print(f"[FAIL] {label}" + (f" - {detail}" if detail else ""))


def by_id(lines: list[dict], msg_id: int) -> dict | None:
    for message in lines:
        if message.get("id") == msg_id:
            return message
    return None


def call(server: subprocess.Popen, msg_id: int, method: str, params: dict | None = None) -> dict:
    request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        request["params"] = params
    server.stdin.write(json.dumps(request) + "\n")
    server.stdin.flush()
    line = server.stdout.readline()
    if not line:
        raise AssertionError(f"server closed before answering {method}")
    return json.loads(line)


def stdio_tests() -> None:
    work = ROOT / "debug" / "gobo-mcp-test"
    work.mkdir(parents=True, exist_ok=True)
    request_file = work / "requests.jsonl"
    output_file = work / "responses.jsonl"
    for stale in (request_file, output_file):
        stale.unlink(missing_ok=True)
    with request_file.open("w", encoding="utf-8") as out:
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "gobo_doctor", "arguments": {"project": PROJECT}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "gobo_gsdev", "arguments": {"args": ["frobnicate"]}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "gobo_gsdev",
                        "arguments": {"project": PROJECT, "args": ["session", "--file", "../../x.txt"]}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "gobo_gsdev",
                        "arguments": {"project": "../../outside", "args": ["get"]}}},
            {"jsonrpc": "2.0", "id": 7, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 8, "method": "resources/read", "params": {"uri": "gobo://status"}},
            {"jsonrpc": "2.0", "id": 9, "method": "ping"},
            {"jsonrpc": "2.0", "id": 10, "method": "nope/nothing"},
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": "gobo_gsbridge",
                        "arguments": {"project": PROJECT, "args": ["run", "--help"]}}},
            {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
             "params": {"name": "gobo_gsbridge", "arguments": {"args": ["setup"]}}},
            {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
             "params": {"name": "gobo_gsbridge",
                        "arguments": {"project": PROJECT, "args": ["screenshot", "--out", "../../x.png"]}}},
            {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
             "params": {"name": "gobo_gsbridge", "arguments": {"args": ["mouse", "--help"]}}},
            {"jsonrpc": "2.0", "id": 15, "method": "tools/call",
             "params": {"name": "gobo_gsbridge", "arguments": {"args": ["profile", "--help"]}}},
            {"jsonrpc": "2.0", "id": 16, "method": "tools/call",
             "params": {"name": "gobo_gsdev", "arguments": {"args": ["setup", "--help"]}}},
        ]
        for message in messages:
            out.write(json.dumps(message) + "\n")
    with request_file.open("r", encoding="utf-8") as fin, output_file.open("w", encoding="utf-8") as fout:
        code = subprocess.run(
            [sys.executable, str(SERVER), "--root", str(ROOT)],
            stdin=fin, stdout=fout, stderr=subprocess.DEVNULL,
            timeout=300, check=False).returncode
    lines = [json.loads(line) for line in output_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    shutil.rmtree(work, ignore_errors=True)

    check("stdio: exits 0", code == 0, code)
    init = by_id(lines, 1)
    check("stdio: initialize", init and init.get("result", {}).get("serverInfo", {}).get("name") == "gobo-agent-mcp",
          init)
    check("stdio: protocol echo", init and init["result"]["protocolVersion"] == "2025-06-18")
    tools = by_id(lines, 2)
    names = [t["name"] for t in (tools or {}).get("result", {}).get("tools", [])]
    check("stdio: seven tools", len(names) == 7, names)
    check("stdio: run tool present", "gobo_run" in names, names)
    check("stdio: gsbridge tool present", "gobo_gsbridge" in names, names)
    doctor = by_id(lines, 3)
    payload = json.loads(doctor["result"]["content"][0]["text"]) if doctor else {}
    check("stdio: doctor runs", doctor and doctor["result"]["isError"] is False and payload.get("code") == 0,
          payload.get("stderr", "")[-300:] if doctor else doctor)
    check("stdio: doctor output", "doctor - all dependencies found" in (payload.get("stdout") or ""))
    denied = by_id(lines, 4)
    check("stdio: rejects unknown verb", denied and denied["result"]["isError"] is True
          and "not allowed" in denied["result"]["content"][0]["text"])
    escape = by_id(lines, 5)
    check("stdio: rejects path escape", escape and escape["result"]["isError"] is True
          and "escapes" in escape["result"]["content"][0]["text"])
    outside = by_id(lines, 6)
    check("stdio: rejects outside project", outside and outside["result"]["isError"] is True
          and "outside the server root" in outside["result"]["content"][0]["text"])
    resources = by_id(lines, 7)
    check("stdio: resources list", resources and resources["result"]["resources"][0]["uri"] == "gobo://status")
    read = by_id(lines, 8)
    check("stdio: resource read", read and "root" in read["result"]["contents"][0]["text"])
    check("stdio: ping", by_id(lines, 9) and by_id(lines, 9)["result"] == {})
    unknown = by_id(lines, 10)
    check("stdio: unknown method", unknown and unknown["error"]["code"] == -32601)
    bridge_help = by_id(lines, 11)
    bridge_payload = json.loads(bridge_help["result"]["content"][0]["text"]) if bridge_help else {}
    check("stdio: gsbridge runs", bridge_help and bridge_payload.get("code") == 0,
          bridge_payload.get("stderr", "")[-300:] if bridge_help else bridge_help)
    check("stdio: gsbridge is the bridge", "target" in (bridge_payload.get("stdout") or "").lower(),
          (bridge_payload.get("stdout") or "")[:200])
    bridge_denied = by_id(lines, 12)
    check("stdio: gsbridge rejects setup", bridge_denied and bridge_denied["result"]["isError"] is True
          and "not allowed" in bridge_denied["result"]["content"][0]["text"])
    bridge_escape = by_id(lines, 13)
    check("stdio: gsbridge rejects path escape", bridge_escape and bridge_escape["result"]["isError"] is True
          and "escapes" in bridge_escape["result"]["content"][0]["text"])
    bridge_mouse = by_id(lines, 14)
    mouse_payload = json.loads(bridge_mouse["result"]["content"][0]["text"]) if bridge_mouse else {}
    check("stdio: gsbridge allows input verbs", mouse_payload.get("code") == 0
          and "usage: gsbridge mouse" in (mouse_payload.get("stdout") or ""),
          mouse_payload.get("stderr", "")[-200:] if bridge_mouse else bridge_mouse)
    bridge_profile = by_id(lines, 15)
    profile_payload = json.loads(bridge_profile["result"]["content"][0]["text"]) if bridge_profile else {}
    check("stdio: gsbridge allows profile", profile_payload.get("code") == 0
          and "usage: gsbridge profile" in (profile_payload.get("stdout") or ""),
          profile_payload.get("stderr", "")[-200:] if bridge_profile else bridge_profile)
    setup_help = by_id(lines, 16)
    setup_payload = json.loads(setup_help["result"]["content"][0]["text"]) if setup_help else {}
    check("stdio: toolchain verbs allowed", setup_payload.get("code") == 0
          and "usage: gsdev.py setup" in (setup_payload.get("stdout") or ""),
          setup_payload.get("stderr", "")[-200:] if setup_help else setup_help)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_post(port: int, payload: dict, session: str | None = None,
              origin: str | None = None) -> tuple[int, dict, dict]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp", data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
            return response.status, dict(response.headers), (json.loads(body) if body else {})
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        return error.code, dict(error.headers), (json.loads(body) if body else {})


def http_tests() -> None:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--root", str(ROOT), "--http", f"127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            check("http: server started", False)
            return
        check("http: server started", True)

        status, headers, init = http_post(port, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}})
        session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        check("http: initialize", status == 200 and init["result"]["serverInfo"]["name"] == "gobo-agent-mcp",
              init)
        check("http: session header", bool(session), headers)

        status, _, listed = http_post(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
        check("http: tools/list", status == 200 and len(listed["result"]["tools"]) == 7, status)

        status, _, doctor = http_post(port, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "gobo_doctor", "arguments": {"project": PROJECT}}}, session)
        payload = json.loads(doctor["result"]["content"][0]["text"]) if status == 200 else {}
        check("http: doctor runs", status == 200 and payload.get("code") == 0,
              payload.get("stderr", "")[-300:] if status == 200 else status)

        status, _, notification = http_post(port, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        check("http: notification accepted", status == 202, status)

        status, _, _ = http_post(port, {"jsonrpc": "2.0", "id": 4, "method": "ping"}, "unknown-session")
        check("http: unknown session rejected", status == 404, status)

        status, _, _ = http_post(port, {"jsonrpc": "2.0", "id": 5, "method": "ping"}, session,
                                 origin="http://evil.example")
        check("http: foreign origin rejected", status == 403, status)

        status, _, _ = http_post(port, {"jsonrpc": "2.0", "id": 6, "method": "ping"}, session)
        check("http: ping", status == 200, status)

        request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", method="DELETE",
                                         headers={"Mcp-Session-Id": session or ""})
        with urllib.request.urlopen(request, timeout=10) as response:
            check("http: session delete", response.status == 200, response.status)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    stdio_tests()
    http_tests()
    print("\nall gobo-mcp checks passed" if not failures else f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
