import fcntl
import os
import sqlite3
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, NamedTuple, Protocol

from bellhop.config import watched_config
from bellhop.delivery import Channel, confirm_if_seen, drainer_may_push, push_waiting
from bellhop.errors import HlsUnavailable
from bellhop.hls import list_sessions
from bellhop.identity import SESSION_VARS, ProcessTable, alive, process_table
from bellhop.launch import STARTUP, Progress, StartCheck, expired, failure, start_checker
from bellhop.models import Binding, Session, TargetKind
from bellhop.paths import state_dir
from bellhop.routes import Route, RouteFinder
from bellhop.store import (
    GONE_DETAIL,
    REPLACED_DETAIL,
    abandon_starts,
    awaiting_confirmation,
    bindings,
    expire_sessions,
    fail_pending,
    fail_start,
    finish_start,
    has_work,
    oldest_pending,
    open_store,
    pending_targets,
    save_binding_unless,
    sweep_sending,
    update_bindings,
)
from bellhop.targets import binding_for, follows_processes, live_session, refresh_bindings
from bellhop.tmux import tmux_for
from bellhop.transcripts import ReaderFinder
from bellhop.wiring import wire

TICK_SECONDS = 1.0
PACE = timedelta(seconds=3)
GONE_POLLS = 3
EXPIRY = timedelta(hours=24)
CONFIRM_WINDOW = timedelta(minutes=10)
STALE_CLAIM = timedelta(seconds=30)
BACKOFF_MAX_SECONDS = 10.0
BACKOFF_STEPS = 4
STDERR_FD = 2
LOCK_NAME = "drain.lock"
LOG_NAME = "drain.log"
LOG_LIMIT = 1 << 20
PRIVATE_FILE = 0o600
NO_FOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC
CALLER_ONLY_PREFIXES = ("CLAUDE_CODE_MESSAGING_",)


class Lock(Protocol):
    def try_acquire(self) -> bool: ...

    def release(self) -> None: ...


class DrainLock:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | NO_FOLLOW, PRIVATE_FILE)
        self.handle: IO[str] = os.fdopen(descriptor, "r+", encoding="utf-8")

    def try_acquire(self) -> bool:
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def record_owner(self) -> None:
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"{os.getpid()}\n")
        self.handle.flush()

    def release(self) -> None:
        fcntl.flock(self.handle, fcntl.LOCK_UN)

    def close(self) -> None:
        self.handle.close()


def no_starts(binding: Binding, sessions: Sequence[Session], now: datetime) -> Progress:
    return Progress("waiting", "nothing checks starts here", binding)


@dataclass(frozen=True)
class Deps:
    conn: sqlite3.Connection
    sessions: Callable[[], Sequence[Session]]
    routes: RouteFinder
    readers: ReaderFinder
    table: Callable[[], ProcessTable | None]
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    starts: StartCheck = no_starts


class Pushed(NamedTuple):
    at: datetime
    observed_at: datetime


@dataclass
class State:
    pushed: dict[str, Pushed] = field(default_factory=dict)
    absent: dict[str, int] = field(default_factory=dict)
    seen: dict[str, Session] = field(default_factory=dict)
    errors: dict[tuple[TargetKind, str], str] = field(default_factory=dict)
    failures: int = 0


@dataclass(frozen=True)
class View:
    present: Mapping[str, Session]
    bindings: Sequence[Binding]
    table: ProcessTable | None
    now: datetime


def follow_bindings(
    conn: sqlite3.Connection,
    sessions: Sequence[Session],
    table: ProcessTable | None,
    now: datetime,
) -> list[Binding]:
    return update_bindings(conn, lambda before: refresh_bindings(before, sessions, table, now))


def drain_work(conn: sqlite3.Connection, now: datetime) -> bool:
    return has_work(conn, now - CONFIRM_WINDOW)


def absences(
    targets: Sequence[tuple[TargetKind, str]], present: Mapping[str, Session], state: State
) -> dict[str, int]:
    return {
        name: state.absent.get(name, 0) + 1
        for kind, name in targets
        if kind == "session" and name not in present
    }


def needs_table(current: Sequence[Binding], absent: Mapping[str, int]) -> bool:
    return follows_processes(current) or any(count >= GONE_POLLS for count in absent.values())


def known_dead(last: Session | None, table: ProcessTable | None) -> bool:
    if last is None or last.pid is None or table is None:
        return False
    return not alive(last.pid, last.process_started_at, table)


