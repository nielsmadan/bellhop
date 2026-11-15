import contextlib
import json
import socket
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from bellhop.codex_rpc import FIN, LENGTH_16, LENGTH_64, MASKED, PING, PONG, unframe

type StartReply = Literal["ok", "error", "drop", "silent"]
SERVER_REQUEST = {"id": "srv-1", "method": "item/commandExecution/requestApproval", "params": {}}


def server_frame(payload: bytes, opcode: int = 1) -> bytes:
    size = len(payload)
    if size < LENGTH_16:
        return bytes([FIN | opcode, size]) + payload
    if size < 1 << 16:
        return bytes([FIN | opcode, LENGTH_16]) + struct.pack(">H", size) + payload
    return bytes([FIN | opcode, LENGTH_64]) + struct.pack(">Q", size) + payload


@dataclass
class FakeAppServer:
    path: Path
    loaded: list[str] = field(default_factory=list)
    running_turn: str | None = None
    start_reply: StartReply = "ok"
    steer_reply: StartReply = "ok"
    turns_listed: bool = True
    waiting: str | None = None
    raw_status: object = None
    raw_turns: object = None
    read_error: str | None = None
    steer_error: str | None = None
    chatter: bool = False
    close_after_upgrade: bool = False
    raw_initialize: bytes | None = None
    delay: float = 0.0
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    answered: list[dict[str, object]] = field(default_factory=list)
    pongs: list[bytes] = field(default_factory=list)
    unmasked: int = 0
    held: list[socket.socket] = field(default_factory=list)

    def reply(self, method: str, params: dict[str, object]) -> dict[str, object] | None:
        if method == "turn/start":
            return self.started()
        if method == "thread/read" and self.read_error is not None:
            return {"error": {"code": -32600, "message": self.read_error}}
        if method == "turn/steer" and self.steer_error is not None:
            return {"error": {"code": -32600, "message": self.steer_error}}
        if method == "turn/steer" and self.steer_reply in ("drop", "silent"):
            return None
        answers: dict[str, dict[str, object]] = {
            "initialize": {"result": {"userAgent": "fake"}},
            "thread/loaded/list": {"result": {"data": list(self.loaded)}},
            "thread/read": {"result": {"thread": {"status": self.thread_status()}}},
            "thread/turns/list": {"result": {"data": self.turns()}},
            "turn/steer": {"result": {"turnId": params.get("expectedTurnId")}},
        }
        unsupported: dict[str, object] = {
            "error": {"code": -32601, "message": f"unsupported: {method}"}
        }
        return answers.get(method, unsupported)

    def thread_status(self) -> object:
        if self.raw_status is not None:
            return self.raw_status
        flags = [self.waiting] if self.waiting else []
        return (
            {"type": self.status(), "activeFlags": flags} if self.running_turn else {"type": "idle"}
        )

    def status(self) -> str:
        return "active" if self.running_turn else "idle"

    def turns(self) -> object:
        if self.raw_turns is not None:
            return self.raw_turns
        listed = self.running_turn and self.turns_listed
        return [{"id": self.running_turn, "status": "inProgress"}] if listed else []

    def started(self) -> dict[str, object] | None:
        if self.start_reply == "error":
            return {"error": {"code": -32000, "message": "thread is closed"}}
        if self.start_reply in ("drop", "silent"):
            return None
        return {"result": {"turn": {"id": "turn-1"}}}

    def serve(self, conn: socket.socket) -> None:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buffer += chunk
        _, buffer = buffer.split(b"\r\n\r\n", 1)
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
        if self.close_after_upgrade:
            return
        while True:
            parsed = unframe(buffer)
            if parsed is None:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                continue
            if not buffer[1] & MASKED:
                self.unmasked += 1
                return
            _, opcode, data, buffer = parsed
            if opcode == PONG:
                self.pongs.append(data)
                continue
            message = json.loads(data)
            if "method" not in message:
                self.answered.append(message)
                continue
            if not self.respond(conn, message):
                return

    def respond(self, conn: socket.socket, message: dict[str, object]) -> bool:
        method = str(message["method"])
        params = message.get("params") or {}
        assert isinstance(params, dict)
        self.calls.append((method, params))
        if "id" not in message:
            return True
        if method == "initialize" and self.raw_initialize is not None:
            conn.sendall(server_frame(self.raw_initialize))
            return True
        answer = self.reply(method, params)
        if answer is None:
            if "drop" in (self.start_reply, self.steer_reply):
                return False
            self.held.append(conn)
            return True
        time.sleep(self.delay)
        if self.chatter:
            conn.sendall(server_frame(json.dumps(SERVER_REQUEST).encode()))
            conn.sendall(server_frame(b"are you there", opcode=PING))
        conn.sendall(server_frame(json.dumps({"id": message["id"], **answer}).encode()))
        return True

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls if method not in ("initialize", "initialized")]


@contextlib.contextmanager
def app_server(path: Path, **settings: object) -> Iterator[FakeAppServer]:
    fake = FakeAppServer(path, **settings)  # type: ignore[arg-type]
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(8)

    def accept() -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = listener.accept()
                threading.Thread(target=handle, args=(conn,), daemon=True).start()

    def handle(conn: socket.socket) -> None:
        with contextlib.suppress(OSError, ValueError):
            fake.serve(conn)
        if conn not in fake.held:
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield fake
    finally:
        listener.close()
        for conn in fake.held:
            conn.close()
        path.unlink(missing_ok=True)
