import pytest

from bellhop.cli import main
from conftest import ALPHA, BETA, Bell, check_golden


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
