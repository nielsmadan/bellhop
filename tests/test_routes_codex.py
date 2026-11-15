from pathlib import Path

import pytest

from bellhop.codex_rpc import Client, Unwritten
from bellhop.models import PushResult
from bellhop.routes.codex import CodexRoute
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


def test_a_write_that_never_finishes_is_never_an_error_of_the_probe(
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

    assert found == (False, None)
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
