import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from bellhop.codex_names import transcript_home
from bellhop.config import Config
from bellhop.envelope import message_id
from bellhop.errors import BellhopError
from bellhop.models import FAKE_ROUTES_ENV, Binding, PushResult, Session
from bellhop.paths import configured_dir, default_state
from bellhop.routes import Route, RouteFinder, RouteSetup, endpoint_routes, route_finder
from bellhop.routes.endpoint import EndpointRoute, Pongs
from bellhop.runtime import resolve_runtime_dir
from bellhop.trace import Emit, quiet
from bellhop.transcripts import ReaderFinder, ReaderSetup, TranscriptReader, reader_finder
from bellhop.transcripts.common import Reported

PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
CODEX_HOME_ENV = "CODEX_HOME"
PI_DEPTH = 2


@dataclass(frozen=True)
class Wiring:
    routes: RouteFinder
    readers: ReaderFinder
    untraced: RouteFinder


type Probed = dict[str, tuple[bool, str | None]]


def why_not(route: Route, session: Session) -> str:
    try:
        return route.obstacle(session)
    except BellhopError as error:
        return str(error)


@dataclass(frozen=True)
class TracedRoute:
    route: Route
    emit: Emit = field(compare=False)
    probed: Probed = field(compare=False)
    clock: Callable[[], float] = field(default=time.monotonic, compare=False)

    @property
    def name(self) -> str:
        return self.route.name

    @property
    def can_interrupt(self) -> bool:
        return self.route.can_interrupt

    @property
    def queues_until_idle(self) -> bool:
        return self.route.queues_until_idle

    def note(self, session: Session, found: bool, reason: str | None) -> None:
        if self.probed.get(session.session_id) == (found, reason):
            return
        self.probed[session.session_id] = (found, reason)
        self.emit(
            "probe",
            session=session.session_id,
            harness=session.harness,
            route=self.name,
            reachable=found,
            reason=reason,
        )

    def reachable(self, session: Session) -> bool:
        try:
            found = self.route.reachable(session)
        except BellhopError as error:
            self.note(session, False, str(error))
            raise
        self.note(session, found, None if found else why_not(self.route, session))
        return found

    def obstacle(self, session: Session) -> str:
        return self.route.obstacle(session)

    def replaced(self, session: Session) -> bool:
        return self.route.replaced(session)

    def busy(self, session: Session) -> bool | None:
        return self.route.busy(session)

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
        started = self.clock()
        fields: dict[str, object] = {
            "message": message_id(text),
            "session": session.session_id,
            "harness": session.harness,
            "route": self.name,
            "interrupt": interrupt,
            "phase": session.phase,
        }
        try:
            result = self.route.push(session, text, interrupt=interrupt)
        except Exception as error:
            self.emit("push", **fields, outcome="raised", detail=f"{type(error).__name__}: {error}")
            raise
        took = round((self.clock() - started) * 1000)
        self.emit("push", **fields, outcome=result.outcome, detail=result.detail, ms=took)
        return result


@dataclass(frozen=True)
class TracedReader:
    reader: TranscriptReader
    emit: Emit = field(compare=False)

    def path(self, session: Session) -> Path | None:
        return self.reader.path(session)

    def size(self, session: Session) -> int | None:
        return self.reader.size(session)

    def contains(
        self,
        session: Session,
        prefix: str,
        *,
        since: int,
        path: Path | None = None,
        prompted: bool = False,
    ) -> bool:
        found = self.reader.contains(session, prefix, since=since, path=path, prompted=prompted)
        if found:
            self.emit(
                "seen",
                message=message_id(prefix),
                session=session.session_id,
                harness=session.harness,
                transcript=str(path or self.reader.path(session) or ""),
            )
        return found


def traced_routes(routes: RouteFinder, emit: Emit) -> RouteFinder:
    probed: Probed = {}

    def find(session: Session, binding: Binding | None) -> Route | None:
        route = routes(session, binding)
        return TracedRoute(route, emit, probed) if route is not None else None

    return find


def traced_readers(readers: ReaderFinder, emit: Emit) -> ReaderFinder:
    def find(session: Session, binding: Binding | None) -> TranscriptReader | None:
        reader = readers(session, binding)
        return TracedReader(reader, emit) if reader is not None else None

    return find


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


def session_homes(session: Session) -> dict[str, str]:
    path = session.transcript_path
    if path is None:
        return {}
    parents = path.parents
    codex = transcript_home(session)
    if codex is not None:
        return {CODEX_HOME_ENV: str(codex)}
    if session.harness == "pi" and len(parents) > PI_DEPTH and parents[1].name == "sessions":
        return {PI_AGENT_DIR_ENV: str(parents[PI_DEPTH])}
    return {}


def homes_env(
    env: Mapping[str, str],
    config: Callable[[], Config],
    session: Session,
    binding: Binding | None,
) -> Mapping[str, str]:
    agent = config().agents.get(binding.agent) if binding is not None else None
    if agent is None:
        return {**env, **session_homes(session)}
    home = {"HOME": env["HOME"]} if "HOME" in env else {}
    return home | dict(agent.env)


def fake_routes(env: Mapping[str, str]) -> str | None:
    fake = env.get(FAKE_ROUTES_ENV)
    return fake if fake and not default_state(env) else None


def wire(env: Mapping[str, str], config: Callable[[], Config], emit: Emit = quiet) -> Wiring:
    runtime = cache(lambda: resolve_runtime_dir(env, config()))
    fake = fake_routes(env)
    pongs = Pongs()
    endpoints = endpoint_routes(runtime, pongs)
    routes = route_finder(
        RouteSetup(
            runtime_dir=runtime,
            fake_routes=Path(fake) if fake else None,
            pongs=pongs,
            endpoints=endpoints,
            codex_home=lambda session, binding: codex_home(
                homes_env(env, config, session, binding)
            ),
        )
    )
    reported = endpoint_transcript(endpoints)

    @cache
    def finder(pi_dir: Path | None, codex: Path | None) -> ReaderFinder:
        return reader_finder(
            ReaderSetup(pi_dir, fake=bool(fake), reported=reported, codex_home=codex)
        )

    def readers(session: Session, binding: Binding | None) -> TranscriptReader | None:
        homes = homes_env(env, config, session, binding)
        return finder(pi_agent_dir(homes), codex_home(homes))(session, binding)

    if emit is quiet:
        return Wiring(routes, readers, routes)
    return Wiring(traced_routes(routes, emit), traced_readers(readers, emit), routes)
