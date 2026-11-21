import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bellhop.config import Config
from bellhop.drain import (
    Deps,
    DrainLock,
    DrainStatus,
    State,
    backoff,
    drainer_argv,
    drainer_env,
    drainer_starts,
    reporting_sessions,
    rotate_log,
    run,
    start_drainer,
    tick,
    work_left,
    write_status,
)
from bellhop.errors import HlsUnavailable
from bellhop.identity import Proc
from bellhop.launch import Progress, StartCheck, expired, failure
from bellhop.models import Binding, PushResult, Session
from bellhop.routes.fake import FakeRoute
from bellhop.store import (
    ABANDONED_DETAIL,
    Push,
    begin_start,
    claim,
    connect,
    get_binding,
    get_message,
    insert_message,
    record_outcome,
    save_binding_unless,
)
from bellhop.transcripts import PlainTranscripts
from conftest import FAKE_HLS, NOW, STARTED, FakeHls, FakeTmux, make_message, make_session

ENKI = Binding("enki", "claude", "live", "s1", 4242, STARTED, None, None, None, "bind", None, NOW)


def never(existing: object) -> bool:
    return False


SLEEP_LIMIT = 10_000


@dataclass
class World:
    conn: sqlite3.Connection
    sessions: list[Session]
    route: FakeRoute = field(default_factory=FakeRoute)
    table: dict[int, Proc] = field(default_factory=lambda: {4242: Proc(4242, 1, STARTED)})
    clock: list[datetime] = field(default_factory=lambda: [NOW])
    sleeps: list[float] = field(default_factory=list)
    failures: int = 0
    reader: PlainTranscripts | None = None

    def listed(self) -> Sequence[Session]:
        if self.failures:
            self.failures -= 1
            raise HlsUnavailable(
                "hooklinesinker sessions --json gave no answer within 5s (2 attempts)"
            )
        return list(self.sessions)

    def sleep(self, seconds: float) -> None:
        assert len(self.sleeps) < SLEEP_LIMIT, "the drainer never ran out of work"
        self.sleeps.append(seconds)
        self.clock[0] += timedelta(seconds=seconds)

    @property
    def deps(self) -> Deps:
        return Deps(
            conn=self.conn,
            sessions=self.listed,
            routes=lambda session, binding: self.route,
            readers=lambda session, binding: self.reader,
            table=lambda: self.table,
            now=lambda: self.clock[0],
            sleep=self.sleep,
        )

    def advance(self, seconds: float) -> None:
        self.clock[0] += timedelta(seconds=seconds)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    conn = connect(tmp_path / "bellhop.sqlite")
    yield World(conn, [make_session("s1")])
    conn.close()


def pushed_bodies(world: World) -> list[str]:
    return [text.split("] ", 1)[1] for _, text, _ in world.route.pushes]


class OpenLock:
    def __init__(self, on_release: object = None) -> None:
        self.on_release = on_release
        self.held = True

    def try_acquire(self) -> bool:
        self.held = True
        return True

    def release(self) -> None:
        self.held = False
        if callable(self.on_release):
            self.on_release()
            self.on_release = None


def test_pushes_in_order_and_paces_until_observed_at_advances(world: World) -> None:
    for number in (1, 2, 3):
        insert_message(world.conn, make_message(number, created_at=NOW))
    state = State()

    tick(world.deps, state)
    tick(world.deps, state)
    assert pushed_bodies(world) == ["body 1"]

    world.sessions = [replace(world.sessions[0], observed_at=NOW + timedelta(seconds=1))]
    tick(world.deps, state)
    assert pushed_bodies(world) == ["body 1", "body 2"]

    world.advance(3)
    tick(world.deps, state)
    assert pushed_bodies(world) == ["body 1", "body 2", "body 3"]


@pytest.mark.parametrize("phase", ["permission", "compacting", "unknown"])
def test_holds_while_the_session_is_busy(world: World, phase: str) -> None:
    insert_message(world.conn, make_message(1, created_at=NOW))
    world.sessions = [make_session("s1", phase=phase)]

    tick(world.deps, State())

    assert world.route.pushes == []


def test_a_route_that_does_not_queue_waits_for_an_idle_answer(world: World) -> None:
    insert_message(world.conn, make_message(1, created_at=NOW))
    world.route = FakeRoute(queues_until_idle=False, busy_answer=True)
    tick(world.deps, State())
    assert world.route.pushes == []

    world.route.busy_answer = False
    tick(world.deps, State())
    assert pushed_bodies(world) == ["body 1"]


