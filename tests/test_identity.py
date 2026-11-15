import os
import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest

from bellhop.errors import BellhopError
from bellhop.identity import (
    Proc,
    alive,
    ancestors,
    complete,
    identify,
    parse_lstart,
    parse_table,
    process_table,
)
from conftest import STARTED, make_session

TABLE = {
    1: Proc(1, 0, STARTED - timedelta(days=1)),
    4242: Proc(4242, 1, STARTED),
    5000: Proc(5000, 4242, STARTED + timedelta(minutes=5)),
    6000: Proc(6000, 5000, STARTED + timedelta(minutes=6)),
}


def test_parse_table_reads_ps_utc_output_whatever_the_callers_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    try:
        table = parse_table("  42     1 Fri Nov 13 10:00:00 2026\nnot a row\n")
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()

    assert table == {42: Proc(42, 1, datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC))}
    assert parse_lstart("garbage") is None


def test_the_live_process_table_matches_this_process_start_in_utc() -> None:
    table = process_table()

    assert table is not None
    started = table[os.getpid()].started
    assert started is not None
    assert abs(datetime.now(UTC) - started) < timedelta(hours=1)


def test_a_table_without_the_callers_own_pid_counts_as_unavailable() -> None:
    assert complete(TABLE, 6000) == TABLE
    assert complete(TABLE, 7) is None


def test_a_failing_ps_gives_no_table(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = f"{os.getpid()} 1 Fri Nov 13 10:00:00 2026\n"
    failed = subprocess.CompletedProcess(["ps"], 1, stdout=partial, stderr="ps: denied")
    monkeypatch.setattr("bellhop.identity.subprocess.run", lambda *a, **k: failed)

    assert process_table() is None


def test_a_ps_that_times_out_gives_no_table(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["ps"], 5)

    monkeypatch.setattr("bellhop.identity.subprocess.run", hang)

    assert process_table() is None


def test_ancestors_walk_up_to_init_and_stop_on_cycles() -> None:
    assert [proc.pid for proc in ancestors(6000, TABLE)] == [6000, 5000, 4242, 1]
    assert [proc.pid for proc in ancestors(7, {7: Proc(7, 8, None), 8: Proc(8, 7, None)})] == [7, 8]


def test_alive_needs_the_same_start_time() -> None:
    assert alive(4242, STARTED, TABLE) is True
    assert alive(4242, STARTED + timedelta(seconds=5), TABLE) is False
    assert alive(31337, None, TABLE) is False


def test_the_harness_variable_wins_when_hls_lists_it() -> None:
    session = make_session("c1", pid=999)

    assert identify({"CLAUDE_CODE_SESSION_ID": "c1"}, [session], TABLE, 6000) == session


def test_a_variable_hls_does_not_list_is_exit_3() -> None:
    with pytest.raises(BellhopError) as caught:
        identify({"CODEX_THREAD_ID": "x9"}, [make_session("c1")], TABLE, 6000)

    assert str(caught.value) == "CODEX_THREAD_ID=x9 is not a live session hooklinesinker lists"
    assert (caught.value.code, caught.value.exit_code) == ("caller_unknown", 3)


def test_two_listed_variables_pick_the_nearest_ancestor() -> None:
    outer = make_session("c1", pid=4242)
    inner = make_session("p1", harness="pi", pid=5000, process_started_at=TABLE[5000].started)
    env = {"CLAUDE_CODE_SESSION_ID": "c1", "PI_SESSION_ID": "p1"}

    assert identify(env, [outer, inner], TABLE, 6000) == inner


def test_without_a_variable_the_process_tree_decides() -> None:
    mine = make_session("c1", pid=4242)
    other = make_session("c2", pid=777)

    assert identify({}, [other, mine], TABLE, 6000) == mine


def test_a_reused_pid_with_another_start_time_is_not_the_caller() -> None:
    reused = make_session("c1", pid=4242, process_started_at=STARTED - timedelta(hours=1))

    with pytest.raises(BellhopError, match="not inside a session hooklinesinker knows"):
        identify({}, [reused], TABLE, 6000)


def test_two_sessions_on_the_same_process_are_ambiguous() -> None:
    first = make_session("x1", harness="codex")
    second = make_session("x2", harness="codex")

    with pytest.raises(BellhopError) as caught:
        identify({}, [first, second], TABLE, 6000)

    assert caught.value.exit_code == 3
    assert caught.value.candidates == ["x1 (codex, title-x1)", "x2 (codex, title-x2)"]
