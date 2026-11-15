import base64
import json
import os
import socket
import stat
import struct
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

CLIENT_INFO = {"name": "bellhop", "title": "bellhop", "version": "1"}
STARTING_ERRORS = ("is empty", "not materialized")
MESSAGE_LIMIT = 4 << 20
CLOSE, PING, PONG = 8, 9, 10
FIN = MASKED = 0x80
LENGTH_16, LENGTH_64 = 126, 127
HEAD, HEAD_16, HEAD_64 = 2, 4, 10


class RpcError(Exception):
    pass


class Unwritten(ConnectionError):
    pass


@dataclass(frozen=True)
class Turn:
    state: Literal["idle", "starting", "running", "waiting", "unavailable"]
    id: str | None = None
    status: str | None = None


IDLE, STARTING = Turn("idle"), Turn("starting")
WAITING_FLAGS = frozenset({"waitingOnApproval", "waitingOnUserInput"})


def reason(error: BaseException) -> str:
    return str(error.strerror or error) if isinstance(error, OSError) else str(error)


def masked(data: bytes, mask: bytes) -> bytes:
    return bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def owned_socket(path: Path) -> Path:
    target = path.resolve()
    found, parent = os.lstat(target), os.stat(target.parent)
    uid = os.getuid()
    if not stat.S_ISSOCK(found.st_mode) or found.st_uid != uid:
        raise ConnectionError(f"{target} is not a socket owned by uid {uid}")
    if parent.st_uid != uid or parent.st_mode & 0o022:
        raise ConnectionError(f"{target.parent} is not a private directory of uid {uid}")
    return target


