import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal, TextIO

from bellhop.config import load_config
from bellhop.errors import BellhopError
from bellhop.identity import ancestors, process_table
from bellhop.paths import configured_dir
from bellhop.routes.unix import MISMATCH, WIRE, endpoint_path
from bellhop.runtime import resolve_runtime_dir
from bellhop.transcripts.claude import transcript_of

type Priority = Literal["later", "next"]
type Forward = Callable[[str, Priority], str | None]
type Bounded = Callable[[str, Priority, float], str | None]
type Registry = Callable[[Path, int], Mapping[str, object] | None]

SERVER_INFO = {"name": "bellhop", "version": "1"}
CHILD_SOCKET, CHILD_TOKEN = "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN"
START_SESSION = "CLAUDE_CODE_SESSION_ID"
CONFIG_DIR = "CLAUDE_CONFIG_DIR"
PRIORITIES: Mapping[str, Priority] = {"boundary": "later", "interrupt": "next"}
DIGITS = re.compile(r"[0-9]+")
SESSION_ID = re.compile(r"[0-9a-fA-F-]{8,64}")
REQUEST_LIMIT = 64 * 1024
REQUEST_SECONDS = 5.0
FORWARD_SECONDS = 1.0
WATCH_SECONDS = 1.0
CLAIM_SECONDS = 1.0
REGISTRY_WAIT_SECONDS = 10.0
ANCESTOR_LIMIT = 4
HANDLERS = 16
ACCEPT_BACKOFF_SECONDS = 0.1
SHUTDOWN_GRACE_SECONDS = 5.0
ANSWER_BY_SECONDS = 1.5
TURN_AWAY_SECONDS = 0.1
AT_CAPACITY = b"busy relay is at capacity\n"


@dataclass(frozen=True)
class Request:
    header: str
    body: str
    body_bytes: int


@dataclass(frozen=True)
class SessionInfo:
    session: str | None
    transcript: Path | None


@dataclass(frozen=True)
class Channel:
    socket: str
    token: str = field(repr=False)


def parse_request(raw: bytes) -> Request:
    head, _, body = raw.partition(b"\n")
    header = head.decode("utf-8", errors="replace").strip()
    return Request(header, body.decode("utf-8", errors="replace"), len(body))


def pong(info: SessionInfo) -> str:
    transcript = str(info.transcript) if info.transcript is not None else None
    fields = {"wire": WIRE, "session": info.session, "transcript": transcript}
    return f"ok pong {json.dumps(fields, separators=(',', ':'))}"


def deliver_reply(
    when: str, target: str, count: str, request: Request, info: SessionInfo, forward: Forward
) -> str:
    if when not in PRIORITIES:
        return f"error unknown header {request.header}"
    if not DIGITS.fullmatch(count):
        return "error wire_version"
    if int(count) != request.body_bytes:
        return "error truncated"
    if info.session is None or target != info.session:
        return f"error {MISMATCH} {info.session or 'unknown'}"
    if not request.body:
        return "error empty message"
    priority = PRIORITIES[when]
    failure = forward(request.body, priority)
    return f"ok {priority}" if failure is None else f"busy {failure}"


def reply(request: Request, info: SessionInfo, forward: Forward) -> str:
    match request.header.split():
        case ["ping"]:
            return pong(info)
        case ["deliver", when, target, count]:
            return deliver_reply(when, target, count, request, info, forward)
        case ["deliver", when] | ["deliver", when, _] if when in PRIORITIES:
            return "error wire_version"
        case _:
            return f"error unknown header {request.header}"


def frame(token: str, text: str, priority: Priority, message_id: str) -> bytes:
    auth = {"type": "auth", "token": token}
    user = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "priority": priority,
        "msgV": 1,
        "msg_id": message_id,
    }
    return f"{json.dumps(auth)}\n{json.dumps(user)}\n".encode()


def remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("forward deadline passed")
    return left


def forwarder(channel: Channel) -> Bounded:
    def forward(text: str, priority: Priority, seconds: float) -> str | None:
        if seconds <= 0:
            return "the relay ran out of time before forwarding"
        payload = frame(channel.token, text, priority, str(uuid.uuid4()))
        deadline = time.monotonic() + min(seconds, FORWARD_SECONDS)
        try:
            with socket.socket(socket.AF_UNIX) as peer:
                peer.settimeout(remaining(deadline))
                peer.connect(channel.socket)
                peer.settimeout(remaining(deadline))
                peer.sendall(payload)
                with contextlib.suppress(OSError):
                    peer.shutdown(socket.SHUT_WR)
        except OSError as error:
            return f"claude's messaging socket: {error.strerror or error}"
        return None

    return forward


