import contextlib
import io
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bellhop.cli import main
from bellhop.routes.endpoint import PING_SECONDS
from bellhop.store import Push, claim, connect
from conftest import (
    ALPHA,
    BETA,
    NOW,
    Bell,
    FakeHls,
    check_golden,
    payload_message,
    pong_line,
    pong_server,
)
from fake_app_server import app_server

SEND_BUDGET = 9.5


def test_an_idle_send_to_a_queueing_route_is_sent(bell: Bell, drainer_starts: list[str]) -> None:
    code, payload = bell.run("send", ALPHA, "hello", "--from", "enki")

    assert code == 0
    sent = payload_message(payload)
    assert (sent["state"], sent["route"], sent["detail"]) == ("sent", "fake", "fake accepted")
    assert bell.pushes() == [
        {"session_id": ALPHA, "text": f"[bellhop {sent['id']} from enki] hello", "interrupt": False}
    ]
    assert drainer_starts == [str(bell.tmp / "state-home")]
    check_golden("send-sent", payload)


def test_the_body_comes_from_stdin_when_omitted(bell: Bell) -> None:
    code, payload = bell.run("send", "alpha", "--from", "enki", stdin="from stdin\n")

    assert code == 0
    assert (
        bell.pushes()[0]["text"]
        == f"[bellhop {payload_message(payload)['id']} from enki] from stdin\n"
    )


def test_a_send_that_must_wait_is_stored_pending(bell: Bell) -> None:
    bell.route(BETA, queues_until_idle=False, busy=True)

    code, payload = bell.run("send", BETA, "later", "--from", "enki")

    assert code == 0
    assert payload_message(payload)["state"] == "pending"
    assert bell.pushes() == []
    check_golden("send-pending", payload)


def test_confirm_waits_for_the_receivers_transcript(bell: Bell) -> None:
    bell.route(ALPHA, echo=True)

    code, payload = bell.run("send", ALPHA, "see me", "--from", "enki", "--confirm", "5")

    assert code == 0
    assert payload_message(payload)["state"] == "confirmed"
    check_golden("send-confirmed", payload)


def test_confirm_times_out_with_a_warning(bell: Bell) -> None:
    code, payload = bell.run("send", ALPHA, "unseen", "--from", "enki", "--confirm", "0.3")

    assert code == 0
    assert payload_message(payload)["state"] == "sent"
    assert payload["warnings"] == ["not confirmed within 0.3s"]


def test_a_rejected_push_is_exit_1_and_carries_the_stored_message(bell: Bell) -> None:
    bell.route(ALPHA, outcomes=["rejected"])

    code, payload = bell.run("send", ALPHA, "no", "--from", "enki")

    assert code == 1
    assert payload["error"] == {
        "code": "push_rejected",
        "message": f"the receiver rejected {payload_message(payload)['id']}: fake rejected",
    }
    assert payload_message(payload)["state"] == "failed"
    check_golden("send-push-rejected", payload)


@pytest.mark.parametrize(("outcome", "state"), [("busy", "pending"), ("unknown", "sent")])
def test_busy_and_unknown_outcomes_exit_0(bell: Bell, outcome: str, state: str) -> None:
    bell.route(ALPHA, outcomes=[outcome])

    code, payload = bell.run("send", ALPHA, "x", "--from", "enki")

    assert (code, payload_message(payload)["state"], payload_message(payload)["detail"]) == (
        0,
        state,
        f"fake {outcome}",
    )


def test_now_on_a_route_that_cannot_interrupt_is_degraded(bell: Bell) -> None:
    bell.route(ALPHA, can_interrupt=False)

    code, payload = bell.run("send", ALPHA, "urgent", "--urgency", "now", "--from", "enki")

    assert code == 0
    assert (payload_message(payload)["degraded_from"], payload_message(payload)["state"]) == (
        "now",
        "sent",
    )
    assert bell.pushes()[0]["interrupt"] is False
    check_golden("send-degraded", payload)


def test_now_interrupts_on_a_route_that_can(bell: Bell) -> None:
    bell.run("send", ALPHA, "urgent", "--urgency", "now", "--from", "enki")

    assert bell.pushes()[0]["interrupt"] is True


