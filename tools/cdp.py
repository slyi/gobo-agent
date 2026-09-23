"""Minimal Chrome DevTools Protocol client over a hand-rolled WebSocket.

Only the Python standard library is used, so screenshots and runtime inspection
work on Windows and macOS without Node, Bun, or any pip package.

The TurboWarp Desktop editor is a Chromium app; when it is started with
--remote-debugging-port it exposes a CDP HTTP endpoint and a per-page WebSocket.
This module talks to that WebSocket directly.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

__all__ = ["CDPError", "CDP", "connect", "list_targets", "find_editor_target"]


class CDPError(RuntimeError):
    """Raised when the DevTools protocol returns an error or the socket fails."""


def list_targets(host: str = "127.0.0.1", port: int = 9223, timeout: float = 5.0) -> Any:
    url = f"http://{host}:{port}/json/list"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def find_editor_target(host: str = "127.0.0.1", port: int = 9223, timeout: float = 5.0) -> Optional[dict]:
    try:
        targets = list_targets(host, port, timeout)
    except Exception:
        return None
    for target in targets:
        if target.get("type") != "page":
            continue
        if not str(target.get("url", "")).startswith("tw-editor://"):
            continue
        if target.get("webSocketDebuggerUrl"):
            return target
    return None


class CDP:
    def __init__(self, ws_url: str, timeout: float = 10.0):
        parsed = urllib.parse.urlsplit(ws_url)
        if parsed.scheme != "ws":
            raise CDPError(f"unsupported websocket url: {ws_url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self._buffer = b""
        self._next_id = 0
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._handshake(host, port, path)

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake failed: connection closed")
            self._buffer += chunk
        head, _, rest = self._buffer.partition(b"\r\n\r\n")
        self._buffer = rest
        status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if "101" not in status:
            raise CDPError(f"websocket handshake failed: {status}")

    def _read_exact(self, count: int, deadline: float) -> bytes:
        data = b""
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("websocket read timed out")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(count - len(data))
            except socket.timeout:
                raise TimeoutError("websocket read timed out") from None
            if not chunk:
                raise CDPError("websocket connection closed")
            data += chunk
        return data

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_message(self, deadline: float) -> bytes:
        fragments = bytearray()
        started = False
        while True:
            first, second = self._read_exact(2, deadline)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            masked = bool(second & 0x80)
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8, deadline))[0]
            mask = self._read_exact(4, deadline) if masked else b""
            payload = self._read_exact(length, deadline) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
            if opcode == 0x9:  # ping
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise CDPError("websocket closed by peer")
            if opcode in (0x1, 0x2):
                fragments = bytearray(payload)
                started = True
            elif opcode == 0x0 and started:
                fragments += payload
            else:
                continue
            if final:
                return bytes(fragments)

    def _send_json(self, message: dict) -> None:
        self._send_frame(json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send_json({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            raw = self._recv_message(deadline)
            try:
                message = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise CDPError(f"{method}: {error.get('message', error)}")
            return message.get("result", {})

    def evaluate(self, expression: str, await_promise: bool = False, timeout: float = 30.0) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = (
                details.get("exception", {}).get("description")
                or details.get("text")
                or "evaluation failed"
            )
            raise CDPError(text)
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def connect(target: dict, timeout: float = 10.0) -> CDP:
    return CDP(target["webSocketDebuggerUrl"], timeout=timeout)
