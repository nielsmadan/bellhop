from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from bellhop.config import Config
from bellhop.errors import BellhopError
from bellhop.models import FAKE_ROUTES_ENV, Session
from bellhop.paths import home
from bellhop.routes import RouteFinder, RouteSetup, route_finder
from bellhop.routes.pi import PiRoute
from bellhop.runtime import resolve_runtime_dir
from bellhop.transcripts import ReaderFinder, ReaderSetup, reader_finder
from bellhop.transcripts.pi import Reported

PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"


@dataclass(frozen=True)
class Wiring:
    routes: RouteFinder
    readers: ReaderFinder


def pi_agent_dir(env: Mapping[str, str]) -> Path | None:
    value = env.get(PI_AGENT_DIR_ENV)
    if not value:
        return home(env) / ".pi" / "agent"
    if value == "~" or value.startswith("~/"):
        return home(env) / value[2:]
    path = Path(value)
    return path if path.is_absolute() else None


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
    routes = route_finder(RouteSetup(runtime_dir=runtime, fake_routes=Path(fake) if fake else None))
    reported = extension_transcript(PiRoute(runtime))
    readers = reader_finder(ReaderSetup(pi_agent_dir(env), fake=bool(fake), pi_reported=reported))
    return Wiring(routes, readers)