def test_hold_stores_and_pushes_nothing(bell: Bell) -> None:
    code, payload = bell.run("send", ALPHA, "later", "--urgency", "hold", "--from", "enki")

    assert (code, payload_message(payload)["state"], payload_message(payload)["urgency"]) == (
        0,
        "pending",
        "hold",
    )
    assert bell.pushes() == []


def test_no_route_is_exit_4_and_stores_nothing(bell: Bell) -> None:
    bell.route(ALPHA, none=True)

    code, payload = bell.run("send", ALPHA, "x", "--from", "enki")

    assert code == 4
    assert payload["error"] == {
        "code": "no_route",
        "message": "no usable route to 'alpha' (claude)",
    }
    assert bell.stored() == []
    check_golden("send-no-route", payload)


def test_or_hold_with_no_route_stores_a_hold(bell: Bell) -> None:
    bell.route(ALPHA, reachable=False)

    code, payload = bell.run("send", ALPHA, "x", "--or-hold", "--from", "enki")

    assert code == 0
    stored = payload_message(payload)
    assert (stored["urgency"], stored["degraded_from"], stored["state"]) == (
        "hold",
        "idle",
        "pending",
    )
    check_golden("send-held", payload)


def test_a_direct_push_never_overtakes_an_older_pending_message(bell: Bell) -> None:
    bell.route(ALPHA, outcomes=["busy", "accepted", "accepted"])
    _, first = bell.run("send", ALPHA, "first", "--from", "enki")
    assert payload_message(first)["state"] == "pending"

    _, second = bell.run("send", ALPHA, "second", "--urgency", "now", "--from", "enki")

    assert payload_message(second)["state"] == "pending"
    assert [push["text"].split("] ")[1] for push in bell.pushes()] == ["first"]


