from typing import Any

import pytest

from bellhop.cli import main
from conftest import ALPHA, Bell


def events(bell: Bell, *words: str) -> list[dict[str, Any]]:
    code, payload = bell.run("log", "-n", "100", *words)
    assert code == 0
    found: list[dict[str, Any]] = payload["events"]
    return found


def test_a_send_leaves_its_command_its_push_and_its_message(bell: Bell) -> None:
    _, sent = bell.run("send", ALPHA, "hello", "--from", "enki")
    message_id = sent["message"]["id"]

    found = {event["event"]: event for event in events(bell)}

    assert found["command"]["argv"] == ["send", ALPHA, "hello", "--from", "enki", "--json"]
    assert (found["command"]["exit"], found["command"]["src"]) == (0, "cli")
    assert {name: found["push"][name] for name in ("message", "session", "route", "outcome")} == {
        "message": message_id,
        "session": ALPHA,
        "route": "fake",
        "outcome": "accepted",
    }
    assert (found["message"]["id"], found["message"]["state"]) == (message_id, "sent")


def test_a_refused_send_leaves_the_reason_it_was_refused(bell: Bell) -> None:
    bell.route(ALPHA, reachable=False)

    bell.run("send", ALPHA, "hello", "--from", "enki")

    found = {event["event"]: event for event in events(bell)}
    assert (found["probe"]["session"], found["probe"]["reachable"]) == (ALPHA, False)
    assert found["probe"]["reason"] == "the fake route is set unreachable"
    assert (found["command"]["exit"], found["command"]["code"]) == (4, "no_route")
    assert found["command"]["error"] == (
        "no usable route to 'alpha' (claude); the fake route is set unreachable"
    )


def test_errors_keeps_only_what_went_wrong(bell: Bell) -> None:
    bell.route(ALPHA, reachable=False)
    bell.run("sessions")
    bell.run("send", ALPHA, "hello", "--from", "enki")

    code, payload = bell.run("log", "--errors")

    assert code == 0
    assert [(event["event"], event["code"]) for event in payload["events"]] == [
        ("command", "no_route")
    ]


def test_the_log_command_is_not_itself_recorded(bell: Bell) -> None:
    bell.run("sessions")
    events(bell)

    assert [event["argv"][0] for event in events(bell, "command")] == ["sessions"]


def test_words_narrow_the_events_and_n_keeps_the_last(bell: Bell) -> None:
    bell.run("sessions")
    bell.run("agents")

    assert [event["argv"][0] for event in events(bell, '"agents"')] == ["agents"]
    _, payload = bell.run("log", "-n", "1")
    assert [event["argv"][0] for event in payload["events"]] == ["agents"]


def test_the_text_form_is_one_line_per_event(
    bell: Bell, capsys: pytest.CaptureFixture[str]
) -> None:
    bell.run("agents")

    assert main(["log", "command"]) == 0

    [line] = capsys.readouterr().out.splitlines()
    clock, source, event, *fields = line.split(" ")
    assert (len(clock), source.split("[")[0], event) == (12, "cli", "command")
    assert 'argv=["agents","--json"]' in fields
    assert "exit=0" in fields


def test_an_empty_trace_says_where_it_looked(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["log"]) == 0

    assert capsys.readouterr().out.startswith("no matching events in ")