def belongs_to(channel: Channel, pid: int) -> bool:
    return Path(channel.socket).name == f"{pid}.sock"


def child_channel(env: Mapping[str, str]) -> Channel | None:
    path, token = env.get(CHILD_SOCKET), env.get(CHILD_TOKEN)
    return Channel(path, token) if path and token else None


def registry_path(claude_dir: Path, pid: int) -> Path:
    return claude_dir / "sessions" / f"{pid}.json"


def session_info(entry: Mapping[str, object] | None, claude_dir: Path) -> SessionInfo:
    session = entry.get("sessionId") if entry is not None else None
    cwd = entry.get("cwd") if entry is not None else None
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        return SessionInfo(None, None)
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return SessionInfo(session, None)
    return SessionInfo(session, transcript_of(claude_dir, cwd, session))


def read_registry(claude_dir: Path, pid: int) -> Mapping[str, object] | None:
    try:
        entry = json.loads(registry_path(claude_dir, pid).read_text())
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) and entry.get("pid") == pid else None


def current_session(claude_dir: Path, pid: int) -> SessionInfo:
    return session_info(read_registry(claude_dir, pid), claude_dir)


def enclosing_claude(
    claude_dir: Path,
    above: Sequence[int],
    started_as: str | None,
    read: Registry = read_registry,
) -> int | None:
    return next(
        (
            pid
            for pid in above
            if started_as is not None
            and (entry := read(claude_dir, pid)) is not None
            and entry.get("sessionId") == started_as
        ),
        None,
    )


def parent_chain(parent: int) -> list[int]:
    table = process_table() or {}
    chain = [proc.pid for proc in ancestors(parent, table)]
    return (chain or [parent])[:ANCESTOR_LIMIT]