def ended(deps: Deps, state: State, target: str, view: View, route: Route | None) -> str | None:
    if target in view.present:
        return (
            REPLACED_DETAIL if route is not None and route.replaced(view.present[target]) else None
        )
    last = state.seen.get(target)
    if state.absent.get(target, 0) < GONE_POLLS or last is None:
        return None
    if known_dead(last, view.table):
        return GONE_DETAIL
    found = deps.routes(last, binding_for(last, view.bindings))
    return REPLACED_DETAIL if found is not None and found.replaced(last) else None


def paced(session: Session, state: State, now: datetime) -> bool:
    last = state.pushed.get(session.session_id)
    return last is not None and session.observed_at <= last.observed_at and now - last.at < PACE


def _drain_target(deps: Deps, state: State, target: tuple[TargetKind, str], view: View) -> None:
    kind, name = target
    agent = next((b for b in view.bindings if b.agent == name), None) if kind == "agent" else None
    session = (
        view.present.get(name) if kind == "session" else live_session(agent, view.present.values())
    )
    binding = agent or (binding_for(session, view.bindings) if session is not None else None)
    route = deps.routes(session, binding) if session is not None else None
    detail = ended(deps, state, name, view, route) if kind == "session" else None
    if detail is not None:
        fail_pending(deps.conn, kind, name, detail, view.now)
        return
    message = oldest_pending(deps.conn, kind, name)
    if session is None or message is None or paced(session, state, view.now):
        return
    if route is None or not drainer_may_push(route, session, message.urgency):
        return
    if not route.reachable(session):
        return
    channel = Channel(route, deps.readers(session, binding))
    push_waiting(deps.conn, message, session, channel, view.now)
    state.pushed[session.session_id] = Pushed(view.now, session.observed_at)


def _report(state: State, target: tuple[TargetKind, str], error: Exception) -> None:
    text = f"{type(error).__name__}: {error}"
    if state.errors.get(target) != text:
        print(f"bellhop: drainer: {target[0]} {target[1]}: {text}", file=sys.stderr, flush=True)
    state.errors[target] = text


def first_transcript(deps: Deps, live: Binding, sessions: Sequence[Session]) -> Path | None:
    session = next((s for s in sessions if s.session_id == live.session_id), None)
    reader = deps.readers(session, live) if session is not None else None
    return reader.path(session) if reader is not None and session is not None else None


def _settle(
    deps: Deps, binding: Binding, progress: Progress, sessions: Sequence[Session], now: datetime
) -> None:
    if progress.state == "ready":
        transcript = first_transcript(deps, progress.binding, sessions)
        finish_start(deps.conn, binding, progress.binding, now, transcript)
    elif progress.state == "failed":
        prompted = binding.tmux_target is not None
        fail_start(deps.conn, binding, progress.binding, now, prompted=prompted)
    elif progress.binding != binding:
        save_binding_unless(deps.conn, progress.binding, lambda existing: existing != binding)


def _complete_start(
    deps: Deps, state: State, binding: Binding, sessions: Sequence[Session], now: datetime
) -> None:
    try:
        progress = deps.starts(binding, sessions, now)
    except Exception as error:  # noqa: BLE001
        _report(state, ("agent", binding.agent), error)
        if not expired(binding, now):
            return
        reason = f"not ready after {STARTUP.seconds}s ({type(error).__name__}: {error})"
        progress = failure(binding, reason, now)
    _settle(deps, binding, progress, sessions, now)


def _complete_starts(
    deps: Deps,
    state: State,
    starting: Sequence[Binding],
    sessions: Sequence[Session],
    now: datetime,
) -> None:
    for binding in starting:
        try:
            _complete_start(deps, state, binding, sessions, now)
        except Exception as error:  # noqa: BLE001
            _report(state, ("agent", binding.agent), error)
    abandon_starts(deps.conn, now)


def _confirm(deps: Deps, view: View) -> None:
    for message in awaiting_confirmation(deps.conn, view.now - CONFIRM_WINDOW):
        session = view.present.get(message.session_id or "")
        if session is not None:
            reader = deps.readers(session, binding_for(session, view.bindings))
            confirm_if_seen(deps.conn, message, session, reader, view.now)


def starting_bindings(current: Sequence[Binding]) -> list[Binding]:
    return [binding for binding in current if binding.state == "starting"]


