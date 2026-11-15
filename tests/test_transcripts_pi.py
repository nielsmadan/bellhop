import os
import shutil
from pathlib import Path

import pytest

from bellhop.transcripts.common import complete_lines
from bellhop.transcripts.pi import PiTranscripts, session_dir
from conftest import make_session

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "transcripts" / "pi"
MIXED_ID = "9f1d2c3b-0000-4000-8000-00000000pi01"


def prefix(message_id: str) -> str:
    return f"[bellhop {message_id} from"


@pytest.fixture
def mixed(tmp_path: Path) -> Path:
    path = tmp_path / "mixed.jsonl"
    shutil.copy(FIXTURES / "bellhop-mixed.jsonl", path)
    return path


def test_the_prefix_at_the_start_of_a_user_entry_is_a_delivery(mixed: Path) -> None:
    session = make_session(MIXED_ID, harness="pi", transcript_path=mixed)

    assert PiTranscripts(Path("/none")).contains(session, prefix("bh_cccccccccccc"), since=0)


@pytest.mark.parametrize(
    "message_id",
    [
        "bh_aaaaaaaaaaaa",
        "bh_bbbbbbbbbbbb",
        "bh_dddddddddddd",
        "bh_ffffffffffff",
        "bh_gggggggggggg",
    ],
    ids=["assistant text", "tool result", "quoted in a user entry", "tool call", "custom data"],
)
def test_the_prefix_anywhere_else_is_not_a_delivery(mixed: Path, message_id: str) -> None:
    session = make_session(MIXED_ID, harness="pi", transcript_path=mixed)

    assert PiTranscripts(Path("/none")).contains(session, prefix(message_id), since=0) is False


def test_an_extension_injected_message_counts_as_received(mixed: Path) -> None:
    session = make_session(MIXED_ID, harness="pi", transcript_path=mixed)

    assert PiTranscripts(Path("/none")).contains(session, prefix("bh_eeeeeeeeeeee"), since=0)


def test_a_delivery_before_the_offset_does_not_count(mixed: Path) -> None:
    session = make_session(MIXED_ID, harness="pi", transcript_path=mixed)
    reader = PiTranscripts(Path("/none"))
    offset = reader.size(session)
    assert offset is not None

    assert reader.contains(session, prefix("bh_cccccccccccc"), since=offset) is False


def test_the_research_fixtures_parse_their_user_entries() -> None:
    session = make_session(
        "2c11abff-8ce0-458a-ae1e-19f09db9f102",
        harness="pi",
        transcript_path=FIXTURES / "ext-turn.jsonl",
    )

    assert PiTranscripts(Path("/none")).contains(session, "ACK-REQUEST 62633465", since=0)


