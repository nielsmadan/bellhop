import json
from functools import partial
from importlib.metadata import version

import pytest

from bellhop.cli import main
from bellhop.cli.context import Context
from bellhop.cli.parsing import json_requested
from bellhop.hls import list_sessions
from conftest import FakeHls


def test_version_prints_the_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"bellhop {version('bellhop')}\n"


def test_option_prefixes_are_not_expanded(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--vers"]) == 2
    assert capsys.readouterr().err.startswith("bellhop: unrecognized arguments: --vers")


def test_missing_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert capsys.readouterr().err == "bellhop: missing command; see 'bellhop --help'\n"


def test_unknown_option_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--bogus"]) == 2
    assert capsys.readouterr().err.startswith("bellhop: unrecognized arguments: --bogus")


def test_usage_error_with_json_prints_the_error_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--bogus", "--json"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert captured.err == ""


def _records() -> list[object]:
    return [
        FakeHls.record("claude", "2e2042a9-937c-4e03", phase="working"),
        FakeHls.record("codex", "019a1b2c-3d4e", session={"id": "019a1b2c-3d4e"}),
        FakeHls.record("pi", "p1", phase="permission"),
        FakeHls.record("cursor", "u1"),
    ]


def test_sessions_text_lists_short_id_harness_phase_and_title(
    fake_hls: FakeHls, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hls.respond(_records())

    assert main(["sessions"]) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "ID        HARNESS  PHASE       TITLE",
        "2e2042a9  claude   working     title-2e2042a9-937c-4e03",
        "019a1b2c  codex    idle        -",
        "p1        pi       permission  title-p1",
    ]
    assert captured.err == ""


def test_sessions_text_escapes_control_characters_in_titles(
    fake_hls: FakeHls, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hls.respond([FakeHls.record("pi", "p1", session={"id": "p1", "title": "a\x1b[2Jb"})])

    assert main(["sessions"]) == 0

    assert capsys.readouterr().out.splitlines()[1] == "p1  pi       idle   a\\x1b[2Jb"


def test_sessions_text_says_when_nothing_is_live(
    fake_hls: FakeHls, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hls.respond([])

    assert main(["sessions"]) == 0

    assert capsys.readouterr().out == "no live sessions\n"


def test_sessions_text_prints_hls_problems_as_warnings_on_stderr(
    fake_hls: FakeHls, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hls.respond([FakeHls.record("pi", "p1")], problems=[{"message": "sink unreachable"}])

    assert main(["sessions"]) == 0

    assert capsys.readouterr().err == "bellhop: warning: sink unreachable\n"


def test_sessions_json_prints_exactly_one_object_and_nothing_on_stderr(
    fake_hls: FakeHls, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hls.respond(_records(), problems=[{"message": "sink unreachable"}])

    assert main(["sessions", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "ok"
    assert payload["warnings"] == ["sink unreachable"]
    assert [s["session_id"] for s in payload["sessions"]] == [
        "2e2042a9-937c-4e03",
        "019a1b2c-3d4e",
        "p1",
    ]
    assert payload["sessions"][0] == {
        "session_id": "2e2042a9-937c-4e03",
        "harness": "claude",
        "title": "title-2e2042a9-937c-4e03",
        "cwd": "/work/2e2042a9-937c-4e03",
        "phase": "working",
        "observed_at": "2026-11-13T09:12:03Z",
        "agent": None,
        "reachable": False,
        "route": None,
        "pending": 0,
    }
    assert payload["sessions"][1]["title"] is None
    assert payload["sessions"][1]["cwd"] is None


def test_sessions_without_hls_exits_5_with_the_json_error_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["sessions", "--json"]) == 5

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "schema_version": 1,
        "status": "error",
        "exit_code": 5,
        "error": {
            "code": "hls_unavailable",
            "message": "hooklinesinker not found; install it or set BELLHOP_HOOKLINESINKER",
        },
    }


def test_sessions_without_hls_exits_5_with_a_diagnostic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["sessions"]) == 5

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "bellhop: hooklinesinker not found; install it or set BELLHOP_HOOKLINESINKER\n"
    )


def test_sessions_succeeds_when_the_retry_follows_a_timeout(
    fake_hls: FakeHls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hls.respond([FakeHls.record("pi", "p1")])
    fake_hls.slow_first_call()
    monkeypatch.setattr("bellhop.cli.context.list_sessions", partial(list_sessions, budget=1.0))

    assert main(["sessions", "--json"]) == 0

    assert [s["session_id"] for s in json.loads(capsys.readouterr().out)["sessions"]] == ["p1"]
    assert len(fake_hls.calls()) == 2


def test_sessions_with_another_hls_protocol_exits_5(
    fake_hls: FakeHls, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hls.respond([FakeHls.record("pi", "p1")], protocol=2)

    assert main(["sessions", "--json"]) == 5

    assert json.loads(capsys.readouterr().out)["error"]["code"] == "hls_unavailable"


def test_sessions_refuses_a_relative_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "relative")

    assert main(["sessions", "--json"]) == 1

    assert json.loads(capsys.readouterr().out)["error"] == {
        "code": "invalid_environment",
        "message": "XDG_DATA_HOME must be an absolute path; got 'relative'",
    }


def test_interrupt_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(_env: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("bellhop.cli.context.list_sessions", interrupted)

    assert main(["sessions"]) == 130
    assert capsys.readouterr().err == "bellhop: interrupted\n"


def test_broken_pipe_exits_quietly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(_env: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr("bellhop.cli.context.list_sessions", broken)

    assert main(["sessions"]) == 1
    assert capsys.readouterr().err == ""


def test_unexpected_failure_with_json_prints_the_internal_error_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(_env: object) -> None:
        raise RuntimeError("store exploded")

    monkeypatch.setattr("bellhop.cli.context.list_sessions", failing)

    assert main(["sessions", "--json"]) == 1

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"] == {
        "code": "internal_error",
        "message": "internal error: RuntimeError: store exploded",
    }


def test_subcommand_option_prefixes_are_not_expanded(
    fake_hls: FakeHls, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hls.respond([])

    assert main(["sessions", "--js"]) == 2
    assert capsys.readouterr().err.startswith("bellhop: unrecognized arguments: --js")


def test_interrupt_with_json_prints_the_interrupted_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(_env: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("bellhop.cli.context.list_sessions", interrupted)

    assert main(["sessions", "--json"]) == 130

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "schema_version": 1,
        "status": "error",
        "exit_code": 130,
        "error": {"code": "interrupted", "message": "interrupted"},
    }


def test_json_after_the_end_of_options_is_text_not_a_flag() -> None:
    assert json_requested(["send", "x", "--", "--json"]) is False
    assert json_requested(["send", "x", "--json", "--", "y"]) is True


def test_a_context_keeps_the_callers_environment_out_of_its_repr() -> None:
    assert "secret-value" not in repr(Context({"GH_TOKEN": "secret-value"}))
