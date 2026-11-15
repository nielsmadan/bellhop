import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bellhop.errors import HlsUnavailable
from bellhop.hls import list_sessions, parse_sessions
from bellhop.models import Session
from conftest import FAKE_HLS, FakeHls


def _four_harness_records() -> list[object]:
    return [
        FakeHls.record("claude", "c1"),
        FakeHls.record("claude", "c2", phase="working"),
        FakeHls.record("codex", "x1", phase="permission"),
        FakeHls.record("pi", "p1", phase="compacting"),
        FakeHls.record("cursor", "u1"),
    ]


def _failing_hls(directory: Path) -> Path:
    directory.mkdir()
    executable = directory / "hooklinesinker"
    executable.write_text("#!/bin/sh\necho 'wrong hooklinesinker' >&2\nexit 9\n")
    executable.chmod(0o755)
    return directory


def test_maps_claude_codex_and_pi_sessions_and_skips_other_agents(fake_hls: FakeHls) -> None:
    fake_hls.respond(_four_harness_records())

    result = list_sessions(os.environ)

    assert [(s.harness, s.session_id, s.phase) for s in result.sessions] == [
        ("claude", "c1", "idle"),
        ("claude", "c2", "working"),
        ("codex", "x1", "permission"),
        ("pi", "p1", "compacting"),
    ]
    assert result.warnings == ()
    assert fake_hls.calls() == ["sessions --json"]


def test_maps_every_session_field(fake_hls: FakeHls) -> None:
    fake_hls.respond([FakeHls.record("claude", "c1")])

    assert list_sessions(os.environ).sessions == (
        Session(
            session_id="c1",
            binding_id="binding-c1",
            harness="claude",
            title="title-c1",
            cwd=Path("/work/c1"),
            transcript_path=Path("/transcripts/c1.jsonl"),
            phase="idle",
            observed_at=datetime(2026, 11, 13, 9, 12, 3, tzinfo=UTC),
            pid=4242,
            process_started_at=datetime(2026, 11, 13, 9, 0, 0, tzinfo=UTC),
            tmux_pane="%3",
            tmux_socket="/sockets/tmux-501/default",
        ),
    )


def test_missing_optional_details_map_to_none(fake_hls: FakeHls) -> None:
    record = FakeHls.record(
        "codex", "x1", session={"id": "x1"}, process=None, tmux=None, phase="napping"
    )
    fake_hls.respond([record])

    [session] = list_sessions(os.environ).sessions

    assert (session.title, session.cwd, session.transcript_path) == (None, None, None)
    assert (session.pid, session.process_started_at) == (None, None)
    assert (session.tmux_pane, session.tmux_socket) == (None, None)
    assert session.phase == "unknown"


def test_hls_problems_become_warnings(fake_hls: FakeHls) -> None:
    fake_hls.respond(
        [FakeHls.record("pi", "p1")],
        problems=[{"observedAt": "2026-11-13T09:00:00Z", "message": "sink unreachable"}, "plain"],
    )

    assert list_sessions(os.environ).warnings == ("sink unreachable", "plain")


def test_unrecognized_records_are_skipped_with_a_warning(fake_hls: FakeHls) -> None:
    fake_hls.respond(
        [
            FakeHls.record("claude", "c1"),
            FakeHls.record("claude", "c2", protocol=2),
            FakeHls.record("claude", "c3", observedAt="yesterday"),
            FakeHls.record("codex", "x1", session={"cwd": "/work"}),
            "not a record",
        ]
    )

    result = list_sessions(os.environ)

    assert [s.session_id for s in result.sessions] == ["c1"]
    assert len(result.warnings) == 4
    assert all(
        warning.startswith("skipped an unrecognized hooklinesinker record: ")
        for warning in result.warnings
    )


def test_stopped_sessions_are_skipped(fake_hls: FakeHls) -> None:
    fake_hls.respond([FakeHls.record("claude", "c1", running=False)])

    assert list_sessions(os.environ).sessions == ()