def tick(deps: Deps, state: State) -> None:
    now = deps.now()
    sweep_sending(deps.conn, now - STALE_CLAIM)
    expire_sessions(deps.conn, now - EXPIRY, now)
    try:
        sessions = deps.sessions()
    except HlsUnavailable:
        _complete_starts(deps, state, starting_bindings(bindings(deps.conn)), [], now)
        raise
    present = {session.session_id: session for session in sessions}
    targets = pending_targets(deps.conn)
    state.absent = absences(targets, present, state)
    table = deps.table() if needs_table(bindings(deps.conn), state.absent) else None
    current = follow_bindings(deps.conn, sessions, table, now)
    starting = starting_bindings(current)
    _complete_starts(deps, state, starting, sessions, now)
    if starting:
        current = bindings(deps.conn)
        targets = pending_targets(deps.conn)
    view = View(present, current, table, now)
    _confirm(deps, view)
    for target in targets:
        try:
            _drain_target(deps, state, target, view)
        except Exception as error:  # noqa: BLE001
            _report(state, target, error)
    started = {("agent", b.agent) for b in current if b.state == "starting"}
    state.errors = {
        target: text
        for target, text in state.errors.items()
        if target in targets or target in started
    }
    state.seen = {name: state.seen[name] for name in state.absent if name in state.seen} | present
    state.pushed = {sid: last for sid, last in state.pushed.items() if now - last.at < PACE}


def work_left(deps: Deps) -> bool:
    return drain_work(deps.conn, deps.now())


def backoff(failures: int) -> float:
    return min(TICK_SECONDS * float(2 ** min(failures, BACKOFF_STEPS)), BACKOFF_MAX_SECONDS)


def run(deps: Deps, lock: Lock) -> int:
    state = State()
    while True:
        if not work_left(deps):
            lock.release()
            if not work_left(deps) or not lock.try_acquire():
                return 0
            continue
        try:
            tick(deps, state)
            state.failures = 0
            delay = TICK_SECONDS
        except HlsUnavailable as error:
            state.failures += 1
            delay = backoff(state.failures)
            print(f"bellhop: drainer: {error}; retrying in {delay:g}s", file=sys.stderr, flush=True)
        deps.sleep(delay)


def drainer_starts(
    conn: sqlite3.Connection, routes: RouteFinder, env: Mapping[str, str]
) -> StartCheck:
    return start_checker(conn, routes, lambda socket: tmux_for(env, socket))


def keeping_config(error: Exception) -> None:
    print(f"bellhop: drainer: {error}; keeping the last good config", file=sys.stderr, flush=True)


def lock_path(env: Mapping[str, str]) -> Path:
    return state_dir(env) / LOCK_NAME


def drainer_running(env: Mapping[str, str]) -> bool:
    lock = DrainLock(lock_path(env))
    try:
        if lock.try_acquire():
            lock.release()
            return False
        return True
    finally:
        lock.close()


def drainer_env(env: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in env.items()
        if name not in SESSION_VARS and not name.startswith(CALLER_ONLY_PREFIXES)
    }


def drainer_argv(executable: str) -> list[str]:
    return [executable, "-I", "-m", "bellhop", "_drain"]


def rotate_log(log: Path, limit: int = LOG_LIMIT) -> None:
    try:
        if not log.is_symlink() and log.stat().st_size > limit:
            log.replace(log.with_name(f"{LOG_NAME}.1"))
    except FileNotFoundError:
        pass


def inherited_fds(candidates: Sequence[int]) -> list[int]:
    def inheritable(fd: int) -> bool:
        try:
            return fd > STDERR_FD and os.get_inheritable(fd)
        except OSError:
            return False

    return [fd for fd in candidates if inheritable(fd)]


def open_fds() -> list[int]:
    try:
        return sorted(int(name) for name in os.listdir("/dev/fd") if name.isdigit())
    except OSError:
        return list(range(3, 256))


def start_drainer(env: Mapping[str, str]) -> int | None:
    if drainer_running(env):
        return None
    log = state_dir(env) / LOG_NAME
    rotate_log(log)
    log_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    closes = [(os.POSIX_SPAWN_CLOSE, fd) for fd in inherited_fds(open_fds())]
    return os.posix_spawn(
        sys.executable,
        drainer_argv(sys.executable),
        drainer_env(env),
        file_actions=[
            *closes,
            (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, str(log), log_flags, PRIVATE_FILE),
            (os.POSIX_SPAWN_DUP2, 1, 2),
        ],
        setsid=True,
    )


def main(env: Mapping[str, str]) -> int:
    os.chdir("/")
    lock = DrainLock(lock_path(env))
    if not lock.try_acquire():
        lock.close()
        return 0
    lock.record_owner()
    conn = open_store(state_dir(env))
    config = watched_config(env, on_error=keeping_config)
    wiring = wire(env, config)
    deps = Deps(
        conn=conn,
        sessions=lambda: list_sessions(env).sessions,
        routes=wiring.routes,
        readers=wiring.readers,
        table=process_table,
        now=lambda: datetime.now(UTC),
        sleep=time.sleep,
        starts=drainer_starts(conn, wiring.routes, env),
    )
    try:
        return run(deps, lock)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    finally:
        conn.close()
        lock.close()
