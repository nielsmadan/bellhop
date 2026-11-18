import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bellhop.models import PushResult, Session
from bellhop.routes.unix import Pong, deliver_header, endpoint_path, exchange, ping

PING_SECONDS = 0.5
PONG_SECONDS = 1.0


@dataclass(frozen=True)
class EndpointKind:
    route: str
    piece: str
    old_wire: str
    unlisted: str | None = None
    setup: str = ""


def serves(pong: Pong | None, session: Session) -> bool:
    return pong is not None and pong.current and pong.session == session.session_id


def replaced_by(pong: Pong | None, session: Session) -> bool:
    return (
        pong is not None
        and pong.current
        and pong.session is not None
        and pong.session != session.session_id
    )


@dataclass
class Pongs:
    clock: Callable[[], float] = time.monotonic
    asked: dict[Path, tuple[float, Pong | None]] = field(default_factory=dict)

    def ask(self, path: Path) -> Pong | None:
        now = self.clock()
        fresh = {p: seen for p, seen in self.asked.items() if now - seen[0] < PONG_SECONDS}
        if path not in fresh:
            fresh[path] = (now, ping(path, timeout=PING_SECONDS))
        self.asked = fresh
        return fresh[path][1]


@dataclass(frozen=True)
class EndpointRoute:
    harness: str
    name: str
    runtime_dir: Callable[[], Path]
    pongs: Pongs = field(default_factory=Pongs, compare=False)
    kind: EndpointKind | None = field(default=None, compare=False)
    can_interrupt: bool = field(default=True, init=False)
    queues_until_idle: bool = field(default=True, init=False)

    def socket(self, session: Session) -> Path | None:
        if session.pid is None:
            return None
        return endpoint_path(self.runtime_dir(), self.harness, session.pid)

    def pong(self, session: Session) -> Pong | None:
        path = self.socket(session)
        return self.pongs.ask(path) if path is not None else None

    def reachable(self, session: Session) -> bool:
        return serves(self.pong(session), session)

    def obstacle(self, session: Session) -> str:
        path = self.socket(session)
        piece = self.kind.piece if self.kind is not None else "endpoint"
        if path is None:
            return f"hooklinesinker reports no pid for this {self.harness} session"
        if not path.exists():
            setup = f"; {self.kind.setup}" if self.kind is not None and self.kind.setup else ""
            return f"no bellhop {piece} listens at {path}{setup}"
        pong = self.pongs.ask(path)
        if pong is None:
            return f"the {piece} at {path} does not answer"
        if not pong.current:
            fix = f"; {self.kind.old_wire}" if self.kind is not None else ""
            return f"the {piece} at {path} speaks another wire version{fix}"
        return f"the {piece} at {path} serves session {pong.session or 'none'}"

    def replaced(self, session: Session) -> bool:
        return replaced_by(self.pong(session), session)

    def transcript(self, session: Session) -> Path | None:
        pong = self.pong(session)
        return pong.transcript if pong is not None and serves(pong, session) else None

    def busy(self, session: Session) -> bool | None:
        return None

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
        path = self.socket(session)
        if path is None:
            return PushResult(
                "busy", f"hooklinesinker reports no pid for this {self.harness} session"
            )
        header = deliver_header("interrupt" if interrupt else "boundary", session.session_id, text)
        return exchange(path, header, text)
