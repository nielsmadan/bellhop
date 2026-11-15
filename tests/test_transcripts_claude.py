import json
from pathlib import Path

import pytest

from bellhop.transcripts.claude import ClaudeTranscripts
from conftest import make_session

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "transcripts" / "claude"
RELAY = FIXTURES / "bellhop-mixed.jsonl"
RELAY_ID = "c1a0de00-0000-4000-8000-00000000c1de"


def prefix(message_id: str) -> str:
    return f"[bellhop {message_id} from"


@pytest.mark.parametrize(
    "message_id",
    ["bh_eeeeeeeeeeee", "bh_111111111111", "bh_222222222222"],
    ids=["at the boundary", "mid-turn", "plugin origin"],
)
def test_a_relay_frame_claude_received_is_a_delivery(message_id: str) -> None:
    session = make_session(RELAY_ID, harness="claude", transcript_path=RELAY)

    assert ClaudeTranscripts().contains(session, prefix(message_id), since=0) is True


@pytest.mark.parametrize(
    "message_id",
    [
        "bh_aaaaaaaaaaaa",
        "bh_bbbbbbbbbbbb",
        "bh_cccccccccccc",
        "bh_dddddddddddd",
        "bh_ffffffffffff",
        "bh_333333333333",
    ],
    ids=[
        "typed by the user",
        "assistant text",
        "tool output",
        "only queued while a dialog was open",
        "quoted inside a peer message",
        "a prompt the user typed that starts with the envelope",
    ],
)
def test_the_prefix_anywhere_else_is_not_a_delivery(message_id: str) -> None:
    session = make_session(RELAY_ID, harness="claude", transcript_path=RELAY)

    assert ClaudeTranscripts().contains(session, prefix(message_id), since=0) is False


def test_a_delivery_before_the_offset_does_not_count() -> None:
    session = make_session(RELAY_ID, harness="claude", transcript_path=RELAY)
    reader = ClaudeTranscripts()
    offset = reader.size(session)
    assert offset is not None

    assert reader.contains(session, prefix("bh_eeeeeeeeeeee"), since=offset) is False


def test_another_sessions_transcript_never_confirms() -> None:
    session = make_session("someone-else", harness="claude", transcript_path=None)

    assert (
        ClaudeTranscripts().contains(session, prefix("bh_eeeeeeeeeeee"), since=0, path=RELAY)
        is False
    )


def test_the_relay_reported_transcript_is_used_when_hls_names_none(tmp_path: Path) -> None:
    session = make_session(RELAY_ID, harness="claude", transcript_path=tmp_path / "gone.jsonl")
    reader = ClaudeTranscripts(reported=lambda s: RELAY)

    assert reader.path(session) == RELAY
    assert reader.contains(session, prefix("bh_eeeeeeeeeeee"), since=0) is True


def test_a_transcript_claude_has_not_written_yet_starts_at_zero(tmp_path: Path) -> None:
    later = tmp_path / "later.jsonl"
    session = make_session(RELAY_ID, harness="claude", transcript_path=later)
    reader = ClaudeTranscripts()

    offset = reader.size(session)
    later.write_bytes(RELAY.read_bytes())

    assert offset == 0
    assert reader.contains(session, prefix("bh_eeeeeeeeeeee"), since=0) is True


@pytest.mark.parametrize(
    ("fixture", "session_id", "text"),
    [
        ("peer-accepted.jsonl", "14a10152-38b1-4ebf-8f41-0cff857fdc82", "ACK-REQUEST e8fede25"),
        ("channel.jsonl", "5cb1aebf-035a-4271-806a-7fb8c6657ff2", "<channel source="),
        ("peer.jsonl", "11111111-2222-4333-8444-555555555555", "<cross-session-message"),
    ],
    ids=["peer frame from the research run", "channel origin", "message from another session"],
)
def test_the_research_transcripts_read_their_received_entries(
    fixture: str, session_id: str, text: str
) -> None:
    session = make_session(session_id, harness="claude", transcript_path=FIXTURES / fixture)

    assert ClaudeTranscripts().contains(session, text, since=0) is True


def test_a_message_claude_only_held_is_not_a_delivery() -> None:
    session = make_session(
        "6c0c630f-c2c9-4e95-a442-8e364939155e",
        harness="claude",
        transcript_path=FIXTURES / "held.jsonl",
    )

    assert ClaudeTranscripts().contains(session, "ACK-REQUEST 4ebf4109", since=0) is False


def first_prompt_transcript(tmp_path: Path, session: str, origin: object) -> Path:
    path = tmp_path / "first.jsonl"
    prompt: dict[str, object] = {
        "type": "user",
        "sessionId": session,
        "message": {"role": "user", "content": "[bellhop bh_9 from x] hi"},
    }
    if origin is not None:
        prompt["origin"] = origin
    reply = {"type": "assistant", "sessionId": session, "message": {"role": "assistant"}}
    path.write_text(json.dumps(prompt) + "\n" + json.dumps(reply) + "\n")
    return path


@pytest.mark.parametrize("origin", [None, {"kind": "human"}])
def test_a_start_prompt_is_found_only_when_it_was_passed_as_one(
    tmp_path: Path, origin: object
) -> None:
    session = make_session(RELAY_ID, harness="claude", transcript_path=None)
    path = first_prompt_transcript(tmp_path, RELAY_ID, origin)
    reader = ClaudeTranscripts()

    found = [
        reader.contains(session, "[bellhop bh_9 from", since=0, path=path, prompted=prompted)
        for prompted in (True, False)
    ]

    assert found == [True, False]
