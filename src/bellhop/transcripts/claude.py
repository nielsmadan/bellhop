import re
from itertools import islice
from pathlib import Path

from bellhop.models import Session
from bellhop.transcripts.common import (
    Cursors,
    Reported,
    content_text,
    nothing_reported,
    parse_records,
)

RECEIVED_ORIGINS = frozenset({"peer", "channel", "plugin"})
UNSAFE_PATH = re.compile(r"[^A-Za-z0-9]")
PEER_WRAPPER = "Another Claude session sent a message:\n"
HEADER_LINES = 50


def transcript_of(claude_dir: Path, cwd: str, session: str) -> Path:
    return claude_dir / "projects" / UNSAFE_PATH.sub("-", cwd) / f"{session}.jsonl"


def origin_kind(value: object) -> object:
    return value.get("kind") if isinstance(value, dict) else None


def received_texts(record: dict[str, object]) -> list[str]:
    message = record.get("message")
    if record.get("type") == "user" and origin_kind(record.get("origin")) in RECEIVED_ORIGINS:
        return [content_text(message.get("content"))] if isinstance(message, dict) else []
    attachment = record.get("attachment")
    if (
        record.get("type") == "attachment"
        and isinstance(attachment, dict)
        and attachment.get("type") == "queued_command"
        and origin_kind(attachment.get("origin")) in RECEIVED_ORIGINS
    ):
        return [str(attachment.get("prompt") or "")]
    return []


def delivered(record: dict[str, object], prefix: str) -> bool:
    return any(
        text.removeprefix(PEER_WRAPPER).lstrip().startswith(prefix)
        for text in received_texts(record)
    )


def owner_of(path: Path) -> object:
    with path.open("rb") as handle:
        for line in islice(handle, HEADER_LINES):
            owner = next((r.get("sessionId") for r in parse_records([line])), None)
            if owner:
                return owner
    return None


def belongs(path: Path, session: Session) -> bool:
    try:
        owner = owner_of(path)
    except OSError:
        return False
    return owner is None or owner == session.session_id


class ClaudeTranscripts:
    def __init__(self, reported: Reported = nothing_reported) -> None:
        self.reported = reported
        self._cursors = Cursors()
        self._found: dict[str, Path] = {}

    def path(self, session: Session) -> Path | None:
        if session.transcript_path is not None and session.transcript_path.is_file():
            return session.transcript_path
        known = self._found.pop(session.session_id, None)
        if known is not None and known.is_file():
            self._found[session.session_id] = known
            return known
        found = self.reported(session) or session.transcript_path
        if found is not None and found.is_file():
            self._found[session.session_id] = found
        return found

    def size(self, session: Session) -> int | None:
        path = self.path(session)
        if path is None:
            return None
        try:
            return path.stat().st_size if path.is_file() else 0
        except OSError:
            return None

    def contains(
        self, session: Session, prefix: str, *, since: int, path: Path | None = None
    ) -> bool:
        path = path or self.path(session)
        if path is None or not path.is_file() or not belongs(path, session):
            return False
        return self._cursors.scan(path, prefix, since, delivered)