def test_each_poll_reads_only_what_was_appended_since_the_last(
    mixed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session(MIXED_ID, harness="pi", transcript_path=mixed)
    reader = PiTranscripts(Path("/none"))
    starts: list[int] = []

    def recording(path: Path, start: int) -> tuple[list[bytes], int]:
        starts.append(start)
        return complete_lines(path, start)

    monkeypatch.setattr("bellhop.transcripts.common.complete_lines", recording)
    size = mixed.stat().st_size

    assert reader.contains(session, prefix("bh_999999999999"), since=0) is False
    with mixed.open("a") as handle:
        handle.write('{"type":"message","message":{"role":"user","content":"[bellhop bh_99')
    assert reader.contains(session, prefix("bh_999999999999"), since=0) is False
    with mixed.open("a") as handle:
        handle.write('9999999999 from enki] late"}}\n')
    assert reader.contains(session, prefix("bh_999999999999"), since=0) is True

    assert starts == [0, size, size]


def test_without_a_transcript_path_the_session_file_is_found_by_cwd_and_id(
    tmp_path: Path,
) -> None:
    agent_dir = tmp_path / "agent"
    directory = session_dir(agent_dir, Path("/work/pi"))
    directory.mkdir(parents=True)
    shutil.copy(FIXTURES / "bellhop-mixed.jsonl", directory / "2026-11-14_mixed.jsonl")
    shutil.copy(FIXTURES / "ext-turn.jsonl", directory / "2026-10-27_other.jsonl")
    session = make_session(MIXED_ID, harness="pi", transcript_path=None, cwd=Path("/work/pi"))

    reader = PiTranscripts(agent_dir)

    assert directory.name == "--work-pi--"
    assert reader.size(session) == (FIXTURES / "bellhop-mixed.jsonl").stat().st_size
    assert reader.contains(session, prefix("bh_cccccccccccc"), since=0) is True


def test_a_session_whose_file_pi_has_not_written_yet_starts_at_zero(tmp_path: Path) -> None:
    session = make_session(MIXED_ID, harness="pi", transcript_path=None, cwd=Path("/work/pi"))
    reader = PiTranscripts(tmp_path)
    directory = session_dir(tmp_path, Path("/work/pi"))
    directory.mkdir(parents=True)

    offset = reader.size(session)
    shutil.copy(FIXTURES / "bellhop-mixed.jsonl", directory / "first.jsonl")

    assert offset == 0
    assert reader.contains(session, prefix("bh_cccccccccccc"), since=offset) is True


def test_a_session_whose_pi_directory_does_not_exist_cannot_be_confirmed(tmp_path: Path) -> None:
    session = make_session("p1", harness="pi", transcript_path=None, cwd=Path("/work/elsewhere"))

    assert PiTranscripts(tmp_path).size(session) is None


def test_a_fifo_named_like_a_session_file_is_skipped(tmp_path: Path) -> None:
    directory = session_dir(tmp_path, Path("/work/pi"))
    directory.mkdir(parents=True)
    os.mkfifo(directory / "a.jsonl")
    shutil.copy(FIXTURES / "bellhop-mixed.jsonl", directory / "b.jsonl")
    session = make_session(MIXED_ID, harness="pi", transcript_path=None, cwd=Path("/work/pi"))

    assert PiTranscripts(tmp_path).path(session) == directory / "b.jsonl"


def test_a_transcript_that_shrank_is_scanned_again_from_the_offset(mixed: Path) -> None:
    session = make_session(MIXED_ID, harness="pi", transcript_path=mixed)
    reader = PiTranscripts(Path("/none"))
    assert reader.contains(session, prefix("bh_777777777777"), since=0) is False

    mixed.write_text(
        '{"type":"message","message":{"role":"user","content":"[bellhop bh_777777777777 from x] y"}}\n'
    )

    assert reader.contains(session, prefix("bh_777777777777"), since=0) is True


def test_a_real_pi_extension_message_counts_as_received() -> None:
    session = make_session(
        "d361f923-716d-424d-a0bb-4fbd5bfef268",
        harness="pi",
        transcript_path=FIXTURES / "custom.jsonl",
    )

    assert PiTranscripts(Path("/none")).contains(session, "ACK-REQUEST c0ffee01", since=0)


def test_a_session_with_neither_a_path_nor_a_cwd_has_no_size(tmp_path: Path) -> None:
    session = make_session("p1", harness="pi", transcript_path=None, cwd=None)

    reader = PiTranscripts(tmp_path)

    assert (reader.size(session), reader.contains(session, "x", since=0)) == (None, False)


def test_the_transcript_the_extension_reports_wins_over_the_cwd_lookup(
    tmp_path: Path, mixed: Path
) -> None:
    by_cwd = session_dir(tmp_path, Path("/work/pi"))
    by_cwd.mkdir(parents=True)
    header = f'{{"type":"session","id":"{MIXED_ID}","timestamp":"2026-11-14T09:00:00Z"}}\n'
    (by_cwd / "by-cwd.jsonl").write_text(header)
    session = make_session(MIXED_ID, harness="pi", transcript_path=None, cwd=Path("/work/pi"))
    reader = PiTranscripts(tmp_path, reported=lambda s: mixed)

    assert reader.path(session) == mixed
    assert reader.contains(session, prefix("bh_cccccccccccc"), since=0) is True


def test_a_reported_transcript_pi_has_not_written_yet_starts_at_zero(tmp_path: Path) -> None:
    later = tmp_path / "custom-dir" / "later.jsonl"
    session = make_session(MIXED_ID, harness="pi", transcript_path=None, cwd=Path("/work/pi"))
    reader = PiTranscripts(None, reported=lambda s: later)

    offset = reader.size(session)
    later.parent.mkdir()
    shutil.copy(FIXTURES / "bellhop-mixed.jsonl", later)

    assert offset == 0
    assert reader.contains(session, prefix("bh_cccccccccccc"), since=0) is True
