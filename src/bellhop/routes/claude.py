import contextlib
import json
import re
import socket
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from bellhop.codex_rpc import owned_socket, private_directory
from bellhop.errors import BellhopError
from bellhop.models import PushResult, Session
from bellhop.routes.unix import remaining

type Priority = Literal["later", "next"]

RELAY_ROUTE = "claude-relay"
SOCKET_ROUTE = "claude-socket"
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_CONFIG_DIR = ".claude"
INBOUND_SETTING = "crossSessionInbound"
INBOUND_ACCEPT = "accept"
SESSION_ID = re.compile(r"[0-9a-fA-F-]{8,64}")
WRITE_SECONDS = 1.0


def frame(token: str | None, text: str, priority: Priority, message_id: str) -> bytes:
    auth = [{"type": "auth", "token": token}] if token else []
    user = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "priority": priority,
        "msgV": 1,
        "msg_id": message_id,
    }
    return "".join(f"{json.dumps(line)}\n" for line in [*auth, user]).encode()


def send_frame(
    path: Path, text: str, priority: Priority, seconds: float, *, token: str | None
) -> str | None:
    payload = frame(token, text, priority, str(uuid.uuid4()))
    deadline = time.monotonic() + seconds
    try:
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(remaining(deadline))
            peer.connect(str(path))
            peer.settimeout(remaining(deadline))
            peer.sendall(payload)
            with contextlib.suppress(OSError):
                peer.shutdown(socket.SHUT_WR)
    except OSError as error:
        return f"claude's messaging socket: {error.strerror or error}"
    return None


def read_object(path: Path) -> Mapping[str, object] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def accepts_inbound(claude_dir: Path) -> bool:
    settings = read_object(claude_dir / "settings.json")
    return settings is not None and settings.get(INBOUND_SETTING) == INBOUND_ACCEPT


def registered(claude_dir: Path, pid: int) -> Mapping[str, object] | None:
    entry = read_object(claude_dir / "sessions" / f"{pid}.json")
    return entry if entry is not None and entry.get("pid") == pid else None


def session_of(entry: Mapping[str, object] | None) -> str | None:
    session = entry.get("sessionId") if entry is not None else None
    return session if isinstance(session, str) and SESSION_ID.fullmatch(session) else None


def own_socket(path: str, pid: int) -> bool:
    return Path(path).name == f"{pid}.sock"


def peer_token(claude_dir: Path, pid: int) -> str | None:
    keys = sorted((claude_dir / "sessions").glob(f"{pid}.*.key"))
    token = (read_object(keys[0]) or {}).get("peerToken") if keys else None
    return token if isinstance(token, str) and token else None


def shared(directory: Path) -> str | None:
    try:
        private_directory(directory)
    except FileNotFoundError:
        return None
    except OSError as error:
        return str(error.strerror or error)
    return None


@dataclass(frozen=True)
class Registered:
    claude_dir: Path
    pid: int
    entry: Mapping[str, object]


@dataclass(frozen=True)
class Peer:
    claude_dir: Path
    pid: int
    socket: Path


