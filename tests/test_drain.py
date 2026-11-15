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

import pytest

from bellhop.drain import (
    Deps,
    DrainLock,
    State,
    backoff,
    drainer_argv,
    drainer_env,
    rotate_log,
    run,
    start_drainer,
    tick,
)
from bellhop.errors import HlsUnavailable
from bellhop.identity import Proc
from bellhop.models import Binding, Session
from bellhop.routes.fake import FakeRoute
from bellhop.store import Push, claim, connect, get_message, insert_message, save_binding
from bellhop.transcripts import PlainTranscripts
from conftest import FAKE_HLS, NOW, STARTED, FakeHls, make_message, make_session

ENKI = Binding("enki", "claude", "live", "s1", 4242, STARTED, None, None, None, "bind", None, NOW)


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

    save_binding(
        world.conn,
        ENKI,
    )
    tick(world.deps, state)

    assert pushed_bodies(world) == ["body 1"]


def test_a_now_message_drained_over_a_non_interrupting_route_reports_the_degrade(
    world: World,
) -> None:
    stored = insert_message(
        world.conn, make_message(1, target_kind="agent", target="enki", urgency="now")
    )
    save_binding(world.conn, ENKI)
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
    save_binding(world.conn, ENKI)
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

    save_binding(world.conn, ENKI)
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
    save_binding(
        world.conn,
        Binding(
            "enki", "claude", "live", "old", 4242, STARTED, None, None, None, "bind", None, NOW
        ),
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
    save_binding(world.conn, ENKI)

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

    save_binding(world.conn, ENKI)
    save_binding(world.conn, replace(ENKI, agent="s1", session_id="elsewhere"))
    insert_message(world.conn, make_message(1))

    tick(replace(world.deps, routes=finder), State())

    assert given == [("s1", "enki")]
    assert pushed_bodies(world) == ["body 1"]


def test_a_replaced_session_fails_its_messages_and_an_agent_waits(world: World) -> None:
    to_session = insert_message(world.conn, make_message(1))
    to_agent = insert_message(world.conn, make_message(2, target_kind="agent", target="enki"))
    save_binding(world.conn, ENKI)
    world.route = FakeRoute(is_reachable=False, is_replaced=True)

    tick(world.deps, State())

    failed = get_message(world.conn, to_session.id)
    assert (failed.state, failed.detail) == ("failed", "session replaced in its process")
    assert get_message(world.conn, to_agent.id).state == "pending"
