import multiprocessing
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from bellhop.errors import BellhopError
from bellhop.models import Binding, PushResult
from bellhop.store import (
    AWAITING_SQL,
    HAS_WORK_SQL,
    SCHEMA_VERSION,
    SWEEP_SQL,
    V1,
    V2,
    Push,
    awaiting_confirmation,
    bindings,
    claim,
    confirm,
    enable_wal,
    expire_sessions,
    fail_pending,
    get_binding,
    get_messages,
    has_work,
    holds_waiting,
    insert_message,
    older_pending,
    oldest_pending,
    pending_targets,
    prune,
    record_outcome,
    save_binding,
    save_binding_unless,
    schema_version,
    sweep_sending,
    take_inbox,
)
from bellhop.store import connect as open_connection
from conftest import NOW, make_message

OPENED: list[sqlite3.Connection] = []


def connect(path: Path) -> sqlite3.Connection:
    conn = open_connection(path)
    OPENED.append(conn)
    return conn


@pytest.fixture(autouse=True)
def close_connections() -> Iterator[None]:
    yield
    while OPENED:
        OPENED.pop().close()


def _open(path: Path) -> None:
    open_connection(path).close()


def test_a_new_store_is_private_and_at_the_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "state" / "bellhop.sqlite"
    conn = connect(path)

    assert schema_version(conn) == SCHEMA_VERSION
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bellhop.sqlite"
    connect(path).execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")

    with pytest.raises(BellhopError, match="has schema version 99; this bellhop reads up to 3"):
        connect(path)


def test_two_processes_creating_the_store_at_once_both_succeed(tmp_path: Path) -> None:
    path = tmp_path / "bellhop.sqlite"
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_open, args=(path,)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)

    assert [worker.exitcode for worker in workers] == [0, 0, 0, 0]
    assert schema_version(connect(path)) == SCHEMA_VERSION


def test_the_wal_switch_is_retried_while_the_database_is_busy() -> None:
    calls: list[str] = []

    class Busy:
        def execute(self, sql: str) -> None:
            calls.append(sql)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")

    ticks = iter(range(100))
    enable_wal(Busy(), clock=lambda: float(next(ticks)), sleep=lambda _: None)  # type: ignore[arg-type]

    assert calls == ["PRAGMA journal_mode = WAL"] * 3


def test_the_wal_switch_raises_other_errors_at_once() -> None:
    calls: list[str] = []

    class Broken:
        def execute(self, sql: str) -> None:
            calls.append(sql)
            raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        enable_wal(Broken(), clock=lambda: 0.0, sleep=lambda _: None)  # type: ignore[arg-type]

    assert len(calls) == 1


def test_the_wal_switch_gives_up_at_the_deadline() -> None:
    class Busy:
        def execute(self, sql: str) -> None:
            raise sqlite3.OperationalError("database is locked")

    ticks = iter([0.0, 10.0, 31.0])
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        enable_wal(Busy(), clock=lambda: next(ticks), sleep=lambda _: None)  # type: ignore[arg-type]


def test_opening_waits_for_an_exclusive_lock_on_a_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "bellhop.sqlite"
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    OPENED.append(holder)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("CREATE TABLE hold (x)")
    timer = threading.Timer(0.3, lambda: holder.execute("ROLLBACK"))
    started = time.monotonic()
    timer.start()

    conn = connect(path)

    timer.join()
    assert time.monotonic() - started >= 0.25
    assert schema_version(conn) == SCHEMA_VERSION