@dataclass(frozen=True)
class ClaudeSocketRoute:
    claude_dir: Callable[[Session], Path | None]
    name: str = field(default=SOCKET_ROUTE, init=False)
    can_interrupt: bool = field(default=True, init=False)
    queues_until_idle: bool = field(default=True, init=False)

    def accepting(self, session: Session) -> bool:
        directory = self.claude_dir(session)
        return directory is not None and accepts_inbound(directory)

    def refusal(self, session: Session) -> str | None:
        directory = self.claude_dir(session)
        if directory is None or accepts_inbound(directory):
            return None
        return f"{directory / 'settings.json'} does not accept peer messages"

    def registration(self, session: Session) -> Registered | str:
        directory = self.claude_dir(session)
        if directory is None or session.pid is None:
            return "no claude config directory or pid is known for this session"
        sessions = directory / "sessions"
        unsafe = shared(directory) or shared(sessions)
        if unsafe is not None:
            return unsafe
        refusal = self.refusal(session)
        if refusal is not None:
            return refusal
        entry = registered(directory, session.pid)
        if entry is None:
            return f"claude lists no session for pid {session.pid} in {sessions}"
        return Registered(directory, session.pid, entry)

    def peer(self, session: Session) -> Peer | str:
        found = self.registration(session)
        if isinstance(found, str):
            return found
        pid, current = found.pid, session_of(found.entry)
        if current is None:
            return f"claude pid {pid} lists no session id"
        if current != session.session_id:
            return f"claude pid {pid} now runs session {current}"
        path = found.entry.get("messagingSocketPath")
        if not isinstance(path, str) or not Path(path).is_absolute() or not own_socket(path, pid):
            return f"claude pid {pid} lists no messaging socket"
        try:
            return Peer(found.claude_dir, pid, owned_socket(Path(path)))
        except FileNotFoundError:
            return f"claude's messaging socket {path} is gone"
        except OSError as error:
            return f"claude's messaging socket cannot be used: {error.strerror or error}"

    def reachable(self, session: Session) -> bool:
        return isinstance(self.peer(session), Peer)

    def obstacle(self, session: Session) -> str:
        found = self.peer(session)
        return found if isinstance(found, str) else f"claude listens at {found.socket}"

    def replaced(self, session: Session) -> bool:
        found = self.registration(session)
        current = None if isinstance(found, str) else session_of(found.entry)
        return current is not None and current != session.session_id

    def busy(self, session: Session) -> bool | None:
        return None

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
        found = self.peer(session)
        if isinstance(found, str):
            return PushResult("busy", found)
        token = peer_token(found.claude_dir, found.pid)
        priority: Priority = "next" if interrupt else "later"
        failure = send_frame(found.socket, text, priority, WRITE_SECONDS, token=token)
        return PushResult("busy", failure) if failure else PushResult("accepted", priority)


class RelayRoute(Protocol):
    @property
    def name(self) -> str: ...

    def reachable(self, session: Session) -> bool: ...

    def obstacle(self, session: Session) -> str: ...

    def replaced(self, session: Session) -> bool: ...

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult: ...


@dataclass(frozen=True)
class Choice:
    route: RelayRoute | ClaudeSocketRoute
    reachable: bool


@dataclass(frozen=True)
class ClaudeRoute:
    relay: RelayRoute
    direct: ClaudeSocketRoute
    session: Session
    can_interrupt: bool = field(default=True, init=False)
    queues_until_idle: bool = field(default=True, init=False)
    choices: dict[str, Choice] = field(default_factory=dict, init=False, compare=False, repr=False)

    def choose(self, session: Session) -> Choice:
        try:
            if self.relay.reachable(session):
                return Choice(self.relay, True)
        except BellhopError:
            if self.direct.reachable(session):
                return Choice(self.direct, True)
            raise
        if self.direct.reachable(session):
            return Choice(self.direct, True)
        return Choice(self.direct if self.direct.accepting(session) else self.relay, False)

    def chosen(self, session: Session) -> Choice:
        if session.session_id not in self.choices:
            self.choices[session.session_id] = self.choose(session)
        return self.choices[session.session_id]

    @property
    def name(self) -> str:
        try:
            return self.chosen(self.session).route.name
        except BellhopError:
            return self.relay.name

    def accepting(self, session: Session) -> bool:
        return self.direct.accepting(session)

    def socket_obstacle(self, session: Session) -> str | None:
        return None if self.direct.reachable(session) else self.direct.obstacle(session)

    def reachable(self, session: Session) -> bool:
        return self.chosen(session).reachable

    def obstacle(self, session: Session) -> str:
        if self.direct.accepting(session):
            return self.direct.obstacle(session)
        reason, refusal = self.relay.obstacle(session), self.direct.refusal(session)
        return f"{reason}; {refusal}" if refusal is not None else reason

    def replaced(self, session: Session) -> bool:
        return self.direct.replaced(session) or self.relay.replaced(session)

    def busy(self, session: Session) -> bool | None:
        return None

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
        return self.chosen(session).route.push(session, text, interrupt=interrupt)
