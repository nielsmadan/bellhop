from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from bellhop.models import Binding, PushResult, Session
from bellhop.routes.fake import fake_finder
from bellhop.routes.pi import PiRoute


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


def route_finder(setup: RouteSetup) -> RouteFinder:
    if setup.fake_routes is not None:
        fake = fake_finder(setup.fake_routes)
        return lambda session, binding: fake(session)
    pi = PiRoute(setup.runtime_dir)
    routes: Mapping[str, Route] = {"pi": pi}
    return lambda session, binding: routes.get(session.harness)
