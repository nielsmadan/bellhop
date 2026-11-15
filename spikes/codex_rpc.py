# copied from _scratch/agent-comms/agentcomms/senders/codex_remote.py (WebSocket client, turn/start, busy check); ownership guard removed
from __future__ import annotations

import argparse
import base64
import fcntl
import importlib.util
import json
import os
import socket
import struct
import sys
import time
import uuid
from pathlib import Path

CLIENT_INFO = {"name": "agentcomms", "title": "agentcomms sender", "version": "0.1"}
IDLE_POLL_S = 0.5
LOCK_NAME = ".lock"
TURN_STARTING = "starting"
STARTING_ERRORS = ("is empty", "not materialized")






def rpc_request(req_id: int, method: str, params: dict) -> dict:
    return {"id": req_id, "method": method, "params": params}


def initialize_request(req_id: int) -> dict:
    return rpc_request(req_id, "initialize", {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": True}})


def text_input(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "text_elements": []}]


def turn_start_request(req_id: int, thread_id: str, text: str, message_id: str) -> dict:
    return rpc_request(req_id, "turn/start", {"threadId": thread_id, "input": text_input(text),
                                              "clientUserMessageId": message_id})


def turn_steer_request(req_id: int, thread_id: str, turn_id: str, text: str, message_id: str) -> dict:
    return rpc_request(req_id, "turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                                              "input": text_input(text), "clientUserMessageId": message_id})


def turns_list_request(req_id: int, thread_id: str) -> dict:
    return rpc_request(req_id, "thread/turns/list", {"threadId": thread_id, "limit": 1, "sortDirection": "desc",
                                                     "itemsView": "notLoaded"})


def read_request(req_id: int, thread_id: str) -> dict:
    return rpc_request(req_id, "thread/read", {"threadId": thread_id, "includeTurns": False})


def starting_error(message: str) -> bool:
    return any(marker in message for marker in STARTING_ERRORS)


def unsupported_reply(request: dict) -> dict:
    return {"id": request["id"], "error": {"code": -32601, "message": f"unsupported: {request.get('method')}"}}


def active_turn(turns_result: dict) -> str | None:
    turns = turns_result.get("data") or []
    return next((t["id"] for t in turns if t.get("status") == "inProgress"), None)


def turn_running(turns_result: dict, turn_id: str) -> bool:
    return any(t.get("id") == turn_id and t.get("status") == "inProgress" for t in turns_result.get("data") or [])


def thread_active(read_result: dict) -> bool:
    return ((read_result.get("thread") or {}).get("status") or {}).get("type") == "active"


def ws_handshake(key: str, host: str = "localhost") -> bytes:
    return (f"GET / HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()


def ws_frame(payload: bytes, mask: bytes, opcode: int = 1) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def ws_parse(buf: bytes) -> tuple[bool, int, bytes, bytes] | None:
    if len(buf) < 2:
        return None
    fin, opcode, n, pos = bool(buf[0] & 0x80), buf[0] & 0x0F, buf[1] & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, pos = struct.unpack(">H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, pos = struct.unpack(">Q", buf[2:10])[0], 10
    masked = buf[1] & 0x80
    mask = buf[pos:pos + 4] if masked else b""
    pos += 4 if masked else 0
    if len(buf) < pos + n:
        return None
    data = buf[pos:pos + n]
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return fin, opcode, data, buf[pos + n:]


class Client:
    def __init__(self, path: str, timeout_s: float = 10.0):
        self.timeout_s = timeout_s
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(timeout_s)
        self.sock.connect(path)
        self.buf = b""
        self.next_id = 1
        self.sock.sendall(ws_handshake(base64.b64encode(os.urandom(16)).decode()))
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("app server closed the websocket handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"websocket upgrade refused: {head[:80]!r}")

    def send(self, message: dict) -> None:
        self.sock.sendall(ws_frame(json.dumps(message).encode(), os.urandom(4)))

    def frame(self, deadline: float) -> tuple[bool, int, bytes]:
        while True:
            parsed = ws_parse(self.buf)
            if parsed is not None:
                fin, opcode, data, self.buf = parsed
                return fin, opcode, data
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("app server reply deadline passed")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("app server closed the connection")
            self.buf += chunk

    def recv(self, deadline: float) -> dict:
        parts: list[bytes] = []
        while True:
            fin, opcode, data = self.frame(deadline)
            if opcode == 8:
                raise ConnectionError("app server sent close")
            if opcode == 9:
                self.sock.sendall(ws_frame(data, os.urandom(4), opcode=10))
            elif opcode in (0, 1):
                parts.append(data)
                if fin:
                    return json.loads(b"".join(parts))

    def call(self, build, *args) -> dict:
        req_id = self.next_id
        self.next_id += 1
        self.send(build(req_id, *args))
        deadline = time.monotonic() + self.timeout_s
        while True:
            message = self.recv(deadline)
            if "method" in message and "id" in message:
                self.send(unsupported_reply(message))
            elif message.get("id") == req_id:
                if "error" in message:
                    raise RuntimeError(f"{message['error'].get('message', message['error'])}")
                return message.get("result") or {}

    def initialize(self) -> dict:
        result = self.call(initialize_request)
        self.send({"method": "initialized"})
        return result

    def busy_turn(self, thread_id: str) -> str | None:
        try:
            if not thread_active(self.call(read_request, thread_id)):
                return None
            return active_turn(self.call(turns_list_request, thread_id)) or TURN_STARTING
        except RuntimeError as e:
            if starting_error(str(e)):
                return TURN_STARTING
            raise

    def close(self) -> None:
        self.sock.close()


class WrittenTimeout(TimeoutError):
    pass


def written_call(client: Client, make, *args):
    try:
        return client.call(make, *args)
    except TimeoutError as e:
        raise WrittenTimeout(f"request written, no reply: {e}") from e


def deliver(client: Client, thread_id: str, text: str, mode: str, wait_s: float) -> str:
    message_id = str(uuid.uuid4())
    deadline = time.monotonic() + wait_s