def test_a_known_key_returns_the_existing_message(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    first = insert_message(conn, make_message(1, key="k1"))

    second = insert_message(conn, make_message(2, key="k1", body="other"))

    assert second == first
    assert get_messages(conn, [make_message(2).id]) == []


def test_ordering_and_hold_counts(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    first = insert_message(conn, make_message(1))
    second = insert_message(conn, make_message(2))
    insert_message(conn, make_message(3, urgency="hold"))

    assert older_pending(conn, [("session", "s1")], second.seq) is True
    assert older_pending(conn, [("session", "s1")], first.seq) is False
    assert holds_waiting(conn, "session", "s1") == 1
    assert oldest_pending(conn, "session", "s1") == first
    assert pending_targets(conn) == [("session", "s1")]


def test_an_older_message_in_flight_or_to_the_other_address_counts_as_older(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    flying = insert_message(conn, make_message(1))
    claim(conn, flying.id, Push("s1", "fake", None), NOW)
    to_agent = insert_message(conn, make_message(2, target_kind="agent", target="enki"))
    newer = insert_message(conn, make_message(3))

    assert older_pending(conn, [("session", "s1")], newer.seq) is True
    assert older_pending(conn, [("session", "s9"), ("agent", "enki")], newer.seq) is True
    assert older_pending(conn, [("session", "s9")], to_agent.seq) is False


def test_claim_then_each_outcome(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    states = {}
    for number, outcome in enumerate(("accepted", "busy", "rejected", "unknown"), start=1):
        stored = insert_message(conn, make_message(number))
        assert claim(conn, stored.id, Push("s1", "fake", 7), NOW) is True
        assert claim(conn, stored.id, Push("s1", "fake", 7), NOW) is False
        result = record_outcome(conn, stored.id, PushResult(outcome, f"said {outcome}"), NOW)  # type: ignore[arg-type]
        states[outcome] = (result.state, result.detail, result.transcript_offset)

    assert states == {
        "accepted": ("sent", "said accepted", 7),
        "busy": ("pending", "said busy", None),
        "rejected": ("failed", "said rejected", 7),
        "unknown": ("sent", "said unknown", 7),
    }


def test_a_stale_claim_is_swept_to_sent_with_an_unknown_outcome(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    stored = insert_message(conn, make_message(1))
    claim(conn, stored.id, Push("s1", "fake", None), NOW)

    assert sweep_sending(conn, NOW - timedelta(seconds=30)) == 0
    assert sweep_sending(conn, NOW + timedelta(seconds=1)) == 1
    [swept] = get_messages(conn, [stored.id])
    assert (swept.state, swept.detail) == ("sent", "outcome unknown")
    assert (
        record_outcome(conn, stored.id, PushResult("accepted", "late"), NOW).detail
        == "outcome unknown"
    )


def test_inbox_takes_holds_once_and_peek_leaves_them(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    insert_message(conn, make_message(1, urgency="hold"))
    insert_message(conn, make_message(2, urgency="hold", target_kind="agent", target="enki"))
    insert_message(conn, make_message(3, urgency="idle"))
    targets = [("session", "s1"), ("agent", "enki")]

    peeked = take_inbox(conn, targets, peek=True, now=NOW)  # type: ignore[arg-type]
    taken = take_inbox(conn, targets, peek=False, now=NOW)  # type: ignore[arg-type]

    assert [m.body for m in peeked] == ["body 1", "body 2"]
    assert [(m.body, m.state) for m in taken] == [("body 1", "read"), ("body 2", "read")]
    assert take_inbox(conn, targets, peek=False, now=NOW) == []  # type: ignore[arg-type]


def test_session_messages_expire_but_holds_and_agent_messages_do_not(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    day_old = NOW - timedelta(hours=25)
    old = insert_message(conn, make_message(1, created_at=day_old))
    hold = insert_message(conn, make_message(2, urgency="hold", created_at=day_old))
    agent = insert_message(
        conn, make_message(3, target_kind="agent", target="enki", created_at=day_old)
    )

    assert expire_sessions(conn, NOW - timedelta(hours=24), NOW) == 1

    states = {m.id: (m.state, m.detail) for m in get_messages(conn, [old.id, hold.id, agent.id])}
    assert states == {
        old.id: ("failed", "expired"),
        hold.id: ("pending", None),
        agent.id: ("pending", None),
    }


def test_confirmation_ends_the_work_and_a_gone_session_fails_only_its_pushes(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    sent = insert_message(conn, make_message(1, target="s2"))
    claim(conn, sent.id, Push("s2", "fake", 0), NOW)
    record_outcome(conn, sent.id, PushResult("accepted", "ok"), NOW)
    waiting = insert_message(conn, make_message(2))
    hold = insert_message(conn, make_message(3, urgency="hold"))

    assert [m.id for m in awaiting_confirmation(conn, NOW - timedelta(minutes=10))] == [sent.id]
    assert confirm(conn, sent.id, NOW).state == "confirmed"
    assert has_work(conn, NOW - timedelta(minutes=10)) is True
    assert fail_pending(conn, "session", "s1", "session gone", NOW) == 1
    assert has_work(conn, NOW - timedelta(minutes=10)) is False
    assert [m.state for m in get_messages(conn, [waiting.id, hold.id])] == ["failed", "pending"]


def test_prune_goes_by_when_a_message_became_final(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    month_ago = NOW - timedelta(days=31)
    long_done = insert_message(conn, make_message(1, created_at=month_ago))
    fail_pending(conn, "session", "s1", "session gone", month_ago)
    just_done = insert_message(
        conn, make_message(2, target_kind="agent", target="enki", created_at=month_ago)
    )
    fail_pending(conn, "agent", "enki", "session gone", NOW)
    read = insert_message(conn, make_message(3, urgency="hold", created_at=month_ago))
    take_inbox(conn, [("session", "s1")], peek=False, now=NOW)

    assert prune(conn, NOW - timedelta(days=30)) == 1
    kept = get_messages(conn, [long_done.id, just_done.id, read.id])
    assert [m.id for m in kept] == [just_done.id, read.id]


def test_a_version_1_store_gains_finish_times_on_open(tmp_path: Path) -> None:
    path = tmp_path / "bellhop.sqlite"
    old = sqlite3.connect(path, isolation_level=None)
    for statement in V1:
        old.execute(statement)
    old.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    old.execute(
        """INSERT INTO messages (id, target_kind, target, sender, body, urgency, state,
        created_at, confirmed_at) VALUES ('bh_000000000001', 'session', 's1', 'x', 'b', 'idle',
        'confirmed', '2026-09-01T00:00:00Z', '2026-11-14T09:00:00Z')"""
    )
    old.close()

    conn = connect(path)

    assert schema_version(conn) == SCHEMA_VERSION
    assert prune(conn, NOW - timedelta(days=30)) == 0


def test_opening_a_current_store_does_not_wait_for_the_write_lock(tmp_path: Path) -> None:
    path = tmp_path / "bellhop.sqlite"
    connect(path).close()
    writer = connect(path)
    writer.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        connect(path).close()
    finally:
        writer.execute("ROLLBACK")

    assert time.monotonic() - started < 2


def test_a_binding_is_saved_only_when_the_holder_allows_it(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    first = binding_named("enki", session_id="s1")
    second = binding_named("enki", session_id="s2")

    assert save_binding_unless(conn, first, lambda existing: existing is not None) is None
    assert save_binding_unless(conn, second, lambda existing: existing is not None) == first
    assert bindings(conn) == [first]


def binding_named(agent: str, **changes: object) -> Binding:
    base = Binding(
        agent=agent,
        harness="claude",
        state="live",
        session_id="s1",
        pid=42,
        process_started_at=NOW,
        app_server_socket=None,
        app_server_pid=None,
        tmux_target=None,
        cause="bind",
        detail=None,
        updated_at=NOW,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_bindings_round_trip(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    binding = binding_named("enki")

    save_binding(conn, binding)

    assert get_binding(conn, "enki") == binding
    assert get_binding(conn, "nobody") is None


def test_a_symlinked_store_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.sqlite"
    target.write_text("")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "bellhop.sqlite").symlink_to(target)

    with pytest.raises(BellhopError) as caught:
        connect(tmp_path / "state" / "bellhop.sqlite")

    assert caught.value.code == "store_error"


@pytest.mark.parametrize("sql", [HAS_WORK_SQL, AWAITING_SQL, SWEEP_SQL])
def test_the_per_tick_queries_use_the_partial_index(tmp_path: Path, sql: str) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")

    plan = conn.execute(f"EXPLAIN QUERY PLAN {sql}", ("2026-11-14T09:00:00Z",)).fetchall()

    assert any("messages_open" in str(tuple(row)) for row in plan)


def test_a_starting_binding_is_work_alone_and_for_its_agents_messages(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bellhop.sqlite")
    window = NOW - timedelta(minutes=10)
    save_binding(conn, binding_named("enki", state="starting"))
    assert has_work(conn, window) is True

    insert_message(conn, make_message(1, target_kind="agent", target="enki"))
    save_binding(conn, binding_named("enki", state="failed"))
    assert has_work(conn, window) is False
    save_binding(conn, binding_named("enki", state="starting"))
    assert has_work(conn, window) is True


def test_a_version_2_store_gains_the_transcript_column(tmp_path: Path) -> None:
    path = tmp_path / "bellhop.sqlite"
    old = sqlite3.connect(path, isolation_level=None)
    for statement in (*V1, *V2):
        old.execute(statement)
    old.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    old.close()

    conn = connect(path)
    stored = insert_message(conn, make_message(1))
    claim(conn, stored.id, Push("s1", "fake", 3, Path("/t/s1.jsonl")), NOW)

    assert schema_version(conn) == 3
    assert get_messages(conn, [stored.id])[0].transcript == Path("/t/s1.jsonl")
