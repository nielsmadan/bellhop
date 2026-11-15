from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from bellhop.models import Binding, Harness, PushResult, Session
from bellhop.routes.claude import NAME as CLAUDE_NAME
from bellhop.routes.codex import CodexRoute, Probes, codex_socket
from bellhop.routes.endpoint import EndpointKind, EndpointRoute, Pongs
from bellhop.routes.fake import fake_finder
from bellhop.routes.pi import NAME as PI_NAME
from bellhop.routes.pi import UNLISTED_PI


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
    pongs: Pongs = field(default_factory=Pongs)
    endpoints: Mapping[str, EndpointRoute] | None = None
    codex_probes: Probes = field(default_factory=Probes)


ENDPOINTS: Mapping[Harness, EndpointKind] = {
    "pi": EndpointKind(
        PI_NAME,
        "extension",
        "export the integration again: bellhop integration export pi --to <dir>",
        UNLISTED_PI,
    ),
    "claude": EndpointKind(
        CLAUDE_NAME, "relay", "restart that claude so it starts the installed bellhop's relay"
    ),
}


def endpoint_routes(runtime_dir: Callable[[], Path], pongs: Pongs) -> dict[str, EndpointRoute]:
    return {
        harness: EndpointRoute(harness, kind.route, runtime_dir, pongs)
        for harness, kind in ENDPOINTS.items()
    }


def route_finder(setup: RouteSetup) -> RouteFinder:
    if setup.fake_routes is not None:
        fake = fake_finder(setup.fake_routes)
        return lambda session, binding: fake(session)
    endpoints = setup.endpoints or endpoint_routes(setup.runtime_dir, setup.pongs)

    def find(session: Session, binding: Binding | None) -> Route | None:
        if session.harness in endpoints:
            return endpoints[session.harness]
        if session.harness == "codex":
            return CodexRoute(codex_socket(binding, setup.codex_home), setup.codex_probes)
        return None

    return find
