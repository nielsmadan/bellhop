import json
import os
import subprocess
import sys

import pytest

from bellhop.cli import main
from bellhop.store import connect, insert_message
from conftest import ALPHA, BETA, FAKE_HLS, Bell, check_golden, make_message, payload_message


def ids(payload: dict[str, object]) -> list[str]:
    messages = payload["messages"]
    assert isinstance(messages, list)
    return [m["id"] for m in messages]


def test_hold_then_inbox_twice_returns_the_items_once(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first = bell.run("send", ALPHA, "one", "--urgency", "hold", "--from", "enki")
    _, second = bell.run("send", ALPHA, "two", "--urgency", "hold", "--from", "enki")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    code, taken = bell.run("inbox")
    _, again = bell.run("inbox")

    assert code == 0
    assert ids(taken) == [payload_message(first)["id"], payload_message(second)["id"]]
    assert ids(again) == []
    check_golden("inbox", taken)


def test_peek_leaves_the_items_unread(bell: Bell, monkeypatch: pytest.MonkeyPatch) -> None:
    bell.run("send", ALPHA, "one", "--urgency", "hold", "--from", "enki")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    _, peeked = bell.run("inbox", "--peek")
    _, taken = bell.run("inbox")

    assert ids(peeked) == ids(taken)
    assert len(ids(taken)) == 1


def test_inbox_includes_holds_for_the_callers_bound_agent(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bell.tmp / "config-home" / "bellhop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[agents.enki]\nharness = 'claude'\n")
    _, held = bell.run("send", "enki", "for the agent", "--urgency", "hold", "--from", "nanna")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    bell.run("bind", "enki")

    _, taken = bell.run("inbox")

    assert ids(taken) == [payload_message(held)["id"]]


def test_inbox_for_another_target(bell: Bell) -> None:
    _, held = bell.run("send", BETA, "x", "--urgency", "hold", "--from", "enki")

    _, taken = bell.run("inbox", "--target", "beta")

    assert ids(taken) == [payload_message(held)["id"]]


def test_inbox_outside_a_session_is_exit_3(bell: Bell) -> None:
    code, payload = bell.run("inbox")

    assert code == 3
    assert payload["error"] == {
        "code": "caller_unknown",
        "message": "the caller is not inside a session hooklinesinker knows",
        "candidates": [],
    }


def test_two_concurrent_inbox_processes_never_return_the_same_item(bell: Bell) -> None:
    for number in range(30):
        bell.run("send", ALPHA, f"item {number}", "--urgency", "hold", "--from", "enki")
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=ALPHA, BELLHOP_HOOKLINESINKER=str(FAKE_HLS))
    command = [sys.executable, "-m", "bellhop", "inbox", "--json"]
    procs = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE) for _ in range(2)]
    outputs = [json.loads(proc.communicate(timeout=60)[0]) for proc in procs]

    taken = [ids(output) for output in outputs]
    assert sorted(taken[0] + taken[1]) == sorted(set(taken[0] + taken[1]))
    assert len(taken[0]) + len(taken[1]) == 30


def test_status_reports_the_store_and_warns_about_unknown_ids(bell: Bell) -> None:
    _, sent = bell.run("send", ALPHA, "x", "--from", "enki")
    _, held = bell.run("send", ALPHA, "y", "--urgency", "hold", "--from", "enki")

    code, payload = bell.run(
        "status",
        str(payload_message(sent)["id"]),
        str(payload_message(held)["id"]),
        "bh_ffffffffffff",
    )

    assert code == 0
    assert ids(payload) == [payload_message(sent)["id"], payload_message(held)["id"]]
    assert payload["warnings"] == ["unknown message id bh_ffffffffffff"]
    check_golden("status", payload)


def test_inbox_text_escapes_terminal_controls_but_keeps_line_breaks(
    bell: Bell, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bell.run("status", "bh_000000000000")
    conn = connect(bell.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    body = "one\n\x1b]0;pwned\x07two\u202e"
    insert_message(conn, make_message(1, urgency="hold", target=ALPHA, body=body))
    conn.close()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    main(["inbox"])

    out = capsys.readouterr().out
    assert out.endswith("one\n\\x1b]0;pwned\\x07two\\u202e\n")


def test_inbox_json_carries_each_message_s_text(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    bell.run("send", ALPHA, "the build is green", "--urgency", "hold")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    _, peeked = bell.run("inbox", "--peek")
    _, taken = bell.run("inbox")

    assert (
        [m["body"] for m in peeked["messages"]]
        == [m["body"] for m in taken["messages"]]
        == ["the build is green"]
    )


def test_status_rechecks_the_transcript_of_a_sent_message(bell: Bell) -> None:
    _, sent = bell.run("send", ALPHA, "check me", "--from", "x")
    message_id = payload_message(sent)["id"]
    bell.transcript(ALPHA).write_text(f"[bellhop {message_id} from x] check me\n")

    _, payload = bell.run("status", str(message_id))

    assert payload["messages"][0]["state"] == "confirmed"


def test_status_reports_the_stored_states_when_it_cannot_recheck(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sent = bell.run("send", ALPHA, "check me", "--from", "x")
    message_id = payload_message(sent)["id"]
    monkeypatch.setenv("FAKE_HLS_EXIT", "3")

    code, payload = bell.run("status", str(message_id))

    assert (code, payload["messages"][0]["state"]) == (0, "sent")
    assert payload["warnings"][0].startswith("cannot recheck transcripts: hooklinesinker")