def test_a_busy_outcome_keeps_the_message_for_a_later_tick(world: World) -> None:
    stored = insert_message(world.conn, make_message(1, created_at=NOW))
    world.route = FakeRoute(outcomes=["busy", "accepted"])
    state = State()

    tick(world.deps, state)
    assert get_message(world.conn, stored.id).state == "pending"
    world.advance(3)
    tick(world.deps, state)
    assert get_message(world.conn, stored.id).state == "sent"


def test_survives_hls_failing_for_several_ticks(world: World) -> None:
    insert_message(world.conn, make_message(1, created_at=NOW))
    world.failures = 3

    assert run(world.deps, OpenLock()) == 0

    assert world.sleeps[:3] == [2.0, 4.0, 8.0]
    assert pushed_bodies(world) == ["body 1"]


def test_a_vanished_session_with_a_live_pid_keeps_its_messages(world: World) -> None:
    stored = insert_message(world.conn, make_message(1, created_at=NOW))
    world.route = FakeRoute(outcomes=["busy"] * 10)
    state = State()
    tick(world.deps, state)
    world.sessions = []

    for _ in range(4):
        tick(world.deps, state)
    assert get_message(world.conn, stored.id).state == "pending"

    world.table = {}
    tick(world.deps, state)
    failed = get_message(world.conn, stored.id)
    assert (failed.state, failed.detail) == ("failed", "session gone")


def test_a_fresh_drainer_keeps_messages_for_a_session_it_never_saw(world: World) -> None:
    stored = insert_message(world.conn, make_message(1, created_at=NOW))
    world.sessions = []
    world.table = {}
    state = State()

    for _ in range(4):
        tick(world.deps, state)

    assert get_message(world.conn, stored.id).state == "pending"


def test_a_vanished_session_keeps_its_messages_while_ps_is_unavailable(world: World) -> None:
    stored = insert_message(world.conn, make_message(1, created_at=NOW))
    world.route = FakeRoute(outcomes=["busy"] * 10)
    state = State()
    tick(world.deps, state)
    world.sessions = []
    unavailable = replace(world.deps, table=lambda: None)

    for _ in range(4):
        tick(unavailable, state)

    assert get_message(world.conn, stored.id).state == "pending"


def test_a_vanished_session_with_a_dead_pid_fails_on_the_third_clean_poll(world: World) -> None:
    stored = insert_message(world.conn, make_message(1, created_at=NOW))
    world.route = FakeRoute(outcomes=["busy"] * 10)
    state = State()
    tick(world.deps, state)
    world.sessions = []
    world.table = {}

    tick(world.deps, state)
    world.failures = 1
    with pytest.raises(HlsUnavailable):
        tick(world.deps, state)
    tick(world.deps, state)
    assert get_message(world.conn, stored.id).state == "pending"

    tick(world.deps, state)
    failed = get_message(world.conn, stored.id)
    assert (failed.state, failed.detail) == ("failed", "session gone")


def test_session_messages_expire_at_24_hours_and_agent_messages_do_not(world: World) -> None:
    old = insert_message(world.conn, make_message(1, created_at=NOW - timedelta(hours=25)))
    agent = insert_message(
        world.conn,
        make_message(2, target_kind="agent", target="enki", created_at=NOW - timedelta(days=3)),
    )
    world.route = FakeRoute(is_reachable=False)

    tick(world.deps, State())

    assert (get_message(world.conn, old.id).state, get_message(world.conn, old.id).detail) == (
        "failed",
        "expired",
    )
    assert get_message(world.conn, agent.id).state == "pending"


def test_an_agent_target_drains_once_a_binding_appears(world: World) -> None:
    insert_message(world.conn, make_message(1, target_kind="agent", target="enki", created_at=NOW))
    state = State()
    tick(world.deps, state)
    assert world.route.pushes == []

    save_binding_unless(
        world.conn,
        ENKI,
        never,
    )
    tick(world.deps, state)

    assert pushed_bodies(world) == ["body 1"]


def test_a_now_message_drained_over_a_non_interrupting_route_reports_the_degrade(
    world: World,
) -> None:
    stored = insert_message(
        world.conn, make_message(1, target_kind="agent", target="enki", urgency="now")
    )
    save_binding_unless(world.conn, ENKI, never)
    world.route = FakeRoute(can_interrupt=False)

    tick(world.deps, State())

    after = get_message(world.conn, stored.id)
    assert (after.state, after.degraded_from) == ("sent", "now")
    assert world.route.pushes[0][2] is False


