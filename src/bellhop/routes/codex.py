import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bellhop.codex_rpc import Client, RpcError, Turn, Unwritten, connect, reason
from bellhop.models import Binding, PushResult, Session

NAME = "codex-app-server"
PROBE_SECONDS = 0.5
PUSH_SECONDS = 2.0
CONTROL_SOCKET = Path("app-server-control") / "app-server-control.sock"
FAILURES = (OSError, RpcError, ValueError)
MEMO_SECONDS = 1.0


def daemon_socket(codex_home: Path | None) -> Path | None:
    return codex_home / CONTROL_SOCKET if codex_home is not None else None


def codex_socket(binding: Binding | None, codex_home: Path | None) -> Path | None:
    if binding is not None and binding.app_server_socket:
        return Path(binding.app_server_socket)
    return daemon_socket(codex_home)


@dataclass
class Probe:
    at: float
    names: frozenset[str] | None
    turns: dict[str, Turn | None] = field(default_factory=dict)


def read_turn(client: Client, thread: str) -> Turn | None:
    try:
        return client.turn(thread)
    except FAILURES:
        return None


@dataclass
class Probes:
    clock: Callable[[], float] = time.monotonic
    sockets: dict[Path, Probe] = field(default_factory=dict)

    def fresh(self, socket: Path) -> Probe | None:
        now = self.clock()
        self.sockets = {
            path: probe for path, probe in self.sockets.items() if now - probe.at < MEMO_SECONDS
        }
        return self.sockets.get(socket)

    def probe(self, socket: Path, thread: str, known: Probe | None) -> Probe:
        try:
            with connect(socket, PROBE_SECONDS) as client:
                names = known.names if known is not None else frozenset(client.loaded())
                turn = read_turn(client, thread) if names and thread in names else None
        except FAILURES:
            failed = Probe(self.clock(), None)
            self.sockets[socket] = failed
            return failed
        found = known or Probe(self.clock(), names)
        found.turns[thread] = turn
        self.sockets[socket] = found
        return found

    def names(self, socket: Path, thread: str) -> frozenset[str] | None:
        known = self.fresh(socket)
        return (known or self.probe(socket, thread, None)).names

    def turn(self, socket: Path, thread: str) -> Turn | None:
        known = self.fresh(socket)
        if known is not None and (known.names is None or thread not in known.names):
            return None
        if known is not None and thread in known.turns:
            return known.turns[thread]
        return self.probe(socket, thread, known).turns.get(thread)


@dataclass(frozen=True)
class CodexRoute:
    socket: Path | None
    probes: Probes = field(default_factory=Probes, compare=False)
    name: str = field(default=NAME, init=False)
    can_interrupt: bool = field(default=True, init=False)
    queues_until_idle: bool = field(default=False, init=False)

    def reachable(self, session: Session) -> bool:
        if self.socket is None or not self.socket.exists():
            return False
        names = self.probes.names(self.socket, session.session_id)
        return names is not None and session.session_id in names

    def replaced(self, session: Session) -> bool:
        return False

    def busy(self, session: Session) -> bool | None:
        if self.socket is None:
            return None
        turn = self.probes.turn(self.socket, session.session_id)
        return turn.state != "idle" if turn is not None else None

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
        if self.socket is None:
            return PushResult("busy", "no app server is known for this codex session")
        try:
            client = connect(self.socket, PUSH_SECONDS)
        except FAILURES as error:
            return PushResult("busy", f"app server not reachable: {reason(error)}")
        with client:
            return start(client, session.session_id, text, interrupt=interrupt)


def running_turn(client: Client, thread_id: str) -> Turn | PushResult:
    try:
        return client.turn(thread_id)
    except RpcError as error:
        return PushResult("busy", f"thread not ready: {error}")
    except (OSError, ValueError) as error:
        return PushResult("busy", f"app server stopped answering: {reason(error)}")


def start(client: Client, thread_id: str, text: str, *, interrupt: bool) -> PushResult:
    turn = running_turn(client, thread_id)
    if isinstance(turn, PushResult):
        return turn
    if turn.state == "starting":
        return PushResult("busy", "a turn is starting")
    if turn.state == "waiting":
        return PushResult("busy", "a dialog is open in the session")
    if turn.state == "unavailable":
        return PushResult("busy", f"the thread's status is {turn.status or 'unknown'}")
    if turn.state == "running" and not interrupt:
        return PushResult("busy", f"turn {turn.id} is running")
    if turn.id is not None:
        return steer(client, thread_id, turn.id, text)
    return send_turn(client, thread_id, text)


def steer(client: Client, thread_id: str, turn_id: str, text: str) -> PushResult:
    try:
        return PushResult("accepted", f"turn/steer {client.steer(thread_id, turn_id, text)}")
    except RpcError as error:
        return PushResult("busy", f"turn/steer refused: {error}")
    except Unwritten as error:
        return PushResult("busy", f"the write did not finish: {error}")
    except TimeoutError:
        return PushResult("unknown", f"no reply within {PUSH_SECONDS:g}s")
    except (OSError, ValueError) as error:
        return PushResult("unknown", f"connection dropped after the write: {reason(error)}")


def send_turn(client: Client, thread_id: str, text: str) -> PushResult:
    try:
        return PushResult("accepted", f"turn/start {client.start_turn(thread_id, text)}".strip())
    except RpcError as error:
        return PushResult("rejected", str(error))
    except Unwritten as error:
        return PushResult("busy", f"the write did not finish: {error}")
    except TimeoutError:
        return PushResult("unknown", f"no reply within {PUSH_SECONDS:g}s")
    except (OSError, ValueError) as error:
        return PushResult("unknown", f"connection dropped after the write: {reason(error)}")