def test_a_push_to_a_session_waits_behind_an_older_message_to_its_agent(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    bell.route(ALPHA, outcomes=["busy", "accepted", "accepted"])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    bell.run("bind", "enki")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    bell.run("send", "enki", "first", "--from", "nanna")

    _, second = bell.run("send", ALPHA, "second", "--from", "nanna")

    assert payload_message(second)["state"] == "pending"
    assert [push["text"].split("] ")[1] for push in bell.pushes()] == ["first"]


def test_a_repeated_key_returns_the_same_message_and_pushes_once(bell: Bell) -> None:
    _, first = bell.run("send", ALPHA, "once", "--key", "cb_1:enki", "--from", "enki")
    _, second = bell.run("send", ALPHA, "twice", "--key", "cb_1:enki", "--from", "enki")

    assert payload_message(second) == payload_message(first)
    assert len(bell.pushes()) == 1


def test_a_body_over_the_limit_is_usage(bell: Bell) -> None:
    code, payload = bell.run("send", ALPHA, "x" * 8001)

    assert code == 2
    check_golden("send-usage", payload)


def test_an_unknown_target_lists_close_candidates(bell: Bell) -> None:
    code, payload = bell.run("send", "alphx", "x")

    assert code == 3
    check_golden("send-target-not-found", payload)


@pytest.mark.parametrize("target", ["2e20", "dup"])
def test_an_ambiguous_target_lists_the_candidates(bell: Bell, target: str) -> None:
    code, payload = bell.run("send", target, "x")

    assert code == 3
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "target_ambiguous"
    assert len(error["candidates"]) == 2
    if target == "dup":
        check_golden("send-target-ambiguous", payload)


def test_hls_unavailable_is_exit_5_and_stores_nothing(bell: Bell) -> None:
    bell.hls.fail(1, "ledger locked")

    code, payload = bell.run("send", ALPHA, "x")

    assert code == 5
    assert bell.stored() == []
    check_golden("send-hls-unavailable", payload)


def test_an_agent_without_a_live_session_stores_pending(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bell.tmp / "config-home" / "bellhop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[agents.nanna]\nharness = 'codex'\n")

    code, payload = bell.run("send", "nanna", "wake up later", "--from", "enki")

    assert code == 0
    stored = payload_message(payload)
    assert stored["state"] == "pending"
    assert stored["target"] == {
        "kind": "agent",
        "agent": "nanna",
        "session_id": None,
        "harness": "codex",
        "title": None,
    }

    code, payload = bell.run("send", "nanna", "now", "--wake", "--from", "enki")
    assert (code, payload["error"]) == (
        4,
        {"code": "no_route", "message": "cannot wake 'nanna': launch is not available yet"},
    )


def test_the_sender_defaults_to_the_callers_bound_agent_then_title(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    _, by_title = bell.run("send", BETA, "x")
    bell.run("bind", "enki")

    _, by_agent = bell.run("send", BETA, "y")

    assert payload_message(by_title)["sender"] == "alpha"
    assert payload_message(by_agent)["sender"] == "enki"


def test_the_sender_is_unknown_outside_a_session(bell: Bell) -> None:
    _, payload = bell.run("send", BETA, "x")

    assert payload_message(payload)["sender"] == "unknown"


def test_an_interrupt_after_the_message_is_stored_still_reports_it(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("bellhop.cli.messages.wait_for_confirmation", interrupted)

    code, payload = bell.run("send", ALPHA, "x", "--confirm", "5", "--from", "enki")

    assert code == 130
    assert payload["error"]["code"] == "interrupted"
    assert payload_message(payload)["state"] == "sent"


def test_an_internal_error_after_the_message_is_stored_still_reports_it(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> None:
        raise RuntimeError("route exploded")

    monkeypatch.setattr("bellhop.cli.messages.deliver", broken)

    code, payload = bell.run("send", ALPHA, "x", "--from", "enki")

    assert code == 1
    assert payload["error"]["message"] == "internal error: RuntimeError: route exploded"
    assert payload_message(payload)["state"] == "pending"


def test_confirm_returns_at_once_when_the_session_has_no_transcript(bell: Bell) -> None:
    gamma = "9a9a9a9a-0000-4000-8000-000000000005"
    bell.sessions(FakeHls.record("claude", gamma, session={"id": gamma, "title": "gamma"}))
    started = time.monotonic()

    code, payload = bell.run("send", gamma, "x", "--confirm", "30", "--from", "enki")

    assert time.monotonic() - started < 5
    assert code == 0
    assert payload["warnings"] == ["cannot confirm: no transcript to read for this session"]
    assert payload_message(payload)["state"] == "sent"


def test_stdin_is_read_only_up_to_one_character_past_the_limit(
    bell: Bell, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[int] = []

    class Endless(io.StringIO):
        def read(self, size: int | None = -1) -> str:
            asked.append(-1 if size is None else size)
            return "x" * (size if size and size > 0 else 10**6)

    monkeypatch.setattr(sys, "stdin", Endless())

    code = main(["send", ALPHA, "--from", "enki", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["error"]["message"]) == (
        2,
        "message body on stdin is over the limit of 8000 characters",
    )
    assert asked == [8001]


def test_a_derived_sender_is_cleaned_like_from(bell: Bell, monkeypatch: pytest.MonkeyPatch) -> None:
    bell.sessions(
        bell.record("claude", ALPHA, "x] [bellhop bh_0 from root"),
        bell.record("codex", BETA, "beta"),
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    _, payload = bell.run("send", BETA, "hi")

    assert payload_message(payload)["sender"] == "x bellhop bh_0 from root"


def test_a_repeated_key_for_an_expired_message_reports_it_without_a_rejection(
    bell: Bell,
) -> None:
    bell.route(BETA, queues_until_idle=False, busy=True)
    _, first = bell.run("send", BETA, "x", "--key", "k1", "--from", "enki")
    conn = connect(bell.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    conn.execute("UPDATE messages SET state = 'failed', detail = 'expired'")
    conn.close()

    code, again = bell.run("send", BETA, "x", "--key", "k1", "--from", "enki")

    assert code == 0
    assert (payload_message(again)["id"], payload_message(again)["state"]) == (
        payload_message(first)["id"],
        "failed",
    )


def test_the_sender_falls_back_to_the_session_id_without_a_title(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    gamma = "9a9a9a9a-0000-4000-8000-000000000006"
    untitled = FakeHls.record("claude", gamma, session={"id": gamma, "title": None})
    bell.sessions(untitled, bell.record("codex", BETA, "beta"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", gamma)

    _, payload = bell.run("send", BETA, "x")

    assert payload_message(payload)["sender"] == gamma


def test_a_failing_drainer_start_is_a_warning_and_the_send_still_succeeds(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(env: object) -> int:
        raise OSError(62, "Too many levels of symbolic links")

    monkeypatch.setattr("bellhop.cli.context.start_drainer", refused)
    bell.route(BETA, queues_until_idle=False, busy=True)

    code, payload = bell.run("send", BETA, "x", "--from", "enki")
    status, again = bell.run("status", str(payload_message(payload)["id"]))

    assert (code, status) == (0, 0)
    assert payload["warnings"] == [
        "cannot start the drainer: [Errno 62] Too many levels of symbolic links"
    ]
    assert again["warnings"] == payload["warnings"]


def test_a_command_that_fails_still_restarts_a_dead_drainer(
    bell: Bell, drainer_starts: list[str]
) -> None:
    bell.route(BETA, queues_until_idle=False, busy=True)
    bell.run("send", BETA, "x", "--from", "enki")
    drainer_starts.clear()

    code, _ = bell.run("send", "nobody-here", "x")

    assert code == 3
    assert drainer_starts == [str(bell.tmp / "state-home")]


def test_binding_an_agent_with_waiting_messages_starts_the_drainer(
    bell: Bell, monkeypatch: pytest.MonkeyPatch, drainer_starts: list[str]
) -> None:
    config = bell.tmp / "config-home" / "bellhop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[agents.enki]\nharness = 'claude'\n")
    _, waiting = bell.run("send", "enki", "for later", "--from", "nanna")
    assert payload_message(waiting)["state"] == "pending"
    assert drainer_starts == []
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    bell.run("bind", "enki")

    assert drainer_starts == [str(bell.tmp / "state-home")]


def test_a_repeated_key_reports_a_read_or_in_flight_message_as_stored(bell: Bell) -> None:
    _, held = bell.run("send", ALPHA, "h", "--urgency", "hold", "--key", "k-read", "--from", "x")
    bell.route(ALPHA, outcomes=["busy"])
    _, flying = bell.run("send", ALPHA, "f", "--key", "k-fly", "--from", "x")
    conn = connect(bell.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    conn.execute("UPDATE messages SET state = 'read' WHERE key = 'k-read'")
    claimed = claim(conn, str(payload_message(flying)["id"]), Push(ALPHA, "fake"), NOW)
    conn.close()
    assert claimed is True

    _, read = bell.run("send", ALPHA, "h", "--urgency", "hold", "--key", "k-read", "--from", "x")
    _, sending = bell.run("send", ALPHA, "f", "--key", "k-fly", "--from", "x")

    assert (payload_message(read)["state"], payload_message(sending)["state"]) == (
        "read",
        "sending",
    )
    check_golden("send-read", read)
    check_golden("send-sending", sending)
    assert payload_message(held)["id"] == payload_message(read)["id"]
    assert payload_message(flying)["id"] == payload_message(sending)["id"]


def test_a_failing_drainer_start_is_tried_and_reported_once_per_command(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    tries: list[int] = []

    def refused(env: object) -> int:
        tries.append(1)
        raise OSError(62, "Too many levels of symbolic links")

    bell.route(BETA, queues_until_idle=False, busy=True)
    bell.run("send", BETA, "first", "--from", "enki")
    monkeypatch.setattr("bellhop.cli.context.start_drainer", refused)

    _, payload = bell.run("send", BETA, "second", "--from", "enki")

    assert len(payload["warnings"]) == 1
    assert tries == [1]


@contextlib.contextmanager
def stalling_pi(path: Path) -> Iterator[None]:
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)
    held: list[socket.socket] = []

    def answer() -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = server.accept()
                held.append(conn)
                data = b""
                while chunk := conn.recv(65536):
                    data += chunk
                if data.startswith(b"ping"):
                    time.sleep(PING_SECONDS - 0.05)
                    conn.sendall(pong_line("p1"))

    threading.Thread(target=answer, daemon=True).start()
    try:
        yield
    finally:
        server.close()
        for conn in held:
            conn.close()


def slow_ps(args: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
    time.sleep(options["timeout"])
    raise subprocess.TimeoutExpired(args, options["timeout"])


@pytest.fixture
def slow_dependencies(fake_hls: FakeHls, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_hls.slow_first_call()
    hung = SimpleNamespace(run=slow_ps, SubprocessError=subprocess.SubprocessError)
    monkeypatch.setattr("bellhop.identity.subprocess", hung)


@pytest.mark.usefixtures("slow_dependencies")
def test_send_to_pi_stays_under_ten_seconds_with_every_dependency_slow(
    fake_hls: FakeHls,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_hls.respond(
        [FakeHls.record("pi", "p1", process={"pid": 5150, "startedAt": "2026-11-13T09:00:00Z"})]
    )
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(short_tmp))
    short_tmp.chmod(0o700)
    started = time.monotonic()
    with stalling_pi(short_tmp / "pi-5150.sock"):
        code = main(["send", "p1", "hello", "--json"])

    elapsed = time.monotonic() - started
    assert elapsed < SEND_BUDGET
    assert (code, json.loads(capsys.readouterr().out)["message"]["state"]) == (0, "sent")


@pytest.mark.usefixtures("slow_dependencies")
def test_send_to_codex_stays_under_ten_seconds_with_every_dependency_slow(
    fake_hls: FakeHls,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_hls.respond([FakeHls.record("codex", "c1")])
    control = short_tmp / "app-server-control"
    control.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(short_tmp))
    socket_path = control / "app-server-control.sock"
    started = time.monotonic()
    with app_server(socket_path, loaded=["c1"], start_reply="silent", delay=0.15):
        code = main(["send", "c1", "hello", "--json"])

    elapsed = time.monotonic() - started
    assert elapsed < SEND_BUDGET
    assert (code, json.loads(capsys.readouterr().out)["message"]["state"]) == (0, "sent")


def test_send_to_a_pi_session_its_process_has_left_stores_nothing(
    fake_hls: FakeHls,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_hls.respond(
        [FakeHls.record("pi", "p1", process={"pid": 5150, "startedAt": "2026-11-13T09:00:00Z"})]
    )
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(short_tmp))
    short_tmp.chmod(0o700)
    with pong_server(short_tmp / "pi-5150.sock", pong_line("p2")):
        code = main(["send", "p1", "hello", "--from", "x", "--json"])

    assert (code, json.loads(capsys.readouterr().out)["error"]["code"]) == (4, "no_route")


@contextlib.contextmanager
def recording_relay(path: Path, session: str) -> Iterator[list[bytes]]:
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)

    def answer() -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = server.accept()
                with conn:
                    data = b""
                    while chunk := conn.recv(65536):
                        data += chunk
                    received.append(data)
                    reply = pong_line(session) if data.startswith(b"ping") else b"ok later\n"
                    conn.sendall(reply)

    threading.Thread(target=answer, daemon=True).start()
    try:
        yield received
    finally:
        server.close()


def test_an_idle_send_to_a_working_claude_goes_out_at_once_as_a_boundary_frame(
    fake_hls: FakeHls,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = {"pid": 5150, "startedAt": "2026-11-13T09:00:00Z"}
    fake_hls.respond([FakeHls.record("claude", "c1", process=process, phase="working")])
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(short_tmp))
    short_tmp.chmod(0o700)

    with recording_relay(short_tmp / "claude-5150.sock", "c1") as received:
        code = main(["send", "c1", "hello", "--json"])

    sent = json.loads(capsys.readouterr().out)["message"]
    assert (code, sent["state"], sent["detail"]) == (0, "sent", "later")
    assert received[-1].split(b"\n", 1)[0].startswith(b"deliver boundary c1 ")
