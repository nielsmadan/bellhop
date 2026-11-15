from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from bellhop.config import Config
from bellhop.errors import BellhopError
from bellhop.models import FAKE_ROUTES_ENV, Session
from bellhop.paths import home
from bellhop.routes import RouteFinder, RouteSetup, route_finder
from bellhop.routes.pi import PiRoute, Pongs
from bellhop.runtime import resolve_runtime_dir
from bellhop.transcripts import ReaderFinder, ReaderSetup, reader_finder
from bellhop.transcripts.pi import Reported

PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
CODEX_HOME_ENV = "CODEX_HOME"


@dataclass(frozen=True)
class Wiring:
    routes: RouteFinder
    readers: ReaderFinder


def configured_dir(env: Mapping[str, str], variable: str, default: str) -> Path | None:
    value = env.get(variable)
    if not value:
        return home(env) / default
    if value == "~" or value.startswith("~/"):
        return home(env) / value[2:]
    path = Path(value)
    return path if path.is_absolute() else None


def pi_agent_dir(env: Mapping[str, str]) -> Path | None:
    return configured_dir(env, PI_AGENT_DIR_ENV, ".pi/agent")


def codex_home(env: Mapping[str, str]) -> Path | None:
    return configured_dir(env, CODEX_HOME_ENV, ".codex")


def extension_transcript(route: PiRoute) -> Reported:
    def reported(session: Session) -> Path | None:
        try:
            return route.transcript(session)
        except BellhopError:
            return None

    return reported


def wire(env: Mapping[str, str], config: Callable[[], Config]) -> Wiring:
    runtime = cache(lambda: resolve_runtime_dir(env, config()))
    fake = env.get(FAKE_ROUTES_ENV)
    codex = codex_home(env)
    pongs = Pongs()
    routes = route_finder(
        RouteSetup(
            runtime_dir=runtime,
            fake_routes=Path(fake) if fake else None,
            codex_home=codex,
            pi_pongs=pongs,
        )
    )
    reported = extension_transcript(PiRoute(runtime, pongs))
    readers = reader_finder(
        ReaderSetup(pi_agent_dir(env), fake=bool(fake), pi_reported=reported, codex_home=codex)
    )
    return Wiring(routes, readers)
