import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from bellhop.errors import BellhopError
from bellhop.models import Binding, Harness, PushResult, Session
from bellhop.routes.claude import RELAY_ROUTE, ClaudeRoute, ClaudeSocketRoute
from bellhop.routes.codex import SESSION_SOCKETS, CodexRoute, Probes, daemon_socket
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

    def obstacle(self, session: Session) -> str: ...

    def replaced(self, session: Session) -> bool: ...

    def busy(self, session: Session) -> bool | None: ...

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult: ...


type RouteFinder = Callable[[Session, Binding | None], Route | None]


@dataclass(frozen=True)
class RouteSetup:
    runtime_dir: Callable[[], Path]
    fake_routes: Path | None = None
    codex_home: Callable[[Session, Binding | None], Path | None] = lambda session, binding: None
    pongs: Pongs = field(default_factory=Pongs)
    endpoints: Mapping[str, EndpointRoute] | None = None
    codex_probes: Probes = field(default_factory=Probes)
    claude_dir: Callable[[Session, Binding | None], Path | None] = lambda session, binding: None


ENDPOINTS: Mapping[Harness, EndpointKind] = {
    "pi": EndpointKind(
        PI_NAME,
        "extension",
        "export the integration again: bellhop integration export pi --to <dir>",
        UNLISTED_PI,
        setup="install the pi extension (`bellhop integration show pi`) and restart pi",
    ),
    "claude": EndpointKind(
        RELAY_ROUTE,
        "relay",
        "restart that claude so it starts the installed bellhop's relay",
        setup="start claude with bellhop's MCP config (`bellhop integration show claude`)",
    ),
}


def endpoint_routes(runtime_dir: Callable[[], Path], pongs: Pongs) -> dict[str, EndpointRoute]:
    return {
        harness: EndpointRoute(harness, kind.route, runtime_dir, pongs, kind)
        for harness, kind in ENDPOINTS.items()
    }


def codex_link(path: Path, uid: int) -> bool:
    try:
        return Path(os.readlink(path)).parent.name == f"codex-daemon-{uid}"
    except OSError:
        return False


def session_sockets(runtime_dir: Callable[[], Path]) -> tuple[Path, ...]:
    try:
        found = sorted(runtime_dir().glob(SESSION_SOCKETS))
    except (BellhopError, OSError):
        return ()
    uid = os.getuid()
    return tuple(path for path in found if codex_link(path, uid))


def route_finder(setup: RouteSetup) -> RouteFinder:
    if setup.fake_routes is not None:
        fake = fake_finder(setup.fake_routes)
        return lambda session, binding: fake(session)
    endpoints = setup.endpoints or endpoint_routes(setup.runtime_dir, setup.pongs)

    def find(session: Session, binding: Binding | None) -> Route | None:
        if session.harness == "claude" and "claude" in endpoints:
            direct = ClaudeSocketRoute(lambda target: setup.claude_dir(target, binding))
            return ClaudeRoute(endpoints["claude"], direct, session)
        if session.harness in endpoints:
            return endpoints[session.harness]
        if session.harness == "codex":
            known = (
                Path(binding.app_server_socket)
                if binding is not None and binding.app_server_socket
                else daemon_socket(setup.codex_home(session, binding))
            )
            return CodexRoute(known, setup.codex_probes, lambda: session_sockets(setup.runtime_dir))
        return None

    return find
