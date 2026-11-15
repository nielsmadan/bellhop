from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from bellhop.config import Config
from bellhop.errors import BellhopError
from bellhop.models import FAKE_ROUTES_ENV, Binding, Session
from bellhop.paths import configured_dir
from bellhop.routes import RouteFinder, RouteSetup, endpoint_routes, route_finder
from bellhop.routes.endpoint import EndpointRoute, Pongs
from bellhop.runtime import resolve_runtime_dir
from bellhop.transcripts import ReaderFinder, ReaderSetup, TranscriptReader, reader_finder
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


def homes_env(
    env: Mapping[str, str], config: Callable[[], Config], binding: Binding | None
) -> Mapping[str, str]:
    agent = config().agents.get(binding.agent) if binding is not None else None
    if agent is None:
        return env
    home = {"HOME": env["HOME"]} if "HOME" in env else {}
    return home | dict(agent.env)


def wire(env: Mapping[str, str], config: Callable[[], Config]) -> Wiring:
    runtime = cache(lambda: resolve_runtime_dir(env, config()))
    fake = env.get(FAKE_ROUTES_ENV)
    pongs = Pongs()
    endpoints = endpoint_routes(runtime, pongs)
    routes = route_finder(
        RouteSetup(
            runtime_dir=runtime,
            fake_routes=Path(fake) if fake else None,
            pongs=pongs,
            endpoints=endpoints,
            codex_home=lambda binding: codex_home(homes_env(env, config, binding)),
        )
    )
    reported = endpoint_transcript(endpoints)

    @cache
    def finder(pi_dir: Path | None, codex: Path | None) -> ReaderFinder:
        return reader_finder(
            ReaderSetup(pi_dir, fake=bool(fake), reported=reported, codex_home=codex)
        )

    def readers(session: Session, binding: Binding | None) -> TranscriptReader | None:
        homes = homes_env(env, config, binding)
        return finder(pi_agent_dir(homes), codex_home(homes))(session, binding)

    return Wiring(routes, readers)
