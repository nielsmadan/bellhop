import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from bellhop.codex_names import INDEX_NAME, named, thread_names, transcript_home
from bellhop.hls import list_sessions
from bellhop.models import Harness, Session
from conftest import FakeHls


def session(session_id: str, harness: Harness = "codex", transcript: Path | None = None) -> Session:
    return Session(
        session_id=session_id,
        binding_id=f"binding-{session_id}",
        harness=harness,
        title=None,
        cwd=None,
        transcript_path=transcript,
        phase="idle",
        observed_at=datetime(2026, 11, 15, tzinfo=UTC),
        pid=None,
        process_started_at=None,
        tmux_pane=None,
        tmux_socket=None,
    )


def write_index(home: Path, *entries: object) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lines = [entry if isinstance(entry, str) else json.dumps(entry) for entry in entries]
    (home / INDEX_NAME).write_text("".join(f"{line}\n" for line in lines))


def rollout(home: Path, session_id: str) -> Path:
    return home / "sessions" / "2026" / "11" / "15" / f"rollout-{session_id}.jsonl"


def test_the_last_name_recorded_for_a_thread_wins(tmp_path: Path) -> None:
    write_index(
        tmp_path,
        {"id": "t1", "thread_name": "Respond to greeting", "updated_at": "2026-11-15T14:52:01Z"},
        {"id": "t2", "thread_name": "Fix the build", "updated_at": "2026-11-15T14:52:05Z"},
        {"id": "t1", "thread_name": "bellhop-test-codex", "updated_at": "2026-11-15T14:52:14Z"},
    )

    assert thread_names(tmp_path) == {"t1": "bellhop-test-codex", "t2": "Fix the build"}


def test_lines_that_are_not_name_entries_are_skipped(tmp_path: Path) -> None:
    write_index(
        tmp_path,
        '{"id": "t1", "thread_na',
        ["t1", "a list"],
        {"id": "t1", "thread_name": 7},
        {"id": "t2", "thread_name": "   "},
        {"id": "t3", "thread_name": " kept "},
    )

    assert thread_names(tmp_path) == {"t3": "kept"}


def test_a_home_without_an_index_has_no_names(tmp_path: Path) -> None:
    assert thread_names(tmp_path / "missing") == {}


def test_a_codex_home_is_read_off_the_rollout_path(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"

    assert transcript_home(session("t1", transcript=rollout(home, "t1"))) == home
    assert transcript_home(session("t1", transcript=tmp_path / "elsewhere" / "t1.jsonl")) is None
    assert transcript_home(session("p1", harness="pi", transcript=rollout(home, "p1"))) is None


def test_a_codex_session_takes_its_name_from_its_own_home(tmp_path: Path) -> None:
    own, default = tmp_path / "own", tmp_path / "default"
    write_index(own, {"id": "t1", "thread_name": "from its own home"})
    write_index(
        default,
        {"id": "t1", "thread_name": "from the default home"},
        {"id": "t2", "thread_name": "found in the default home"},
    )
    sessions = [session("t1", transcript=rollout(own, "t1")), session("t2"), session("t3")]

    titles = [found.title for found in named(sessions, default)]

    assert titles == ["from its own home", "found in the default home", None]


def test_only_untitled_codex_sessions_are_named(tmp_path: Path) -> None:
    write_index(tmp_path, {"id": "c1", "thread_name": "a codex name"})
    write_index(tmp_path, {"id": "x1", "thread_name": "a codex name"})
    claude = session("c1", harness="claude")
    titled = replace(session("x1"), title="reported by hooklinesinker")

    assert named([claude, titled], tmp_path) == (claude, titled)


def test_listed_codex_sessions_carry_the_name_given_in_codex(fake_hls: FakeHls) -> None:
    home = Path(os.environ["HOME"]) / ".codex"
    write_index(home, {"id": "x1", "thread_name": "bellhop-test-codex"})
    untitled = {"id": "x1", "cwd": "/work/x1", "transcriptPath": str(rollout(home, "x1"))}
    fake_hls.respond([FakeHls.record("codex", "x1", session=untitled)])

    [listed] = list_sessions(os.environ).sessions

    assert listed.title == "bellhop-test-codex"
