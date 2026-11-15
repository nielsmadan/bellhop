from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from bellhop.models import Binding, PushResult, Session
from bellhop.routes.codex import CodexRoute, codex_socket
from bellhop.routes.fake import fake_finder
from bellhop.routes.pi import PiRoute, Pongs


class Route(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def can_interrupt(self) -> bool: ...

    @property
    def queues_until_idle(self) -> bool: ...

    def reachable(self, session: Session) -> bool: ...

    def replaced(self, session: Session) -> bool: ...

    def busy(self, session: Session) -> bool | None: ...

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult: ...


type RouteFinder = Callable[[Session, Binding | None], Route | None]


@dataclass(frozen=True)
class RouteSetup:
    runtime_dir: Callable[[], Path]
    fake_routes: Path | None = None
    codex_home: Path | None = None
    pi_pongs: Pongs = field(default_factory=Pongs)


def route_finder(setup: RouteSetup) -> RouteFinder:
    if setup.fake_routes is not None:
        fake = fake_finder(setup.fake_routes)
        return lambda session, binding: fake(session)
    pi = PiRoute(setup.runtime_dir, setup.pi_pongs)

    def find(session: Session, binding: Binding | None) -> Route | None:
        if session.harness == "pi":
            return pi
        if session.harness == "codex":
            return CodexRoute(codex_socket(binding, setup.codex_home))
        return None

    return find
