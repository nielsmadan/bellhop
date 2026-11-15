from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from bellhop.config import Config
from bellhop.errors import BellhopError
from bellhop.models import FAKE_ROUTES_ENV, Session
from bellhop.paths import configured_dir
from bellhop.routes import RouteFinder, RouteSetup, endpoint_routes, route_finder
from bellhop.routes.endpoint import EndpointRoute, Pongs
from bellhop.runtime import resolve_runtime_dir
from bellhop.transcripts import ReaderFinder, ReaderSetup, reader_finder
from bellhop.transcripts.common import Reported

PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
CODEX_HOME_ENV = "CODEX_HOME"


@dataclass(frozen=True)
class Wiring:
    routes: RouteFinder
    readers: ReaderFinder


def pi_agent_dir(env: Mapping[str, str]) -> Path | None:
    return configured_dir(env, PI_AGENT_DIR_ENV, ".pi/agent")


def codex_home(env: Mapping[str, str]) -> Path | None:
    return configured_dir(env, CODEX_HOME_ENV, ".codex")


def endpoint_transcript(routes: Mapping[str, EndpointRoute]) -> Reported:
    def reported(session: Session) -> Path | None:
        route = routes.get(session.harness)
        try:
            return route.transcript(session) if route is not None else None
        except BellhopError:
            return None

    return reported


def wire(env: Mapping[str, str], config: Callable[[], Config]) -> Wiring:
    runtime = cache(lambda: resolve_runtime_dir(env, config()))
    fake = env.get(FAKE_ROUTES_ENV)
    codex = codex_home(env)
    pongs = Pongs()
    endpoints = endpoint_routes(runtime, pongs)
    routes = route_finder(
        RouteSetup(
            runtime_dir=runtime,
            fake_routes=Path(fake) if fake else None,
            codex_home=codex,
            pongs=pongs,
            endpoints=endpoints,
        )
    )
    reported = endpoint_transcript(endpoints)
    readers = reader_finder(
        ReaderSetup(pi_agent_dir(env), fake=bool(fake), reported=reported, codex_home=codex)
    )
    return Wiring(routes, readers)
