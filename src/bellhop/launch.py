import fcntl
import os
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Literal

from bellhop.allowlist import passed
from bellhop.codes import INVALID_CONFIG, LAUNCH_FAILED, USAGE
from bellhop.codex_rpc import RpcError, connect
from bellhop.codex_session import session_argv
from bellhop.config import VARIABLE, AgentConfig
from bellhop.envelope import prefix, render
from bellhop.errors import BellhopError
from bellhop.models import Binding, Message, Session
from bellhop.paths import state_dir
from bellhop.readiness import Screen, classify
from bellhop.routes import RouteFinder
from bellhop.store import (
    LAUNCH_ROUTE,
    begin_start,
    fail_start,
    get_message,
    holds_waiting,
    launch_message,
    open_start,
)
from bellhop.tmux import Place, Tmux, TmuxTimeout, target_place, tmux_executable, tmux_target

STARTUP = timedelta(seconds=90)
STALE_START = STARTUP + timedelta(seconds=30)
START_FAILED = "start failed"
ENV_PROGRAM = "/usr/bin/env"
TERM = "tmux-256color"
DEFAULT_SERVER = ("codex", "app-server")
CLAUDE_MCP = ("_integrations", "claude", "bellhop-mcp.json")
THREAD_PROBE_SECONDS = 1.0

type Decision = Literal["start", "queue", "send", "fail"]
type StartCheck = Callable[[Binding, Sequence[Session], datetime], Progress]
type StartState = Literal["ready", "waiting", "failed"]


@dataclass(frozen=True)
class Start:
    argv: list[str]
    session_id: str | None
    socket: Path | None


@dataclass(frozen=True)
class Progress:
    state: StartState
    detail: str
    binding: Binding


def child_env(caller: Mapping[str, str], agent: AgentConfig) -> dict[str, str]:
    return passed(caller) | {"TERM": TERM} | dict(agent.env)


def scrubbed(env: Mapping[str, str], argv: Sequence[str]) -> list[str]:
    bad = [name for name in env if not VARIABLE.fullmatch(name)]
    if bad:
        raise BellhopError(f"'{bad[0]}' is not an environment variable name", code=INVALID_CONFIG)
    if not argv or "=" in argv[0]:
        raise BellhopError("an agent's command must start with a program path", code=INVALID_CONFIG)
    return [ENV_PROGRAM, "-i", *(f"{name}={env[name]}" for name in sorted(env)), *argv]


def claude_mcp_config() -> Path:
    return Path(str(files("bellhop").joinpath(*CLAUDE_MCP)))


def plan_start(
    agent: AgentConfig,
    prompt: str | None,
    *,
    runtime_dir: Callable[[], Path],
    python: str,
    token: Callable[[], str] = lambda: secrets.token_hex(4),
    new_id: Callable[[], str],
) -> Start:
    tail = [prompt] if prompt is not None else []
    match agent.harness:
        case "claude":
            session_id = new_id()
            end = ["--", *tail] if tail else []
            mcp = ["--mcp-config", str(claude_mcp_config())]
            return Start([*agent.command, "--session-id", session_id, *mcp, *end], session_id, None)
        case "pi":
            session_id = new_id()
            return Start([*agent.command, "--session-id", session_id, *tail], session_id, None)
        case "codex":
            if prompt is None:
                raise BellhopError(
                    f"'{agent.name}' is a codex agent; launch it with --prompt, since "
                    "hooklinesinker lists a codex thread only after its first turn",
                    code=USAGE,
                )
            socket = runtime_dir() / f"codex-{agent.name}-{token()}.sock"
            listen = f"unix://{socket}"
            server = [*(agent.server_command or DEFAULT_SERVER), "--listen", listen]
            tui = [*agent.command, "--remote", listen, *tail]
            wrapper = [python, "-I", "-m", "bellhop", *session_argv(socket, server, tui)]
            return Start(wrapper, None, socket)


def failed_start(binding: Binding) -> bool:
    return binding.state == "failed" and (binding.detail or "").startswith(START_FAILED)


def expired(binding: Binding, now: datetime) -> bool:
    return now - binding.updated_at > STARTUP


