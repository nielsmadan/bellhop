from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bellhop.models import PushResult, Session
from bellhop.routes.unix import Pong, deliver_header, endpoint_path, exchange, ping

NAME = "pi-extension"
PING_SECONDS = 0.5
UNLISTED_PI = (
    "hooklinesinker 1.5.1 lists Pi only when it runs through an executable named pi (ADR 0002)"
)


def serves(pong: Pong | None, session: Session) -> bool:
    if pong is None or not pong.current:
        return False
    return pong.session is None or pong.session == session.session_id


@dataclass(frozen=True)
class PiRoute:
    runtime_dir: Callable[[], Path]
    name: str = field(default=NAME, init=False)
    can_interrupt: bool = field(default=True, init=False)
    queues_until_idle: bool = field(default=True, init=False)

    def socket(self, session: Session) -> Path | None:
        if session.pid is None:
            return None
        return endpoint_path(self.runtime_dir(), "pi", session.pid)

    def pong(self, session: Session) -> Pong | None:
        path = self.socket(session)
        return ping(path, timeout=PING_SECONDS) if path is not None else None

    def reachable(self, session: Session) -> bool:
        return serves(self.pong(session), session)

    def replaced(self, session: Session) -> bool:
        pong = self.pong(session)
        return pong is not None and pong.current and pong.session not in (None, session.session_id)

    def transcript(self, session: Session) -> Path | None:
        pong = self.pong(session)
        return pong.transcript if pong is not None and serves(pong, session) else None

    def busy(self, session: Session) -> bool | None:
        return None

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
        path = self.socket(session)
        if path is None:
            return PushResult("busy", "hooklinesinker reports no pid for this pi session")
        header = deliver_header("interrupt" if interrupt else "boundary", session.session_id, text)
        return exchange(path, header, text)