def test_absent_hls_is_unavailable() -> None:
    with pytest.raises(HlsUnavailable, match="hooklinesinker not found"):
        list_sessions(os.environ)


def test_missing_override_binary_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BELLHOP_HOOKLINESINKER", str(tmp_path / "nowhere"))

    with pytest.raises(HlsUnavailable, match="cannot run"):
        list_sessions(os.environ)


def test_retries_once_after_a_timeout(fake_hls: FakeHls) -> None:
    fake_hls.respond([FakeHls.record("pi", "p1")])
    fake_hls.slow_first_call()

    result = list_sessions(os.environ, timeout=0.5)

    assert [s.session_id for s in result.sessions] == ["p1"]
    assert fake_hls.calls() == ["sessions --json", "sessions --json"]


def test_two_timeouts_are_unavailable(fake_hls: FakeHls) -> None:
    fake_hls.always_slow()

    with pytest.raises(HlsUnavailable, match="timed out twice"):
        list_sessions(os.environ, timeout=0.2)
    assert len(fake_hls.calls()) == 2


def test_nonzero_exit_is_unavailable(fake_hls: FakeHls) -> None:
    fake_hls.fail(1, "ledger locked")

    with pytest.raises(HlsUnavailable, match="exited 1: ledger locked"):
        list_sessions(os.environ)


def test_other_protocol_is_unavailable(fake_hls: FakeHls) -> None:
    fake_hls.respond([FakeHls.record("claude", "c1")], protocol=2)

    with pytest.raises(HlsUnavailable, match="speaks protocol 2"):
        list_sessions(os.environ)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "is not JSON"),
        ("[]", "is not a JSON object"),
        ('{"protocol": true, "sessions": []}', "speaks protocol True"),
        ('{"protocol": 1, "sessions": {}}', "lacks a sessions or problems list"),
        ('{"protocol": 1, "sessions": [], "problems": {}}', "lacks a sessions or problems list"),
    ],
)
def test_unreadable_envelopes_are_unavailable(text: str, message: str) -> None:
    with pytest.raises(HlsUnavailable, match=message):
        parse_sessions(text)


def test_env_override_wins_over_path(
    fake_hls: FakeHls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_hls.respond([FakeHls.record("pi", "p1")])
    monkeypatch.setenv("PATH", f"{_failing_hls(tmp_path / 'bin')}{os.pathsep}{os.defpath}")

    assert [s.session_id for s in list_sessions(os.environ).sessions] == ["p1"]


def test_active_binary_under_xdg_data_home_wins_over_path(
    fake_hls: FakeHls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_hls.respond([FakeHls.record("pi", "p1")])
    monkeypatch.delenv("BELLHOP_HOOKLINESINKER")
    active = tmp_path / "data-home" / "hooklinesinker" / "bin" / "hooklinesinker"
    active.parent.mkdir(parents=True)
    active.symlink_to(FAKE_HLS)
    monkeypatch.setenv("PATH", f"{_failing_hls(tmp_path / 'bin')}{os.pathsep}{os.defpath}")

    assert [s.session_id for s in list_sessions(os.environ).sessions] == ["p1"]


def test_falls_back_to_path(
    fake_hls: FakeHls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_hls.respond([FakeHls.record("pi", "p1")])
    monkeypatch.delenv("BELLHOP_HOOKLINESINKER")
    directory = tmp_path / "bin"
    directory.mkdir()
    (directory / "hooklinesinker").symlink_to(FAKE_HLS)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.defpath}")

    assert [s.session_id for s in list_sessions(os.environ).sessions] == ["p1"]


def test_undecodable_bytes_are_replaced(fake_hls: FakeHls) -> None:
    fake_hls.respond([FakeHls.record("pi", "p1")])
    output = Path(os.environ["FAKE_HLS_OUTPUT"])
    output.write_bytes(output.read_bytes().replace(b"title-p1", b"title-\xffp1"))

    [session] = list_sessions(os.environ).sessions

    assert session.title == "title-�p1"
