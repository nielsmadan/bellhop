from dataclasses import dataclass, field
from pathlib import Path

from bellhop.codex_rpc import Client, RpcError, Turn, Unwritten, connect, reason
from bellhop.models import Binding, PushResult, Session

NAME = "codex-app-server"
PROBE_SECONDS = 0.5
PUSH_SECONDS = 2.0
CONTROL_SOCKET = Path("app-server-control") / "app-server-control.sock"
FAILURES = (OSError, RpcError, ValueError)


def daemon_socket(codex_home: Path | None) -> Path | None:
    return codex_home / CONTROL_SOCKET if codex_home is not None else None


def codex_socket(binding: Binding | None, codex_home: Path | None) -> Path | None:
    if binding is not None and binding.app_server_socket:
        return Path(binding.app_server_socket)
    return daemon_socket(codex_home)


@dataclass(frozen=True)
class CodexRoute:
    socket: Path | None
    name: str = field(default=NAME, init=False)
    can_interrupt: bool = field(default=True, init=False)
    queues_until_idle: bool = field(default=False, init=False)
    probed: dict[str, Turn] = field(default_factory=dict, init=False, compare=False)

    def reachable(self, session: Session) -> bool:
        if self.socket is None or not self.socket.exists():
            return False
        try:
            with connect(self.socket, PROBE_SECONDS) as client:
                if session.session_id not in client.loaded():
                    return False
                self.probed[session.session_id] = client.turn(session.session_id)
                return True
        except FAILURES:
            return False

    def replaced(self, session: Session) -> bool:
        return False

    def busy(self, session: Session) -> bool | None:
        probed = self.probed.pop(session.session_id, None)
        if probed is not None:
            return probed.state != "idle"
        if self.socket is None:
            return None
        try:
            with connect(self.socket, PROBE_SECONDS) as client:
                return client.turn(session.session_id).state != "idle"
        except FAILURES:
            return None

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
