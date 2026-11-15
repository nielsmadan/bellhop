import contextlib
import socket
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from bellhop.models import FAKE_ROUTES_ENV, PushOutcome, PushResult, Session
from bellhop.routes.fake import fake_finder

REPLY_SECONDS = 2.0
REPLY_LIMIT = 4096


class Route(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def can_interrupt(self) -> bool: ...

    @property
    def queues_until_idle(self) -> bool: ...

    def reachable(self, session: Session) -> bool: ...

    def busy(self, session: Session) -> bool | None: ...

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult: ...


type RouteFinder = Callable[[Session], Route | None]

OUTCOMES: Mapping[str, PushOutcome] = {"ok": "accepted", "busy": "busy", "error": "rejected"}


def parse_reply(line: str) -> PushResult:
    word, _, detail = line.strip().partition(" ")
    outcome = OUTCOMES.get(word)
    if outcome is None:
        return PushResult("unknown", f"unrecognized reply: {line.strip()[:200]}")
    return PushResult(outcome, detail)


def read_reply(conn: socket.socket, deadline: float, limit: int = REPLY_LIMIT) -> str:
    data = b""
    while b"\n" not in data and len(data) < limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        conn.settimeout(remaining)
        chunk = conn.recv(limit - len(data))
        if not chunk:
            break
        data += chunk
    return data.split(b"\n", 1)[0].decode("utf-8", errors="replace")


def endpoint_push(
    path: Path, header: str, text: str, *, timeout: float = REPLY_SECONDS
) -> PushResult:
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(timeout)
        try:
            conn.connect(str(path))
        except OSError as error:
            return PushResult("busy", f"endpoint not listening: {error.strerror or error}")
        try:
            conn.sendall(f"{header}\n{text}".encode())
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_WR)
            reply = read_reply(conn, deadline)
        except TimeoutError:
            return PushResult("unknown", f"no reply within {timeout:g}s")
        except OSError as error:
            return PushResult("unknown", f"connection dropped after the write: {error}")
    if not reply:
        return PushResult("unknown", "connection dropped after the write")
    return parse_reply(reply)


def route_finder(env: Mapping[str, str]) -> RouteFinder:
    config = env.get(FAKE_ROUTES_ENV)
    if config:
        return fake_finder(Path(config))
    return lambda session: None
