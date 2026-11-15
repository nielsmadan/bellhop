import json
import shutil
from datetime import UTC
from pathlib import Path

import pytest

from bellhop.transcripts.codex import CodexTranscripts, created_days, rollout
from conftest import make_session

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "transcripts" / "codex"
MIXED = "01a1d000-0000-7000-8000-00000000c0de"


def prefix(message_id: str) -> str:
    return f"[bellhop {message_id} from"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    sessions = tmp_path / ".codex" / "sessions"
    mixed_day = sessions / "2026" / "11" / "12"
    probe_day = sessions / "2026" / "10" / "24"
    for day in (mixed_day, probe_day):
        day.mkdir(parents=True)
    shutil.copy(
        FIXTURES / "bellhop-mixed.jsonl", mixed_day / f"rollout-2026-11-12T09-00-00-{MIXED}.jsonl"
    )
    shutil.copy(
        FIXTURES / "s1-probe.jsonl",
        probe_day / "rollout-2026-10-24T12-11-48-01a16dd2-da5b-7970-bab3-f3983307a86e.jsonl",
    )
    return tmp_path / ".codex"


def test_the_rollout_is_found_by_thread_id_and_a_user_entry_is_a_delivery(home: Path) -> None:
    session = make_session(MIXED, harness="codex", transcript_path=None)

    assert CodexTranscripts(home).contains(session, prefix("bh_eeeeeeeeeeee"), since=0) is True


@pytest.mark.parametrize(
    "message_id",
    ["bh_bbbbbbbbbbbb", "bh_cccccccccccc", "bh_dddddddddddd"],
    ids=["assistant text", "tool output", "quoted in a user entry"],
)
def test_the_prefix_anywhere_else_is_not_a_delivery(home: Path, message_id: str) -> None:
    session = make_session(MIXED, harness="codex", transcript_path=None)

    assert CodexTranscripts(home).contains(session, prefix(message_id), since=0) is False


def test_a_delivery_before_the_offset_does_not_count(home: Path) -> None:
    session = make_session(MIXED, harness="codex", transcript_path=None)
    reader = CodexTranscripts(home)
    offset = reader.size(session)
    assert offset is not None

    assert reader.contains(session, prefix("bh_eeeeeeeeeeee"), since=offset) is False


def test_a_rollout_of_another_thread_never_confirms(home: Path) -> None:
    other = home / "sessions" / "2026" / "11" / "12" / f"rollout-2026-11-12T09-00-00-{MIXED}.jsonl"
    session = make_session("someone-else", harness="codex", transcript_path=None)

    assert (
        CodexTranscripts(home).contains(session, prefix("bh_eeeeeeeeeeee"), since=0, path=other)
        is False
    )


def test_the_research_rollout_reads_its_user_entry(home: Path) -> None:
    session = make_session(
        "01a16dd2-da5b-7970-bab3-f3983307a86e", harness="codex", transcript_path=None
    )

    assert CodexTranscripts(home).contains(session, "ACK-REQUEST cafef00d", since=0) is True


def test_a_thread_without_a_rollout_has_no_size(tmp_path: Path) -> None:
    session = make_session("no-rollout", harness="codex", transcript_path=None)

    assert CodexTranscripts(tmp_path / ".codex").size(session) is None


def test_a_steered_entry_inside_an_open_turn_is_a_delivery(tmp_path: Path) -> None:
    thread = "01a17117-1088-75b1-9578-2ef044bf7599"
    day = tmp_path / ".codex" / "sessions" / "2026" / "10" / "25"
    day.mkdir(parents=True)
    shutil.copy(FIXTURES / "s2-steer.jsonl", day / f"rollout-2026-10-25T03-24-59-{thread}.jsonl")
    session = make_session(thread, harness="codex", transcript_path=None)

    assert CodexTranscripts(tmp_path / ".codex").contains(session, "ACK-REQUEST abcd1234", since=0)


def test_a_thread_id_names_the_day_its_rollout_was_created() -> None:
    days = created_days("01a1d994-2578-7000-8000-000000000000", UTC)

    assert days[0] == Path("2026/11/14")
    assert len(days) == 3


def test_the_rollout_is_found_in_its_creation_day(
    tmp_path: Path,
) -> None:
    thread = "01a1d994-2578-7000-8000-000000000000"
    dated = tmp_path / "2026" / "11" / "14" / f"rollout-2026-11-14T10-00-00-{thread}.jsonl"
    elsewhere = tmp_path / "2027" / "01" / "01" / f"rollout-2027-01-01T10-00-00-{thread}.jsonl"
    for path in (dated, elsewhere):
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")

    assert rollout(tmp_path, thread, UTC) == dated


def test_a_thread_id_that_names_no_creation_day_finds_nothing(tmp_path: Path) -> None:
    day = tmp_path / "2026" / "11" / "14"
    day.mkdir(parents=True)
    (day / "rollout-x-aaaa.jsonl").write_text("{}\n")
    (day / "rollout-x-[ab]*.jsonl").write_text("{}\n")

    assert rollout(tmp_path, "[ab]*") is None
    assert rollout(tmp_path, "*") is None
    assert rollout(tmp_path, "../x") is None


def test_a_rollout_outside_its_creation_days_is_not_searched_for(tmp_path: Path) -> None:
    thread = "01a1d994-2578-7000-8000-000000000000"
    far = tmp_path / "2027" / "01" / "01" / f"rollout-2027-01-01T10-00-00-{thread}.jsonl"
    far.parent.mkdir(parents=True)
    far.write_text("{}\n")

    assert rollout(tmp_path, thread, UTC) is None


def test_a_cached_rollout_that_disappears_is_looked_up_again(home: Path) -> None:
    session = make_session(MIXED, harness="codex", transcript_path=None)
    reader = CodexTranscripts(home)
    first = reader.path(session)
    assert first is not None
    moved = home / "sessions" / "2026" / "11" / "13" / first.name
    moved.parent.mkdir(parents=True)
    first.rename(moved)

    assert reader.path(session) == moved


def test_a_start_prompt_in_the_rollout_counts_as_delivered(tmp_path: Path) -> None:
    thread = "01a1d948-f680-7000-8000-000000000001"
    path = tmp_path / f"rollout-2026-11-14T09-00-00-{thread}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": thread}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "[bellhop bh_9 from x] hi"}],
            },
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    session = make_session(thread, harness="codex", transcript_path=path)

    assert CodexTranscripts(None).contains(session, "[bellhop bh_9 from", since=0, prompted=True)
