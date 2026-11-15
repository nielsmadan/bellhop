from pathlib import Path

from bellhop.transcripts import PlainTranscripts, reader_finder
from conftest import make_session


def test_size_and_contains_read_from_the_offset(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("[bellhop bh_1 from a] old\n")
    session = make_session("s1", transcript_path=path)
    reader = PlainTranscripts()
    offset = reader.size(session)
    assert offset is not None
    with path.open("a") as handle:
        handle.write("[bellhop bh_2 from a] new\n")

    assert reader.contains(session, "[bellhop bh_2 from", since=offset) is True
    assert reader.contains(session, "[bellhop bh_1 from", since=offset) is False


def test_a_missing_transcript_has_no_size_and_contains_nothing(tmp_path: Path) -> None:
    session = make_session("s1", transcript_path=tmp_path / "gone.jsonl")
    reader = PlainTranscripts()

    assert (reader.size(session), reader.contains(session, "x", since=0)) == (None, False)


def test_the_plain_reader_is_used_only_with_fake_routes() -> None:
    session = make_session("s1")

    assert reader_finder({})(session) is None
    assert isinstance(reader_finder({"BELLHOP_FAKE_ROUTES": "/r.json"})(session), PlainTranscripts)