def test_one_target_failing_does_not_stop_the_others(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    class BrokenForS1(PlainTranscripts):
        def size(self, session: Session) -> int | None:
            if session.session_id == "s1":
                raise OSError("transcript unreadable")
            return None

    world.sessions = [make_session("s1"), make_session("s2", pid=4343)]
    world.reader = BrokenForS1()
    insert_message(world.conn, make_message(1))
    insert_message(world.conn, make_message(2, target="s2"))
    state = State()

    tick(world.deps, state)
    tick(world.deps, state)

    assert pushed_bodies(world) == ["body 2"]
    assert (
        capsys.readouterr().err == "bellhop: drainer: session s1: OSError: transcript unreadable\n"
    )


def test_two_addresses_of_one_session_share_its_pacing(world: World) -> None:
    save_binding_unless(world.conn, ENKI, never)
    insert_message(world.conn, make_message(1, target_kind="agent", target="enki"))
    insert_message(world.conn, make_message(2))
    world.route = FakeRoute(queues_until_idle=False, busy_answer=False)

    tick(world.deps, State())

    assert pushed_bodies(world) == ["body 1"]


def test_the_drainer_sweeps_a_claim_left_by_a_crashed_sender(world: World) -> None:
    stored = insert_message(world.conn, make_message(1))
    claim(world.conn, stored.id, Push("s1", "fake", None), NOW)
    world.advance(31)

    tick(world.deps, State())

    after = get_message(world.conn, stored.id)
    assert (after.state, after.detail) == ("sent", "outcome unknown")
    assert world.route.pushes == []


def test_a_session_message_under_24_hours_old_keeps_waiting(world: World) -> None:
    young = insert_message(world.conn, make_message(1, created_at=NOW - timedelta(hours=23)))
    world.route = FakeRoute(is_reachable=False)

    tick(world.deps, State())

    assert get_message(world.conn, young.id).state == "pending"


def test_ps_runs_only_when_a_binding_or_a_vanished_session_needs_it(world: World) -> None:
    calls: list[int] = []

    def table() -> dict[int, Proc]:
        calls.append(1)
        return world.table

    insert_message(world.conn, make_message(1))
    counted = replace(world.deps, table=table)
    tick(counted, State())
    assert calls == []

    save_binding_unless(world.conn, ENKI, never)
    tick(counted, State())
    assert calls == [1]


def test_the_drainer_confirms_from_the_transcript(world: World, tmp_path: Path) -> None:
    transcript = tmp_path / "t.txt"
    transcript.write_text("")
    world.sessions = [make_session("s1", transcript_path=transcript)]
    world.reader = PlainTranscripts()
    world.route = FakeRoute(echo=True)
    stored = insert_message(world.conn, make_message(1, created_at=NOW))

    assert run(world.deps, OpenLock()) == 0

    assert get_message(world.conn, stored.id).state == "confirmed"


def test_an_unconfirmed_message_stops_being_checked_after_ten_minutes(
    world: World, tmp_path: Path
) -> None:
    transcript = tmp_path / "t.txt"
    transcript.write_text("")
    world.sessions = [make_session("s1", transcript_path=transcript)]
    world.reader = PlainTranscripts()
    stored = insert_message(world.conn, make_message(1, created_at=NOW))

    assert run(world.deps, OpenLock()) == 0

    assert get_message(world.conn, stored.id).state == "sent"
    assert world.clock[0] - NOW >= timedelta(minutes=10)


def test_work_arriving_while_the_lock_is_released_is_not_lost(world: World) -> None:
    def concurrent_send() -> None:
        insert_message(world.conn, make_message(9, created_at=world.clock[0]))

    assert run(world.deps, OpenLock(on_release=concurrent_send)) == 0

    assert pushed_bodies(world) == ["body 9"]


def test_another_drainer_taking_the_lock_ends_this_one(world: World) -> None:
    class Taken(OpenLock):
        def try_acquire(self) -> bool:
            return False

    lock = Taken(on_release=lambda: insert_message(world.conn, make_message(9, created_at=NOW)))

    assert run(world.deps, lock) == 0

    assert world.route.pushes == []


def test_a_binding_follows_its_process_to_a_new_session_id(world: World) -> None:
    save_binding_unless(
        world.conn,
        Binding(
            "enki", "claude", "live", "old", 4242, STARTED, None, None, None, "bind", None, NOW
        ),
        never,
    )
    insert_message(world.conn, make_message(1, target_kind="agent", target="enki", created_at=NOW))

    tick(world.deps, State())

    assert world.route.pushes[0][0] == "s1"


@pytest.fixture
def process_env(tmp_path: Path) -> dict[str, str]:
    hls_output = tmp_path / "sessions.json"
    record = FakeHls.record(
        "claude", "s1", process={"pid": os.getpid(), "startedAt": "2026-11-13T09:00:00Z"}
    )
    hls_output.write_text(json.dumps({"protocol": 1, "sessions": [record], "problems": []}))
    routes = tmp_path / "routes.json"
    routes.write_text(
        json.dumps(
            {"log": str(tmp_path / "pushes.jsonl"), "default": {"outcomes": ["busy"] * 1000}}
        )
    )
    env = dict(os.environ)
    env.update(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "BELLHOP_HOOKLINESINKER": str(FAKE_HLS),
            "FAKE_HLS_OUTPUT": str(hls_output),
            "BELLHOP_FAKE_ROUTES": str(routes),
        }
    )
    conn = connect(tmp_path / "state" / "bellhop" / "bellhop.sqlite")
    insert_message(conn, make_message(1, created_at=datetime.now(UTC)))
    conn.close()
    return env


