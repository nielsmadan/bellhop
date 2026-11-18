import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bellhop.trace import OLD_NAME, read_events, rotate, trace_path, tracer

AT = datetime(2026, 11, 15, 14, 53, 16, 250000, tzinfo=UTC)


def env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(tmp_path / "state"), **extra}


def lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_an_event_is_one_json_line_without_its_empty_fields(tmp_path: Path) -> None:
    emit = tracer(env(tmp_path), "cli", now=lambda: AT)

    emit("push", session="s1", outcome="busy", detail=None, ms=12)

    [event] = lines(trace_path(env(tmp_path)))
    assert {name: event[name] for name in ("at", "src", "event")} == {
        "at": "2026-11-15T14:53:16.250Z",
        "src": "cli",
        "event": "push",
    }
    assert {name: event.get(name) for name in ("session", "outcome", "detail", "ms")} == {
        "session": "s1",
        "outcome": "busy",
        "detail": None,
        "ms": 12,
    }
    assert "detail" not in event


def test_long_text_is_cut_and_says_how_long_it_was(tmp_path: Path) -> None:
    tracer(env(tmp_path), "cli")("command", argv=["send", "x" * 1000])

    [event] = lines(trace_path(env(tmp_path)))
    assert event["argv"] == ["send", "x" * 300 + "… (1000 characters)"]


def test_the_trace_file_is_private(tmp_path: Path) -> None:
    tracer(env(tmp_path), "cli")("command")

    assert trace_path(env(tmp_path)).stat().st_mode & 0o777 == 0o600


def test_tracing_can_be_switched_off(tmp_path: Path) -> None:
    tracer(env(tmp_path, BELLHOP_TRACE="off"), "cli")("command")

    assert not trace_path(env(tmp_path)).exists()


def test_a_state_dir_that_cannot_be_written_never_fails_the_caller(tmp_path: Path) -> None:
    (tmp_path / "state").write_text("a file where the directory should be")

    tracer(env(tmp_path), "cli")("command")

    assert (tmp_path / "state").read_text() == "a file where the directory should be"


def test_a_planted_link_is_not_written_through(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("mine")
    path = trace_path(env(tmp_path))
    path.parent.mkdir(parents=True)
    path.symlink_to(victim)

    tracer(env(tmp_path), "cli")("command")

    assert victim.read_text() == "mine"


def test_a_full_file_is_set_aside_and_still_read(tmp_path: Path) -> None:
    emit = tracer(env(tmp_path), "cli")
    emit("first")
    path = trace_path(env(tmp_path))

    rotate(path, limit=10)
    emit("second")

    assert [event["event"] for event in lines(path.with_name(OLD_NAME))] == ["first"]
    assert [event["event"] for event in read_events(env(tmp_path), count=10)] == ["first", "second"]


def test_reading_keeps_the_last_events_that_hold_every_word(tmp_path: Path) -> None:
    emit = tracer(env(tmp_path), "cli")
    emit("push", message="bh_1", session="s1")
    emit("push", message="bh_2", session="s1")
    emit("push", message="bh_3", session="s2")
    with trace_path(env(tmp_path)).open("a") as handle:
        handle.write("not json\n")

    found = read_events(env(tmp_path), count=1, words=["push", "s1"])

    assert [event["message"] for event in found] == ["bh_2"]


def test_failures_are_what_failed_was_refused_or_went_unanswered(tmp_path: Path) -> None:
    emit = tracer(env(tmp_path), "cli")
    emit("command", argv=["sessions"], exit=0)
    emit("command", argv=["send"], exit=4, code="no_route", error="no usable route")
    emit("push", message="bh_1", outcome="accepted")
    emit("push", message="bh_2", outcome="busy")
    emit("push", message="bh_3", outcome="rejected")
    emit("push", message="bh_4", outcome="raised")
    emit("message", id="bh_5", state="failed")
    emit("message", id="bh_6", state="confirmed")
    emit("fail", target="alpha")
    emit("hls", error="hooklinesinker gave no answer")
    emit("stop", exit=129, signal=1)
    emit("stop", exit=0)

    found = read_events(env(tmp_path), count=3, failures=True)
    every = read_events(env(tmp_path), count=100, failures=True)

    assert [event["event"] for event in found] == ["fail", "hls", "stop"]
    assert [
        event.get("message") or event.get("id") or event.get("code") for event in every[:4]
    ] == [
        "no_route",
        "bh_3",
        "bh_4",
        "bh_5",
    ]
    assert len(every) == 7


def test_a_file_that_cannot_be_set_aside_is_emptied_instead_of_growing(tmp_path: Path) -> None:
    emit = tracer(env(tmp_path), "cli")
    emit("first")
    path = trace_path(env(tmp_path))
    path.with_name(OLD_NAME).mkdir()

    rotate(path, limit=10)
    emit("second")

    assert [event["event"] for event in lines(path)] == ["second"]


def test_reading_takes_the_end_of_a_long_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emit = tracer(env(tmp_path), "cli")
    for number in range(50):
        emit("push", number=number)
    monkeypatch.setattr("bellhop.trace.READ_LIMIT", 400)

    found = read_events(env(tmp_path), count=2)

    assert [event["number"] for event in found] == [48, 49]


def test_a_removed_working_directory_does_not_fail_a_command(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    script = 'rmdir "$PWD" && exec "$0" -m bellhop --version'

    done = subprocess.run(
        ["/bin/sh", "-c", script, sys.executable],
        cwd=gone,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.startswith("bellhop ")
    [event] = read_events(os.environ, count=5)
    assert (event["argv"], event.get("cwd")) == (["--version"], None)
