import socket
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from bellhop.codex_rpc import Client, Unwritten
from bellhop.models import PushResult
from bellhop.routes.codex import MEMO_SECONDS, PUSH_SECONDS, CodexRoute, Probes
from conftest import make_session
from fake_app_server import app_server

THREAD = "01a1d000-0000-7000-8000-00000000c0de"
SESSION = make_session(THREAD, harness="codex")


def test_reachable_needs_the_server_to_list_the_thread(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD]):
        assert CodexRoute(path).reachable(SESSION) is True
        assert CodexRoute(path).reachable(make_session("other", harness="codex")) is False
    assert CodexRoute(path).reachable(SESSION) is False
    assert CodexRoute(None).reachable(SESSION) is False


def test_an_idle_thread_gets_a_turn_start(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD]) as server:
        result = CodexRoute(path).push(SESSION, "[bellhop bh_1 from x] hi", interrupt=False)

    assert result == PushResult("accepted", "turn/start turn-1")
    started = [params for method, params in server.calls if method == "turn/start"]
    assert started[0]["threadId"] == THREAD
    assert started[0]["input"] == [
        {"type": "text", "text": "[bellhop bh_1 from x] hi", "text_elements": []}
    ]


def test_a_running_turn_is_busy_for_an_idle_push(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], running_turn="turn-9") as server:
        result = CodexRoute(path).push(SESSION, "hi", interrupt=False)
        busy = CodexRoute(path).busy(SESSION)

    assert result == PushResult("busy", "turn turn-9 is running")
    assert busy is True
    assert "turn/start" not in server.methods()


def test_an_interrupt_steers_the_running_turn(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], running_turn="turn-9") as server:
        result = CodexRoute(path).push(SESSION, "now please", interrupt=True)

    assert result == PushResult("accepted", "turn/steer turn-9")
    steered = [params for method, params in server.calls if method == "turn/steer"]
    assert (steered[0]["expectedTurnId"], steered[0]["threadId"]) == ("turn-9", THREAD)


def test_an_interrupt_to_a_turn_still_starting_waits(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], running_turn="turn-9", turns_listed=False) as server:
        result = CodexRoute(path).push(SESSION, "now please", interrupt=True)

    assert result == PushResult("busy", "a turn is starting")
    assert "turn/steer" not in server.methods()


@pytest.mark.parametrize(
    ("reply", "outcome"),
    [("error", "rejected"), ("drop", "unknown"), ("silent", "unknown")],
)
def test_turn_start_failures_map_to_their_outcomes(
    short_tmp: Path, reply: str, outcome: str
) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], start_reply=reply):
        result = CodexRoute(path).push(SESSION, "hi", interrupt=False)

    assert result.outcome == outcome


def test_a_gone_server_is_busy_at_push_and_unknown_to_busy(short_tmp: Path) -> None:
    route = CodexRoute(short_tmp / "gone.sock")

    assert route.push(SESSION, "hi", interrupt=False).outcome == "busy"
    assert route.busy(SESSION) is None


def test_a_thread_whose_turn_is_starting_is_busy_for_every_push(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], read_error="thread is empty") as server:
        results = [CodexRoute(path).push(SESSION, "x", interrupt=flag) for flag in (False, True)]
        busy = CodexRoute(path).busy(SESSION)

    assert results == [PushResult("busy", "a turn is starting")] * 2
    assert busy is True
    assert server.methods().count("turn/start") == 0


def test_a_refused_steer_leaves_the_message_waiting(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    refusal = "expected active turn id turn-9 but found none"
    with app_server(path, loaded=[THREAD], running_turn="turn-9", steer_error=refusal):
        result = CodexRoute(path).push(SESSION, "now please", interrupt=True)

    assert result == PushResult("busy", f"turn/steer refused: {refusal}")


def test_a_turn_read_that_fails_leaves_the_thread_reachable_with_its_turn_unknown(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Client, thread_id: str) -> None:
        raise Unwritten("Broken pipe")

    monkeypatch.setattr(Client, "turn", broken)
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD]):
        route = CodexRoute(path)
        found = (route.reachable(SESSION), route.busy(SESSION))
        pushed = route.push(SESSION, "x", interrupt=False)

    assert found == (True, None)
    assert pushed == PushResult("busy", "app server stopped answering: Broken pipe")


def test_a_server_that_hangs_up_after_the_upgrade_is_unreachable(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], close_after_upgrade=True):
        route = CodexRoute(path)
        found = (route.reachable(SESSION), route.busy(SESSION))
        pushed = route.push(SESSION, "x", interrupt=False)

    assert found == (False, None)
    assert pushed.outcome == "busy"


def test_send_asks_the_app_server_about_the_thread_once_before_pushing(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD]) as server:
        route = CodexRoute(path)
        steps = (route.reachable(SESSION), route.busy(SESSION))
        route.push(SESSION, "x", interrupt=False)

    assert steps == (True, False)
    assert [method for method, _ in server.calls].count("initialize") == 2


@pytest.mark.parametrize("flag", ["waitingOnApproval", "waitingOnUserInput"])
def test_a_thread_waiting_on_a_dialog_takes_no_push_not_even_now(
    short_tmp: Path, flag: str
) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], running_turn="turn-9", waiting=flag) as server:
        results = [CodexRoute(path).push(SESSION, "x", interrupt=now) for now in (False, True)]
        busy = CodexRoute(path).busy(SESSION)

    assert results == [PushResult("busy", "a dialog is open in the session")] * 2
    assert busy is True
    assert "turn/steer" not in server.methods()


