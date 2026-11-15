import json
import os
from pathlib import Path

import pytest

from bellhop.cli import main
from bellhop.cli.context import Context
from bellhop.cli.info import probed_rows
from bellhop.models import Binding
from bellhop.store import connect, get_binding, save_binding_unless
from conftest import ALPHA, BETA, NOW, Bell, FakeHls, check_golden, pong_line, pong_server
from fake_app_server import app_server


def test_sessions_report_agent_route_reachability_and_pending(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    bell.route(BETA, none=True)
    bell.run("send", ALPHA, "x", "--urgency", "hold", "--from", "enki")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    bell.run("bind", "enki")

    code, payload = bell.run("sessions")

    assert code == 0
    rows = {row["session_id"]: row for row in payload["sessions"]}
    assert (
        rows[ALPHA]["agent"],
        rows[ALPHA]["route"],
        rows[ALPHA]["reachable"],
        rows[ALPHA]["pending"],
    ) == (
        "enki",
        "fake",
        True,
        1,
    )
    assert (rows[BETA]["route"], rows[BETA]["reachable"]) == (None, False)
    check_golden("sessions", payload)


def test_whoami_names_the_session_and_its_agent(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    bell.run("bind", "enki")

    code, payload = bell.run("whoami")

    assert code == 0
    assert payload["session"] == {
        "session_id": ALPHA,
        "harness": "claude",
        "title": "alpha",
        "agent": "enki",
    }
    check_golden("whoami", payload)


def test_whoami_with_an_unlisted_session_id_is_exit_3(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "not-live")

    code, payload = bell.run("whoami")

    assert code == 3
    assert payload["error"]["code"] == "caller_unknown"
    check_golden("whoami-caller-unknown", payload)


def test_agents_list_configured_and_bound_names(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bell.tmp / "config-home" / "bellhop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[agents.nanna]\nharness = 'codex'\n\n[agents.enki]\nharness = 'claude'\n")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    bell.run("bind", "enki")
    bell.run("bind", "scribe")

    code, payload = bell.run("agents")

    assert code == 0
    assert payload["agents"] == [
        {
            "agent": "enki",
            "harness": "claude",
            "configured": True,
            "binding": {"state": "live", "session_id": ALPHA, "cause": "bind", "detail": None},
        },
        {"agent": "nanna", "harness": "codex", "configured": True, "binding": None},
        {
            "agent": "scribe",
            "harness": "claude",
            "configured": False,
            "binding": {"state": "live", "session_id": ALPHA, "cause": "bind", "detail": None},
        },
    ]
    check_golden("agents", payload)


def test_bind_refuses_a_name_held_by_another_live_session(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", BETA)
    bell.run("bind", "enki")
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    code, payload = bell.run("bind", "enki")

    assert code == 1
    assert payload["error"] == {
        "code": "binding_conflict",
        "message": f"'enki' is bound to live session {BETA}",
    }
    check_golden("bind-conflict", payload)


def test_bind_takes_over_a_dead_binding(bell: Bell, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    bell.run("bind", "enki")
    monkeypatch.setattr("bellhop.cli.context.process_table", dict)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    code, payload = bell.run("bind", "enki")

    assert code == 0
    assert payload["agent"]["binding"]["state"] == "live"
    check_golden("bind", payload)


def test_bind_checks_the_name_and_the_configured_harness(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bell.tmp / "config-home" / "bellhop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[agents.nanna]\nharness = 'codex'\n")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    assert bell.run("bind", "Bad Name")[0] == 2
    code, payload = bell.run("bind", "nanna")

    assert code == 2
    assert (
        payload["error"]["message"]
        == "'nanna' is configured as a codex agent; this session is claude"
    )


def test_whoami_text_escapes_a_title_with_terminal_controls(
    bell: Bell, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bell.sessions(bell.record("claude", ALPHA, "a\x1b[2Jb"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    main(["whoami"])

    assert capsys.readouterr().out == f"{ALPHA} claude a\\x1b[2Jb -\n"


def test_sessions_ping_a_pi_extension_for_reachable_and_route(
    fake_hls: FakeHls,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_tmp: Path,
) -> None:
    runtime = short_tmp / "rt"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(runtime))
    process = {"pid": 5150, "startedAt": "2026-11-13T09:00:00Z"}
    fake_hls.respond(
        [FakeHls.record("pi", "p-live", process=process), FakeHls.record("pi", "p-quiet")]
    )
    with pong_server(runtime / "pi-5150.sock", pong_line("p-live")):
        assert main(["sessions", "--json"]) == 0

    rows = {row["session_id"]: row for row in json.loads(capsys.readouterr().out)["sessions"]}
    assert (rows["p-live"]["reachable"], rows["p-live"]["route"]) == (True, "pi-extension")
    assert (rows["p-quiet"]["reachable"], rows["p-quiet"]["route"]) == (False, None)


def test_sessions_list_every_harness_when_the_runtime_dir_is_unsafe(
    fake_hls: FakeHls,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "rt"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(runtime))
    fake_hls.respond([FakeHls.record("pi", "p1"), FakeHls.record("claude", "c1")])

    assert main(["sessions", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [(r["session_id"], r["reachable"]) for r in payload["sessions"]] == [
        ("p1", False),
        ("c1", False),
    ]
    assert payload["warnings"] == [
        f"cannot reach {harness} sessions: runtime directory {runtime} has mode 0755; it must be 0700"
        for harness in ("pi", "claude")
    ]


def test_send_to_pi_with_an_unsafe_runtime_dir_is_exit_1(
    fake_hls: FakeHls,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "rt"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(runtime))
    fake_hls.respond([FakeHls.record("pi", "p1")])

    assert main(["send", "p1", "hi", "--from", "x", "--json"]) == 1

    assert json.loads(capsys.readouterr().out)["error"]["code"] == "runtime_dir_unsafe"


@pytest.mark.parametrize("extra", [["--urgency", "hold"], ["--or-hold"]])
def test_holds_to_pi_survive_an_unsafe_runtime_dir(
    fake_hls: FakeHls,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> None:
    runtime = tmp_path / "rt"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(runtime))
    fake_hls.respond([FakeHls.record("pi", "p1")])

    assert main(["send", "p1", "hi", "--from", "x", *extra, "--json"]) == 0

    stored = json.loads(capsys.readouterr().out)["message"]
    assert (stored["urgency"], stored["state"]) == ("hold", "pending")


def test_a_codex_binding_keeps_the_app_server_of_the_session_that_bound(
    bell: Bell, monkeypatch: pytest.MonkeyPatch, short_tmp: Path
) -> None:
    control = short_tmp / "codex-home" / "app-server-control"
    control.mkdir(parents=True)
    monkeypatch.delenv("BELLHOP_FAKE_ROUTES")
    monkeypatch.setenv("CODEX_THREAD_ID", BETA)
    monkeypatch.setenv("CODEX_HOME", str(short_tmp / "codex-home"))
    with app_server(control / "app-server-control.sock", loaded=[BETA]) as server:
        bell.run("bind", "nanna")
        monkeypatch.delenv("CODEX_THREAD_ID")
        monkeypatch.delenv("CODEX_HOME")
        code, payload = bell.run("send", "nanna", "hi", "--from", "x")

    assert (code, payload["message"]["state"]) == (0, "sent")
    assert server.methods()[-1] == "turn/start"


def test_sessions_stop_checking_routes_once_their_budget_is_spent(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bellhop.cli.info.PROBE_BUDGET_SECONDS", 0.0)

    code, payload = bell.run("sessions")

    assert code == 0
    assert {row["reachable"] for row in payload["sessions"]} == {False}
    assert payload["warnings"] == [
        "stopped checking routes after 0s; the last 4 sessions show reachable: false unchecked"
    ]


def test_a_route_check_that_ends_past_the_budget_skips_nothing(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 9.0])
    ctx = Context(dict(os.environ))

    try:
        rows, skipped = probed_rows(ctx.sessions.sessions, ctx, clock=lambda: next(ticks))
    finally:
        ctx.conn.close()

    assert skipped == 0
    assert len(rows) == 4


def test_bind_inside_a_launched_codex_agent_keeps_its_own_app_server(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(bell.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    launched = Binding(
        "nanna",
        "codex",
        "live",
        BETA,
        None,
        None,
        "/rt/codex-nanna-ab.sock",
        4343,
        "-S /t nanna:%0",
        "launch",
        None,
        NOW,
    )
    save_binding_unless(conn, launched, lambda existing: False)
    conn.close()
    monkeypatch.setenv("CODEX_THREAD_ID", BETA)
    monkeypatch.setenv("CODEX_HOME", str(bell.tmp / "other-codex"))

    bell.run("bind", "nanna")
    bell.run("bind", "nanna")

    conn = connect(bell.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    binding = get_binding(conn, "nanna")
    conn.close()
    assert binding is not None
    assert (binding.app_server_socket, binding.cause) == ("/rt/codex-nanna-ab.sock", "bind")


def test_bind_refuses_a_name_over_64_characters_as_usage(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    code, payload = bell.run("bind", "a" * 300)

    assert (code, payload["error"]["code"]) == (2, "usage")