def claude_pid(
    claude_dir: Path,
    chain: Sequence[int],
    started_as: str | None,
    *,
    read: Registry = read_registry,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parent, *above = chain
    deadline = clock() + REGISTRY_WAIT_SECONDS
    while clock() < deadline:
        if read(claude_dir, parent) is not None:
            return parent
        sleep(0.5)
    return enclosing_claude(claude_dir, above, started_as, read) or parent


def mcp_result(request_id: object, result: Mapping[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def mcp_reply(request: Mapping[str, object]) -> dict[str, object] | None:
    method, request_id = request.get("method"), request.get("id")
    if request_id is None:
        return None
    params = request.get("params")
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return mcp_result(
            request_id,
            {
                "protocolVersion": version or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "tools/list":
        return mcp_result(request_id, {"tools": []})
    if method == "ping":
        return mcp_result(request_id, {})
    failure = {"code": -32601, "message": f"method not found: {method}"}
    return {"jsonrpc": "2.0", "id": request_id, "error": failure}


def answer_mcp(lines: Iterable[str], out: TextIO) -> None:
    for line in lines:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        answer = mcp_reply(request) if isinstance(request, dict) else None
        if answer is not None:
            out.write(json.dumps(answer) + "\n")
            out.flush()


def same_file(descriptor: int, path: Path) -> bool:
    try:
        return os.fstat(descriptor).st_ino == os.stat(path, follow_symlinks=False).st_ino
    except OSError:
        return False


@dataclass
class Listener:
    path: Path
    lock: Path
    server: socket.socket | None = field(default=None, init=False)
    inode: int | None = field(default=None, init=False)
    held: int | None = field(default=None, init=False)
    closed: bool = field(default=False, init=False)
    closing: threading.RLock = field(default_factory=threading.RLock, init=False)

    def claim(self) -> bool:
        try:
            descriptor = os.open(self.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            return False
        if not same_file(descriptor, self.lock):
            os.close(descriptor)
            return False
        self.held = descriptor
        return True

    def bind(self) -> None:
        temporary = self.path.with_name(f".{secrets.token_hex(4)}.tmp")
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(temporary))
            os.chmod(temporary, 0o600)
            server.listen(socket.SOMAXCONN)
            os.replace(temporary, self.path)
        except BaseException:
            server.close()
            temporary.unlink(missing_ok=True)
            raise
        with self.closing:
            self.inode = self.path.stat().st_ino
            self.server = server
            if self.closed:
                self.close()

    def close(self) -> None:
        with self.closing:
            self.closed = True
            if self.server is not None:
                self.server.close()
                self.server = None
            with contextlib.suppress(OSError):
                if self.inode is not None and self.path.lstat().st_ino == self.inode:
                    self.path.unlink()
            self.inode = None
            if self.held is not None:
                with contextlib.suppress(OSError):
                    self.lock.unlink()
                os.close(self.held)
                self.held = None


def read_request(conn: socket.socket) -> bytes | None:
    deadline = time.monotonic() + REQUEST_SECONDS
    data = b""
    while True:
        conn.settimeout(remaining(deadline))
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk
        if len(data) > REQUEST_LIMIT:
            return None


def handle(
    conn: socket.socket,
    info: Callable[[], SessionInfo],
    forward: Bounded,
    slots: threading.BoundedSemaphore,
) -> None:
    answer_by = time.monotonic() + ANSWER_BY_SECONDS

    def in_time(text: str, priority: Priority) -> str | None:
        return forward(text, priority, answer_by - time.monotonic())

    try:
        with conn, contextlib.suppress(OSError):
            raw = read_request(conn)
            if raw is not None:
                conn.sendall((reply(parse_request(raw), info(), in_time) + "\n").encode())
    finally:
        slots.release()


def turn_away(conn: socket.socket) -> None:
    with conn, contextlib.suppress(OSError):
        conn.settimeout(TURN_AWAY_SECONDS)
        conn.sendall(AT_CAPACITY)


def serve(listener: Listener, info: Callable[[], SessionInfo], forward: Bounded) -> None:
    slots = threading.BoundedSemaphore(HANDLERS)
    while (server := listener.server) is not None:
        try:
            conn, _ = server.accept()
        except OSError as error:
            if listener.server is None or error.errno == errno.EBADF:
                return
            time.sleep(ACCEPT_BACKOFF_SECONDS)
            continue
        if not slots.acquire(blocking=False):
            turn_away(conn)
            continue
        try:
            threading.Thread(target=handle, args=(conn, info, forward, slots), daemon=True).start()
        except RuntimeError:
            conn.close()
            slots.release()


def watch_parent(
    parent: int,
    stop: Callable[[], None],
    *,
    current_parent: Callable[[], int] = os.getppid,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    while current_parent() == parent:
        sleep(WATCH_SECONDS)
    stop()


def note(text: str) -> None:
    print(f"bellhop: relay: {text}", file=sys.stderr, flush=True)


def claim_until_free(listener: Listener, stopping: threading.Event) -> bool:
    noted = False
    while not listener.claim():
        if not noted:
            note(f"another relay serves {listener.path.name}; waiting for it to go")
            noted = True
        if stopping.wait(CLAIM_SECONDS):
            return False
    return True


@dataclass
class Relay:
    env: Mapping[str, str] = field(repr=False)
    channel: Channel
    parent: int
    stopping: threading.Event = field(default_factory=threading.Event)
    listener: Listener | None = None

    def start(self) -> None:
        claude_dir = configured_dir(self.env, CONFIG_DIR, ".claude")
        if claude_dir is None:
            note(f"{CONFIG_DIR} must be an absolute path; not listening")
            return
        pid = claude_pid(claude_dir, parent_chain(self.parent), self.env.get(START_SESSION))
        if not belongs_to(self.channel, pid):
            note(f"the messaging socket here is not claude {pid}'s own; not listening")
            return
        try:
            directory = resolve_runtime_dir(self.env, load_config(self.env))
            path = endpoint_path(directory, "claude", pid)
        except BellhopError as error:
            note(str(error))
            return
        listener = Listener(path, path.with_suffix(".lock"))
        self.listener = listener
        if not claim_until_free(listener, self.stopping):
            return
        try:
            listener.bind()
        except OSError as error:
            note(f"cannot listen on {path}: {error.strerror or error}")
            listener.close()
            return
        info = partial(current_session, claude_dir, pid)
        threading.Thread(
            target=serve, args=(listener, info, forwarder(self.channel)), daemon=True
        ).start()

    def close(self) -> None:
        self.stopping.set()
        if self.listener is not None:
            self.listener.close()


def main(env: Mapping[str, str]) -> int:
    channel = child_channel(env)
    if channel is None:
        note(f"{CHILD_SOCKET} and {CHILD_TOKEN} are not set; nothing to deliver to")
        answer_mcp(sys.stdin, sys.stdout)
        return 0
    relay = Relay(env, channel, os.getppid())

    def stop(*_: object) -> None:
        relay.close()
        os._exit(0)

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, stop)
    starter = threading.Thread(target=relay.start, daemon=True)
    starter.start()
    threading.Thread(target=watch_parent, args=(relay.parent, stop), daemon=True).start()
    try:
        answer_mcp(sys.stdin, sys.stdout)
    except (OSError, ValueError):
        pass
    finally:
        relay.stopping.set()
        starter.join(REGISTRY_WAIT_SECONDS + SHUTDOWN_GRACE_SECONDS)
        relay.close()
    return 0