@pytest.mark.parametrize(
    "status",
    [
        {"type": "notLoaded"},
        {"type": "systemError"},
        {"type": "somethingNew"},
        {"kind": "active"},
        "active",
    ],
    ids=["not loaded", "system error", "unknown", "no type", "not an object"],
)
def test_only_an_idle_thread_takes_a_turn_start(short_tmp: Path, status: object) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], running_turn="turn-9", raw_status=status) as server:
        result = CodexRoute(path).push(SESSION, "x", interrupt=False)
        busy = CodexRoute(path).busy(SESSION)

    assert result.outcome == "busy"
    assert result.detail.startswith("the thread's status is ")
    assert busy is True
    assert "turn/start" not in server.methods()


@pytest.mark.parametrize(
    ("status", "turns", "detail"),
    [
        ({"type": "active", "activeFlags": [{"odd": 1}]}, [], "a turn is starting"),
        ({"type": "active", "activeFlags": []}, [{"status": "inProgress"}], "a turn is starting"),
    ],
    ids=["flags that are not names", "a running turn without an id"],
)
def test_a_malformed_reply_is_busy_not_an_error(
    short_tmp: Path, status: object, turns: object, detail: str
) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], running_turn="t", raw_status=status, raw_turns=turns):
        result = CodexRoute(path).push(SESSION, "x", interrupt=True)

    assert result == PushResult("busy", detail)


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        ("silent", "no reply within 2s"),
        ("drop", "connection dropped after the write: app server closed the connection"),
    ],
    ids=["no reply", "connection dropped"],
)
def test_a_steer_whose_outcome_is_unknown_is_never_retried(
    short_tmp: Path, reply: str, detail: str
) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], running_turn="turn-9", steer_reply=reply):
        result = CodexRoute(path).push(SESSION, "now please", interrupt=True)

    assert result == PushResult("unknown", detail)


def test_a_turn_start_whose_write_failed_is_busy(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = Client.send

    def broken(self: Client, message: Mapping[str, object]) -> None:
        if message.get("method") == "turn/start":
            raise BrokenPipeError(32, "Broken pipe")
        sent(self, message)

    monkeypatch.setattr(Client, "send", broken)
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD]):
        result = CodexRoute(path).push(SESSION, "x", interrupt=False)

    assert result == PushResult("busy", "the write did not finish: Broken pipe")


def test_a_push_keeps_to_one_deadline_across_its_calls(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], delay=0.8):
        started = time.monotonic()
        result = CodexRoute(path).push(SESSION, "x", interrupt=False)
        elapsed = time.monotonic() - started

    assert result == PushResult("unknown", "no reply within 2s")
    assert elapsed < PUSH_SECONDS + 0.8


def test_one_probe_answers_every_thread_on_a_socket_within_a_second(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    clock = [100.0]
    probes = Probes(clock=lambda: clock[0])
    threads = [make_session(name, harness="codex") for name in ("t1", "t2", "t3")]
    with app_server(path, loaded=["t1", "t2", "t3"]) as server:
        first = [CodexRoute(path, probes).reachable(thread) for thread in threads]
        busy = CodexRoute(path, probes).busy(threads[0])
        clock[0] += MEMO_SECONDS
        later = CodexRoute(path, probes).reachable(threads[1])

    assert (first, busy, later) == ([True, True, True], False, True)
    assert [method for method, _ in server.calls].count("initialize") == 2


def test_a_daemon_that_never_answers_costs_one_probe_not_one_per_thread(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(8)
    probes = Probes()
    threads = [make_session(f"t{n}", harness="codex") for n in range(5)]
    try:
        started = time.monotonic()
        found = [CodexRoute(path, probes).reachable(thread) for thread in threads]
        elapsed = time.monotonic() - started
    finally:
        listener.close()

    assert found == [False] * 5
    assert elapsed < 1.5


def test_a_daemon_that_never_answers_costs_one_probe_for_every_busy_check(
    short_tmp: Path,
) -> None:
    path = short_tmp / "app.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(8)
    probes = Probes()
    threads = [make_session(f"t{n}", harness="codex") for n in range(5)]
    try:
        started = time.monotonic()
        found = [CodexRoute(path, probes).busy(thread) for thread in threads]
        elapsed = time.monotonic() - started
    finally:
        listener.close()

    assert found == [None] * 5
    assert elapsed < 1.5


def test_a_thread_the_daemon_has_not_loaded_is_answered_without_a_probe(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    probes = Probes()
    with app_server(path, loaded=["t1"]) as server:
        CodexRoute(path, probes).reachable(make_session("t1", harness="codex"))
        busy = CodexRoute(path, probes).busy(make_session("gone", harness="codex"))

    assert busy is None
    assert [method for method, _ in server.calls].count("initialize") == 1


def test_an_unreadable_status_is_reported_as_unknown(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=[THREAD], running_turn="t", raw_status="active"):
        result = CodexRoute(path).push(SESSION, "x", interrupt=False)

    assert result == PushResult("busy", "the thread's status is unknown")