def decide(binding: Binding | None, *, retry: bool, server_alive: bool, now: datetime) -> Decision:
    if binding is None:
        return "start"
    if binding.state == "live":
        return "send"
    if binding.state == "starting":
        return "start" if retry and now - binding.updated_at > STALE_START else "queue"
    if failed_start(binding):
        return "start" if retry else "fail"
    if binding.harness == "codex" and server_alive:
        return "send"
    return "start"


@contextmanager
def agent_lock(env: Mapping[str, str], agent: str) -> Iterator[None]:
    directory = state_dir(env) / "launch"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(directory / f"{agent}.lock", flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def starting_binding(agent: AgentConfig, start: Start, *, cause: str, now: datetime) -> Binding:
    return Binding(
        agent=agent.name,
        harness=agent.harness,
        state="starting",
        session_id=start.session_id,
        pid=None,
        process_started_at=None,
        app_server_socket=str(start.socket) if start.socket is not None else None,
        app_server_pid=None,
        tmux_target=None,
        cause=cause,
        detail=None,
        updated_at=now,
    )


def working_dir(agent: AgentConfig, env: Mapping[str, str]) -> Path:
    return agent.cwd or Path(env.get("HOME", "/"))


def cannot_launch(agent: AgentConfig | None, env: Mapping[str, str]) -> str | None:
    if agent is None or not agent.command:
        return "it has no command to start it with"
    cwd = working_dir(agent, env)
    if not cwd.is_dir():
        return f"its cwd {cwd} is not a directory"
    if tmux_executable(env) is None:
        return "there is no tmux on PATH or at the system locations"
    return None


@dataclass(frozen=True)
class StartSetup:
    env: Mapping[str, str] = field(repr=False)
    tmux: Tmux
    runtime_dir: Callable[[], Path]
    python: str
    new_id: Callable[[], str]


def prompt_for(conn: sqlite3.Connection, message: Message | None) -> str | None:
    if message is None:
        return None
    waiting = holds_waiting(conn, message.target_kind, message.target)
    return render(message.id, message.sender, message.body, waiting)


def begin(
    conn: sqlite3.Connection,
    agent: AgentConfig,
    message: Message | None,
    *,
    cause: str,
    setup: StartSetup,
    now: datetime,
) -> tuple[Message | None, Binding | None]:
    cwd = working_dir(agent, setup.env)
    tmux = setup.tmux
    try:
        start = plan_start(
            agent,
            prompt_for(conn, message),
            runtime_dir=setup.runtime_dir,
            python=setup.python,
            new_id=setup.new_id,
        )
        argv = scrubbed(child_env(setup.env, agent), start.argv)
        name = tmux.free_name(agent.name)
    except BellhopError as error:
        raise CannotLaunch(str(error), code=error.code) from error
    launched = replace(message, route=LAUNCH_ROUTE) if message is not None else None
    stored, starting = begin_start(
        conn, starting_binding(agent, start, cause=cause, now=now), launched, now
    )
    if starting is None:
        return stored, None
    try:
        place = tmux.new_session(name, cwd, argv)
    except TmuxTimeout as error:
        found = located(tmux, name)
        if found is None:
            reason = f"{error}; it may have started: attach with: {tmux.attach_hint(name)}"
            raise refused(conn, starting, stored, reason, now, prompted=True) from error
        place = found
    except BellhopError as error:
        raise refused(conn, starting, stored, str(error), now, prompted=False) from error
    opened = replace(starting, tmux_target=tmux_target(place))
    open_start(conn, starting, opened)
    return stored, opened


class CannotLaunch(BellhopError):
    pass


def located(tmux: Tmux, name: str) -> Place | None:
    try:
        return tmux.locate(name)
    except BellhopError:
        return None


def refused(
    conn: sqlite3.Connection,
    starting: Binding,
    stored: Message | None,
    detail: str,
    now: datetime,
    *,
    prompted: bool,
) -> BellhopError:
    fail_start(conn, starting, failure(starting, detail, now).binding, now, prompted=prompted)
    latest = get_message(conn, stored.id) if stored is not None else None
    return BellhopError(
        f"cannot launch '{starting.agent}': {detail}", code=LAUNCH_FAILED, stored=latest
    )


def failure(binding: Binding, detail: str, now: datetime) -> Progress:
    failed = replace(binding, state="failed", detail=f"{START_FAILED}: {detail}", updated_at=now)
    return Progress("failed", failed.detail or "", failed)


def stalled(binding: Binding, waiting: str, hint: str, now: datetime) -> Progress:
    if expired(binding, now):
        detail = f"not ready after {STARTUP.seconds}s ({waiting}); attach with: {hint}"
        return failure(binding, detail, now)
    return Progress("waiting", waiting, binding)


def screen_failure(screen: Screen, hint: str) -> str | None:
    if screen.state == "auth":
        return f"stopped at a login screen ({screen.detail}); attach with: {hint}"
    if screen.state == "dialog":
        return f"stopped at the {screen.detail} screen; attach with: {hint}"
    return None


def ready_binding(binding: Binding, session: Session, now: datetime) -> Binding:
    codex = binding.harness == "codex"
    return replace(
        binding,
        state="live",
        session_id=session.session_id,
        pid=None if codex else session.pid,
        process_started_at=session.process_started_at,
        app_server_pid=session.pid if codex else None,
        detail=None,
        updated_at=now,
    )


def waiting_reason(session_id: str | None, listed: Session | None, screen: Screen) -> str:
    if screen.state == "permission":
        return "a permission prompt is open"
    if session_id is None:
        return "waiting for the codex app server to open the thread"
    if listed is None:
        return f"waiting for hooklinesinker to list {session_id}"
    return "waiting for its route to answer"


@dataclass(frozen=True)
class Look:
    pane: str | None
    hint: str
    sessions: Sequence[Session]
    thread: str | None
    reachable: Callable[[Session, Binding], bool]


def check_start(binding: Binding, look: Look, now: datetime) -> Progress:
    if look.pane is None:
        return failure(binding, "the agent's tmux pane closed before it was ready", now)
    session_id = binding.session_id or look.thread
    known = replace(binding, session_id=session_id)
    listed = next((s for s in look.sessions if s.session_id == session_id), None)
    if listed is not None:
        ready = ready_binding(known, listed, now)
        if look.reachable(listed, ready):
            return Progress("ready", "", ready)
    screen = classify(binding.harness, look.pane)
    stuck = screen_failure(screen, look.hint) if listed is None else None
    if stuck is not None:
        return failure(binding, stuck, now)
    return stalled(known, waiting_reason(session_id, listed, screen), look.hint, now)


def find_thread(socket: Path, message_id: str) -> str | None:
    wanted = prefix(message_id)
    try:
        with connect(socket, THREAD_PROBE_SECONDS) as client:
            return next(
                (thread for thread in client.loaded() if client.preview(thread).startswith(wanted)),
                None,
            )
    except (OSError, RpcError, ValueError):
        return None


def reachable(routes: RouteFinder, session: Session, binding: Binding) -> bool:
    route = routes(session, binding)
    try:
        return route is not None and route.reachable(session)
    except BellhopError:
        return False


def start_checker(
    conn: sqlite3.Connection, routes: RouteFinder, tmux: Callable[[str], Tmux | None]
) -> StartCheck:
    def check(binding: Binding, sessions: Sequence[Session], now: datetime) -> Progress:
        place = target_place(binding.tmux_target)
        if place is None:
            if expired(binding, now):
                return failure(binding, "its tmux session never opened", now)
            return Progress("waiting", "opening its tmux session", binding)
        server = tmux(place.socket)
        if server is None:
            return failure(
                binding, f"its tmux server {place.socket} is not the configured one", now
            )
        hint = server.attach_hint(place.name)
        try:
            captured = server.capture(place.pane)
        except BellhopError as error:
            return stalled(binding, f"tmux did not answer: {error}", hint, now)
        thread = None
        if binding.session_id is None and binding.app_server_socket:
            message = launch_message(conn, binding.agent)
            if message is not None:
                thread = find_thread(Path(binding.app_server_socket), message.id)
        look = Look(
            captured, hint, sessions, thread, lambda session, live: reachable(routes, session, live)
        )
        return check_start(binding, look, now)

    return check