def handshake(key: str) -> bytes:
    return (
        "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode()


def frame(payload: bytes, mask: bytes, opcode: int = 1) -> bytes:
    size = len(payload)
    if size < LENGTH_16:
        head = bytes([FIN | opcode, MASKED | size])
    elif size < 1 << 16:
        head = bytes([FIN | opcode, MASKED | LENGTH_16]) + struct.pack(">H", size)
    else:
        head = bytes([FIN | opcode, MASKED | LENGTH_64]) + struct.pack(">Q", size)
    return head + mask + masked(payload, mask)


def frame_length(buffer: bytes) -> tuple[int, int] | None:
    marker = buffer[1] & 0x7F
    if marker == LENGTH_16:
        return (
            (struct.unpack(">H", buffer[2:HEAD_16])[0], HEAD_16) if len(buffer) >= HEAD_16 else None
        )
    if marker == LENGTH_64:
        return (
            (struct.unpack(">Q", buffer[2:HEAD_64])[0], HEAD_64) if len(buffer) >= HEAD_64 else None
        )
    return marker, HEAD


def unframe(buffer: bytes) -> tuple[bool, int, bytes, bytes] | None:
    length = frame_length(buffer) if len(buffer) >= HEAD else None
    if length is None:
        return None
    size, position = length
    mask = buffer[position : position + 4] if buffer[1] & MASKED else b""
    position += len(mask)
    if len(buffer) < position + size:
        return None
    data = (
        masked(buffer[position : position + size], mask)
        if mask
        else buffer[position : position + size]
    )
    return bool(buffer[0] & FIN), buffer[0] & 0x0F, data, buffer[position + size :]


def request(request_id: int, method: str, params: Mapping[str, object]) -> dict[str, object]:
    return {"id": request_id, "method": method, "params": dict(params)}


def text_input(text: str) -> list[dict[str, object]]:
    return [{"type": "text", "text": text, "text_elements": []}]


def active_turn(turns: Mapping[str, object]) -> Turn:
    data = turns.get("data")
    rows = data if isinstance(data, list) else []
    running = next(
        (
            t["id"]
            for t in rows
            if isinstance(t, dict)
            and t.get("status") == "inProgress"
            and isinstance(t.get("id"), str)
            and t["id"]
        ),
        None,
    )
    return Turn("running", running) if running is not None else STARTING


def thread_status(read: Mapping[str, object]) -> Turn | None:
    thread = read.get("thread")
    status = thread.get("status") if isinstance(thread, dict) else None
    kind = status.get("type") if isinstance(status, dict) else None
    if kind == "idle":
        return IDLE
    if kind != "active" or not isinstance(status, dict):
        return Turn("unavailable", status=kind if isinstance(kind, str) else None)
    flags = status.get("activeFlags")
    names = {flag for flag in flags if isinstance(flag, str)} if isinstance(flags, list) else set()
    return Turn("waiting") if names & WAITING_FLAGS else None


@dataclass
class Client:
    path: Path
    deadline: float
    sock: socket.socket = field(default_factory=lambda: socket.socket(socket.AF_UNIX))
    buffer: bytes = field(default=b"", init=False)
    next_id: int = field(default=1, init=False)

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("app server reply deadline passed")
        return left

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> None:
        self.sock.settimeout(self.remaining())
        self.sock.connect(str(owned_socket(self.path)))
        self.sock.sendall(handshake(base64.b64encode(os.urandom(16)).decode()))
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self.receive()
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"websocket upgrade refused: {head[:80]!r}")

    def receive(self) -> bytes:
        self.sock.settimeout(self.remaining())
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("app server closed the connection")
        if len(self.buffer) + len(chunk) > MESSAGE_LIMIT:
            raise ConnectionError(f"app server reply exceeds {MESSAGE_LIMIT} bytes")
        return chunk

    def send(self, message: Mapping[str, object]) -> None:
        self.sock.settimeout(self.remaining())
        self.sock.sendall(frame(json.dumps(message).encode(), os.urandom(4)))

    def message(self) -> dict[str, object]:
        parts: list[bytes] = []
        while sum(map(len, parts)) <= MESSAGE_LIMIT:
            parsed = unframe(self.buffer)
            if parsed is None:
                self.buffer += self.receive()
                continue
            fin, opcode, data, self.buffer = parsed
            if opcode == CLOSE:
                raise ConnectionError("app server sent close")
            if opcode == PING:
                self.sock.sendall(frame(data, os.urandom(4), opcode=PONG))
            elif opcode in (0, 1):
                parts.append(data)
                if fin:
                    return decoded(b"".join(parts))
        raise ConnectionError(f"app server reply exceeds {MESSAGE_LIMIT} bytes")

    def call(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        request_id = self.next_id
        self.next_id += 1
        try:
            self.send(request(request_id, method, params))
        except OSError as error:
            raise Unwritten(reason(error)) from error
        while True:
            reply = self.message()
            if "method" not in reply and reply.get("id") == request_id:
                failure = reply.get("error")
                if failure is not None:
                    detail = (
                        failure.get("message", failure) if isinstance(failure, dict) else failure
                    )
                    raise RpcError(str(detail))
                result = reply.get("result")
                return result if isinstance(result, dict) else {}

    def initialize(self) -> None:
        self.call(
            "initialize", {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": True}}
        )
        self.send({"method": "initialized"})

    def loaded(self) -> list[str]:
        data = self.call("thread/loaded/list", {}).get("data")
        return [str(item) for item in data] if isinstance(data, list) else []

    def preview(self, thread_id: str) -> str:
        read = self.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = read.get("thread")
        preview = thread.get("preview") if isinstance(thread, dict) else None
        return preview if isinstance(preview, str) else ""

    def turn(self, thread_id: str) -> Turn:
        try:
            known = thread_status(
                self.call("thread/read", {"threadId": thread_id, "includeTurns": False})
            )
            if known is not None:
                return known
            turns = self.call(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            )
        except RpcError as error:
            if any(marker in str(error) for marker in STARTING_ERRORS):
                return STARTING
            raise
        return active_turn(turns)

    def start_turn(self, thread_id: str, text: str) -> str:
        result = self.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": text_input(text),
                "clientUserMessageId": str(uuid.uuid4()),
            },
        )
        turn = result.get("turn")
        return str(turn.get("id", "")) if isinstance(turn, dict) else ""

    def steer(self, thread_id: str, turn_id: str, text: str) -> str:
        result = self.call(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": text_input(text),
                "clientUserMessageId": str(uuid.uuid4()),
            },
        )
        return str(result.get("turnId", turn_id))

    def close(self) -> None:
        self.sock.close()


def decoded(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data)
    except RecursionError as error:
        raise ValueError("app server reply nests too deeply") from error
    return value if isinstance(value, dict) else {}


def connect(path: Path, timeout: float) -> Client:
    client = Client(path, time.monotonic() + timeout)
    try:
        client.open()
        client.initialize()
    except BaseException:
        client.close()
        raise
    return client
