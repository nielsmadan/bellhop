import contextlib
import json
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bellhop.codes import RUNTIME_DIR_UNSAFE
from bellhop.errors import BellhopError
from bellhop.models import PushOutcome, PushResult

type When = Literal["boundary", "interrupt"]

REPLY_SECONDS = 2.0
REPLY_LIMIT = 4096
WIRE = 2
MISMATCH = "session_mismatch"
SOCKET_PATH_LIMIT = 104
OUTCOMES: Mapping[str, PushOutcome] = {"ok": "accepted", "busy": "busy", "error": "rejected"}


def endpoint_path(directory: Path, harness: str, pid: int) -> Path:
    path = directory / f"{harness}-{pid}.sock"
    if len(str(path).encode()) >= SOCKET_PATH_LIMIT:
        raise BellhopError(
            f"runtime directory {directory} is too long for a socket path ({path})",
            code=RUNTIME_DIR_UNSAFE,
        )
    return path


@dataclass(frozen=True)
class Pong:
    wire: int | None
    session: str | None
    transcript: Path | None

    @property
    def current(self) -> bool:
        return self.wire == WIRE


def parse_reply(line: str) -> PushResult:
    word, _, detail = line.strip().partition(" ")
    outcome = OUTCOMES.get(word)
    if outcome is None:
        return PushResult("unknown", f"unrecognized reply: {line.strip()[:200]}")
    if outcome == "rejected" and detail.split(" ", 1)[0] == MISMATCH:
        return PushResult("busy", detail)
    return PushResult(outcome, detail)


def parse_pong(detail: str) -> Pong:
    word, _, payload = detail.partition(" ")
    try:
        fields = json.loads(payload) if word == "pong" else None
    except json.JSONDecodeError:
        fields = None
    if not isinstance(fields, dict):
        return Pong(None, None, None)
    wire, session, transcript = fields.get("wire"), fields.get("session"), fields.get("transcript")
    return Pong(
        wire if isinstance(wire, int) and not isinstance(wire, bool) else None,
        session if isinstance(session, str) and session else None,
        Path(transcript)
        if isinstance(transcript, str) and Path(transcript).is_absolute()
        else None,
    )


def deliver_header(when: When, session_id: str, text: str) -> str:
    return f"deliver {when} {session_id} {len(text.encode())}"


def remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    return left


def read_reply(conn: socket.socket, deadline: float, limit: int = REPLY_LIMIT) -> str:
    data = b""
    while b"\n" not in data and len(data) < limit:
        conn.settimeout(remaining(deadline))
        chunk = conn.recv(limit - len(data))
        if not chunk:
            break
        data += chunk
    return data.split(b"\n", 1)[0].decode("utf-8", errors="replace")


def write(conn: socket.socket, path: Path, payload: bytes, deadline: float) -> PushResult | None:
    try:
        conn.settimeout(remaining(deadline))
        conn.connect(str(path))
    except OSError as error:
        return PushResult("busy", f"endpoint not listening: {error.strerror or error}")
    try:
        conn.settimeout(remaining(deadline))
        conn.sendall(payload)
    except OSError as error:
        return PushResult("busy", f"the write did not finish: {error.strerror or error}")
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_WR)
    return None


def exchange(path: Path, header: str, text: str, *, timeout: float = REPLY_SECONDS) -> PushResult:
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX) as conn:
        unwritten = write(conn, path, f"{header}\n{text}".encode(), deadline)
        if unwritten is not None:
            return unwritten
        try:
            reply = read_reply(conn, deadline)
        except TimeoutError:
            return PushResult("unknown", f"no reply within {timeout:g}s")
        except OSError as error:
            return PushResult("unknown", f"connection dropped after the write: {error}")
    if not reply:
        return PushResult("unknown", "connection dropped after the write")
    return parse_reply(reply)


def ping(path: Path, *, timeout: float = REPLY_SECONDS) -> Pong | None:
    result = exchange(path, "ping", "", timeout=timeout)
    return parse_pong(result.detail) if result.outcome == "accepted" else None