def drainer_pid(env: dict[str, str]) -> int | None:
    deadline = time.monotonic() + 10
    path = Path(env["XDG_STATE_HOME"]) / "bellhop" / "drain.lock"
    while time.monotonic() < deadline:
        text = path.read_text().strip() if path.exists() else ""
        if text.isdigit() and alive_pid(int(text)):
            return int(text)
        time.sleep(0.1)
    return None


def alive_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop(pid: int | None) -> None:
    assert pid not in {os.getpid(), os.getppid()}
    if pid is not None and alive_pid(pid):
        os.kill(pid, signal.SIGKILL)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            deadline = time.monotonic() + 5
            while alive_pid(pid) and time.monotonic() < deadline:
                time.sleep(0.05)


def test_two_drainers_started_together_leave_one_running(process_env: dict[str, str]) -> None:
    command = [sys.executable, "-m", "bellhop", "_drain"]
    first = subprocess.Popen(command, env=process_env, stderr=subprocess.DEVNULL)
    second = subprocess.Popen(command, env=process_env, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while first.poll() is None and second.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        codes = sorted(code for code in (first.poll(), second.poll()) if code is not None)
        assert codes == [0]
        assert drainer_pid(process_env) in {first.pid, second.pid}
    finally:
        for proc in (first, second):
            if proc.poll() is None:
                proc.kill()
            proc.wait(5)


def test_a_detached_drainer_is_started_and_restarted_after_a_kill(
    process_env: dict[str, str],
) -> None:
    spawned: list[int | None] = []
    started = time.monotonic()
    spawned.append(start_drainer(process_env))
    try:
        assert spawned[0] is not None
        assert time.monotonic() - started < 2
        assert drainer_pid(process_env) == spawned[0]
        assert start_drainer(process_env) is None
        stop(spawned[0])

        spawned.append(start_drainer(process_env))
        assert spawned[1] is not None
        assert drainer_pid(process_env) == spawned[1]
    finally:
        for pid in spawned:
            stop(pid)


def test_a_python_without_spawn_sessions_still_detaches_the_drainer(
    process_env: dict[str, str],
) -> None:
    def spawn(path: str, argv: list[str], env: dict[str, str], **options: Any) -> int:
        if "setsid" in options:
            raise NotImplementedError("posix_spawn: setsid unavailable on this platform")
        return os.posix_spawn(path, argv, env, **options)

    pid = start_drainer(process_env, spawn)
    try:
        assert pid is not None
        assert drainer_pid(process_env) == pid
        assert os.getsid(pid) == pid
    finally:
        stop(pid)


def test_the_drainer_keeps_none_of_the_callers_inherited_descriptors(
    process_env: dict[str, str],
) -> None:
    read_end, write_end = os.pipe()
    os.set_inheritable(write_end, True)
    pid = start_drainer(process_env)
    try:
        os.close(write_end)
        ready, _, _ = select.select([read_end], [], [], 5)
        assert ready == [read_end]
        assert os.read(read_end, 1) == b""
        assert pid is not None and alive_pid(pid)
    finally:
        os.close(read_end)
        stop(pid)


def test_the_drainer_never_imports_from_the_callers_directory(
    process_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = tmp_path / "planted"
    planted.mkdir()
    marker = tmp_path / "imported"
    (planted / "json.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    monkeypatch.chdir(planted)

    pid = start_drainer(process_env)
    try:
        assert pid is not None
        assert drainer_pid(process_env) == pid
        assert marker.exists() is False
    finally:
        stop(pid)


def test_the_drainer_drops_the_callers_session_identity() -> None:
    env = {
        "PATH": "/bin",
        "XDG_STATE_HOME": "/s",
        "CLAUDE_CODE_SESSION_ID": "c1",
        "CODEX_THREAD_ID": "t1",
        "PI_SESSION_ID": "p1",
        "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
        "CODEX_HOME": "/c",
    }

    assert drainer_env(env) == {"PATH": "/bin", "XDG_STATE_HOME": "/s", "CODEX_HOME": "/c"}
    assert drainer_argv("/py")[:2] == ["/py", "-I"]


def test_a_large_drain_log_is_rotated_before_a_start(tmp_path: Path) -> None:
    log = tmp_path / "drain.log"
    log.write_text("x" * 11)

    rotate_log(log, limit=10)

    assert (log.exists(), (tmp_path / "drain.log.1").read_text()) == (False, "x" * 11)


def test_the_lock_file_is_private_whatever_the_umask(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        lock = DrainLock(tmp_path / "state" / "drain.lock")
        lock.close()
    finally:
        os.umask(previous)

    assert (tmp_path / "state" / "drain.lock").stat().st_mode & 0o777 == 0o600


def test_the_drainer_exits_when_only_messages_to_unbound_agents_wait(world: World) -> None:
    stored = insert_message(world.conn, make_message(1, target_kind="agent", target="nanna"))

    assert run(world.deps, OpenLock()) == 0

    assert get_message(world.conn, stored.id).state == "pending"
    assert world.sleeps == []


def test_a_bound_agents_message_is_work_for_the_drainer(world: World) -> None:
    insert_message(world.conn, make_message(1, target_kind="agent", target="enki"))
    save_binding_unless(world.conn, ENKI, never)

    assert run(world.deps, OpenLock()) == 0

    assert pushed_bodies(world) == ["body 1"]


def test_session_messages_expire_while_hls_stays_down(world: World) -> None:
    stored = insert_message(world.conn, make_message(1, created_at=NOW - timedelta(hours=30)))
    world.failures = 10**6

    assert run(world.deps, OpenLock()) == 0

    expired = get_message(world.conn, stored.id)
    assert (expired.state, expired.detail) == ("failed", "expired")
    assert world.sleeps == [2.0]


def test_the_backoff_stays_at_ten_seconds_however_long_hls_is_down() -> None:
    assert [backoff(n) for n in (1, 2, 3, 4, 5000)] == [2.0, 4.0, 8.0, 10.0, 10.0]


def test_the_route_gets_the_binding_of_the_session_it_delivers_to(world: World) -> None:
    given: list[tuple[str, str | None]] = []

    def finder(session: Session, binding: Binding | None) -> FakeRoute:
        given.append((session.session_id, binding.agent if binding else None))
        return world.route

    save_binding_unless(world.conn, ENKI, never)
    save_binding_unless(world.conn, replace(ENKI, agent="s1", session_id="elsewhere"), never)
    insert_message(world.conn, make_message(1))

    tick(replace(world.deps, routes=finder), State())

    assert given == [("s1", "enki")]
    assert pushed_bodies(world) == ["body 1"]


def test_failing_a_gone_sessions_messages_is_traced(world: World) -> None:
    insert_message(world.conn, make_message(1))
    world.route = FakeRoute(is_reachable=False, is_replaced=True)
    seen: list[tuple[str, dict[str, object]]] = []

    tick(
        replace(world.deps, trace=lambda event, /, **fields: seen.append((event, fields))), State()
    )

    assert seen == [
        (
            "fail",
            {
                "target": "s1",
                "kind": "session",
                "detail": "session replaced in its process",
                "messages": 1,
            },
        )
    ]


def test_a_replaced_session_fails_its_messages_and_an_agent_waits(world: World) -> None:
    to_session = insert_message(world.conn, make_message(1))
    to_agent = insert_message(world.conn, make_message(2, target_kind="agent", target="enki"))
    save_binding_unless(world.conn, ENKI, never)
    world.route = FakeRoute(is_reachable=False, is_replaced=True)

    tick(world.deps, State())

    failed = get_message(world.conn, to_session.id)
    assert (failed.state, failed.detail) == ("failed", "session replaced in its process")
    assert get_message(world.conn, to_agent.id).state == "pending"


@pytest.mark.parametrize("phase", ["idle", "permission", "compacting", "unknown"])
def test_a_replaced_session_fails_its_messages_whatever_its_last_phase(
    world: World, phase: str
) -> None:
    world.sessions = [make_session("s1", phase=phase)]
    stored = insert_message(world.conn, make_message(1))
    world.route = FakeRoute(is_replaced=True)

    tick(world.deps, State())

    failed = get_message(world.conn, stored.id)
    assert (failed.state, failed.detail) == ("failed", "session replaced in its process")
    assert world.route.pushes == []


def test_a_session_hls_dropped_from_a_live_process_fails_once_replaced(world: World) -> None:
    stored = insert_message(world.conn, make_message(1, created_at=NOW))
    world.route = FakeRoute(outcomes=["busy"] * 10)
    state = State()
    tick(world.deps, state)
    world.sessions = []
    world.route = FakeRoute(is_replaced=True)

    tick(world.deps, state)
    tick(world.deps, state)
    assert get_message(world.conn, stored.id).state == "pending"

    tick(world.deps, state)
    failed = get_message(world.conn, stored.id)
    assert (failed.state, failed.detail) == ("failed", "session replaced in its process")


@pytest.mark.parametrize(
    "route",
    [
        FakeRoute(queues_until_idle=False, busy_answer=True),
        FakeRoute(queues_until_idle=True),
    ],
    ids=["codex-like, turn running", "pi-like"],
)
def test_a_waiting_now_message_is_pushed_as_an_interrupt(world: World, route: FakeRoute) -> None:
    world.sessions = [make_session("s1", phase="working")]
    insert_message(world.conn, make_message(1, urgency="now"))
    world.route = route

    tick(world.deps, State())

    assert [interrupt for _, _, interrupt in route.pushes] == [True]


def test_a_waiting_now_message_still_waits_out_a_permission_prompt(world: World) -> None:
    world.sessions = [make_session("s1", phase="permission")]
    stored = insert_message(world.conn, make_message(1, urgency="now"))

    tick(world.deps, State())

    assert get_message(world.conn, stored.id).state == "pending"
    assert world.route.pushes == []


def test_a_message_to_a_stopped_codex_agent_does_not_keep_the_drainer(world: World) -> None:
    stopped = Binding(
        "nanna", "codex", "live", "gone-thread", None, None, None, None, None, "bind", None, NOW
    )
    save_binding_unless(world.conn, stopped, never)
    insert_message(world.conn, make_message(1, target_kind="agent", target="nanna"))

    assert run(world.deps, OpenLock()) == 0

    assert world.route.pushes == []


STARTING = replace(ENKI, state="starting", pid=None, process_started_at=None, tmux_target="enki:%0")


def launched(world: World, number: int = 1) -> None:
    message = make_message(number, target_kind="agent", target="enki", route="launch")
    begin_start(world.conn, STARTING, message, NOW)


def checked(progress: str, **changes: object) -> StartCheck:
    def check(binding: Binding, sessions: Sequence[Session], now: datetime) -> Progress:
        if progress == "waiting" and expired(binding, now):
            return failure(binding, "not ready", now)
        return Progress(progress, "checked", replace(binding, **changes))  # type: ignore[arg-type]

    return check


def test_a_ready_start_goes_live_marks_its_prompt_sent_and_drains_the_queue(world: World) -> None:
    launched(world)
    insert_message(world.conn, make_message(2, target_kind="agent", target="enki"))
    deps = replace(
        world.deps, starts=checked("ready", state="live", pid=4242, process_started_at=STARTED)
    )

    tick(deps, State())

    first, second = (
        get_message(world.conn, "bh_000000000001"),
        get_message(world.conn, "bh_000000000002"),
    )
    assert (first.state, first.route, first.session_id) == ("sent", "launch", "s1")
    assert (second.state, pushed_bodies(world)) == ("sent", ["body 2"])
    assert get_binding(world.conn, "enki") == replace(
        STARTING, state="live", pid=4242, process_started_at=STARTED
    )


def test_a_start_that_fails_after_its_pane_opened_leaves_its_prompt_outcome_unknown(
    world: World,
) -> None:
    launched(world)
    insert_message(world.conn, make_message(2, target_kind="agent", target="enki"))
    deps = replace(world.deps, starts=checked("failed", state="failed", detail="start failed: x"))

    tick(deps, State())

    first, second = (
        get_message(world.conn, "bh_000000000001"),
        get_message(world.conn, "bh_000000000002"),
    )
    assert (first.state, first.detail, second.state) == (
        "sent",
        "outcome unknown; start failed: x",
        "pending",
    )
    assert world.route.pushes == []


def test_a_waiting_codex_start_keeps_the_thread_it_found(world: World) -> None:
    launched(world)
    deps = replace(world.deps, starts=checked("waiting", session_id="thread-1"))

    tick(deps, State())

    binding = get_binding(world.conn, "enki")
    assert binding is not None
    assert (binding.state, binding.session_id) == ("starting", "thread-1")


def test_a_start_replaced_meanwhile_is_left_alone(world: World) -> None:
    launched(world)

    def rebound(binding: Binding, sessions: Sequence[Session], now: datetime) -> Progress:
        save_binding_unless(world.conn, ENKI, never)
        return Progress("failed", "x", replace(binding, state="failed", detail="start failed: x"))

    tick(replace(world.deps, starts=rebound), State())

    message = get_message(world.conn, "bh_000000000001")
    assert get_binding(world.conn, "enki") == ENKI
    assert (message.state, message.detail) == ("failed", ABANDONED_DETAIL)


def test_a_start_check_that_raises_is_reported_and_other_targets_still_drain(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    launched(world)
    insert_message(world.conn, make_message(2))

    def broken(binding: Binding, sessions: Sequence[Session], now: datetime) -> Progress:
        raise RuntimeError("pane went missing")

    tick(replace(world.deps, starts=broken), State())

    assert pushed_bodies(world) == ["body 2"]
    assert "drainer: agent enki: RuntimeError: pane went missing" in capsys.readouterr().err


def test_a_starting_binding_keeps_the_drainer_running(world: World) -> None:
    save_binding_unless(world.conn, STARTING, never)
    calls: list[int] = []

    def ready_later(binding: Binding, sessions: Sequence[Session], now: datetime) -> Progress:
        calls.append(1)
        if len(calls) < 3:
            return Progress("waiting", "w", binding)
        return Progress(
            "ready", "", replace(binding, state="live", pid=4242, process_started_at=STARTED)
        )

    assert run(replace(world.deps, starts=ready_later), OpenLock()) == 0
    assert len(calls) == 3


@pytest.mark.parametrize(("after", "state"), [(10, "starting"), (91, "failed")])
def test_a_start_check_that_keeps_raising_fails_the_start_at_its_deadline(
    world: World, after: int, state: str
) -> None:
    launched(world)

    def broken(binding: Binding, sessions: Sequence[Session], now: datetime) -> Progress:
        raise RuntimeError("tmux hung")

    world.advance(after)
    tick(replace(world.deps, starts=broken), State())

    binding = get_binding(world.conn, "enki")
    assert binding is not None
    assert binding.state == state


@pytest.mark.parametrize(("after", "state"), [(10, "starting"), (91, "failed")])
def test_a_start_fails_at_its_deadline_even_while_hls_is_unavailable(
    world: World, after: int, state: str
) -> None:
    launched(world)
    world.failures = 1
    world.advance(after)

    with pytest.raises(HlsUnavailable):
        tick(replace(world.deps, starts=checked("waiting")), State())

    binding = get_binding(world.conn, "enki")
    assert binding is not None
    assert binding.state == state


def test_a_start_prompt_is_confirmed_from_the_new_session_s_transcript(
    world: World, tmp_path: Path
) -> None:
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("")
    world.sessions = [make_session("s1", transcript_path=transcript)]
    world.reader = PlainTranscripts()
    launched(world)
    deps = replace(
        world.deps, starts=checked("ready", state="live", pid=4242, process_started_at=STARTED)
    )
    state = State()
    tick(deps, state)
    transcript.write_text("[bellhop bh_000000000001 from enki] body 1\n")

    tick(deps, state)

    message = get_message(world.conn, "bh_000000000001")
    assert (message.state, message.transcript_offset, message.transcript) == (
        "confirmed",
        0,
        transcript,
    )


def test_a_start_prompt_of_unknown_outcome_is_never_confirmed(world: World, tmp_path: Path) -> None:
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("[bellhop bh_000000000001 from enki] body 1\n")
    world.sessions = [make_session("s1", transcript_path=transcript)]
    world.reader = PlainTranscripts()
    launched(world)
    state = State()
    tick(
        replace(world.deps, starts=checked("failed", state="failed", detail="start failed: x")),
        state,
    )

    tick(world.deps, state)

    message = get_message(world.conn, "bh_000000000001")
    assert (message.state, message.detail) == ("sent", "outcome unknown; start failed: x")


def test_the_drainer_checks_a_start_on_the_server_it_was_recorded_on(
    world: World, fake_tmux: FakeTmux
) -> None:
    config = Config(Path("/c/config.toml"), tmux_socket="/srv/tmux/fleet")
    check = drainer_starts(world.conn, lambda s, b: world.route, dict(os.environ), lambda: config)

    check(replace(STARTING, tmux_target="-S /srv/tmux/fleet enki:%3"), [], NOW)

    assert fake_tmux.calls()[-1][:2] == ["-S", "/srv/tmux/fleet"]


def sent_at(world: World, minutes_ago: int, transcript: Path) -> str:
    message = insert_message(world.conn, make_message(1, created_at=NOW))
    then = NOW - timedelta(minutes=minutes_ago)
    claim(world.conn, message.id, Push("s1", "fake", 0, transcript), then)
    record_outcome(world.conn, message.id, PushResult("accepted", "ok"), then)
    return message.id


@pytest.mark.parametrize(("phase", "state"), [("working", "confirmed"), ("idle", "sent")])
def test_a_message_sent_before_a_long_turn_is_confirmed_while_the_session_works(
    world: World, tmp_path: Path, phase: str, state: str
) -> None:
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("[bellhop bh_000000000001 from enki] body 1\n")
    world.sessions = [make_session("s1", transcript_path=transcript, phase=phase)]
    world.reader = PlainTranscripts()
    message_id = sent_at(world, 15, transcript)

    tick(world.deps, State())

    assert get_message(world.conn, message_id).state == state


def test_the_drainer_keeps_running_while_it_watches_a_working_session(
    world: World, tmp_path: Path
) -> None:
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("")
    world.sessions = [make_session("s1", transcript_path=transcript, phase="working")]
    world.reader = PlainTranscripts()
    sent_at(world, 15, transcript)
    state = State()
    tick(world.deps, state)

    assert work_left(world.deps, state) is True


def test_the_drainer_outlasts_the_confirm_window_while_the_session_keeps_working(
    world: World, tmp_path: Path
) -> None:
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("")
    world.sessions = [make_session("s1", transcript_path=transcript, phase="working")]
    world.reader = PlainTranscripts()
    message_id = sent_at(world, 0, transcript)
    world.advance(9 * 60 + 58.4)

    def slow_tick(seconds: float) -> None:
        world.sleep(seconds)
        world.advance(1.4)
        if world.clock[0] > NOW + timedelta(minutes=12):
            transcript.write_text("[bellhop bh_000000000001 from enki] body 1\n")

    assert run(replace(world.deps, sleep=slow_tick), OpenLock()) == 0
    assert get_message(world.conn, message_id).state == "confirmed"


def test_a_session_hls_cannot_place_does_not_hold_a_message_open(
    world: World, tmp_path: Path
) -> None:
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("")
    world.sessions = [make_session("s1", transcript_path=transcript, phase="unknown")]
    world.reader = PlainTranscripts()
    message_id = sent_at(world, 15, transcript)
    transcript.write_text("[bellhop bh_000000000001 from enki] body 1\n")

    tick(world.deps, State())

    assert get_message(world.conn, message_id).state == "sent"


def test_the_drainer_records_whether_hooklinesinker_answers(
    tmp_path: Path, fake_hls: FakeHls
) -> None:
    path = tmp_path / "drain.status.json"
    first = DrainStatus(4242, "2026-11-14T09:00:00+00:00", str(fake_hls.path))
    listed = reporting_sessions(os.environ, path, first)
    fake_hls.fail(3, "ledger locked")
    with pytest.raises(HlsUnavailable):
        listed()
    failing = json.loads(path.read_text())
    fake_hls.fail(0, "")
    fake_hls.respond([])

    listed()

    assert (failing["hls_ok"], failing["last_error"]) == (
        False,
        "hooklinesinker sessions --json exited 3: ledger locked",
    )
    assert json.loads(path.read_text())["hls_ok"] is True


def test_a_status_file_that_cannot_be_written_does_not_stop_the_drainer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "missing" / "drain.status.json"

    write_status(path, DrainStatus(4242, "2026-11-14T09:00:00+00:00", None))

    assert capsys.readouterr().err.startswith(f"bellhop: drainer: cannot write {path}: ")


def test_the_drainer_logs_to_a_fresh_file_in_place_of_a_planted_link(
    process_env: dict[str, str], tmp_path: Path
) -> None:
    target = tmp_path / "zshrc"
    target.write_text("mine\n")
    log = tmp_path / "state" / "bellhop" / "drain.log"
    log.symlink_to(target)
    pid = start_drainer({**process_env, "FAKE_HLS_EXIT": "3"})
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (log.exists() and "exited 3" in log.read_text()):
            time.sleep(0.1)
        logged = log.exists() and "exited 3" in log.read_text()
    finally:
        stop(pid)

    assert (logged, target.read_text(), log.is_symlink()) == (True, "mine\n", False)
