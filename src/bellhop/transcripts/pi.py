from collections.abc import Callable
from pathlib import Path

from bellhop.models import Session
from bellhop.transcripts.common import Cursors, content_text, first_record


def session_dir(agent_dir: Path, cwd: Path) -> Path:
    text = str(cwd)
    safe = (text.removeprefix("/")).replace("/", "-").replace(":", "-")
    return agent_dir / "sessions" / f"--{safe}--"


def find_session_file(directory: Path, session_id: str) -> Path | None:
    headers = [(first_record(path), path) for path in directory.glob("*.jsonl") if path.is_file()]
    matches = [
        (str(header.get("timestamp", "")), path)
        for header, path in headers
        if header is not None and header.get("type") == "session" and header.get("id") == session_id
    ]
    return max(matches)[1] if matches else None


def belongs(path: Path, session: Session) -> bool:
    header = first_record(path)
    if header is None or header.get("type") != "session":
        return True
    return header.get("id") == session.session_id


def received_texts(record: dict[str, object]) -> list[str]:
    if record.get("type") == "custom_message":
        return [content_text(record.get("content"))]
    message = record.get("message")
    if (
        record.get("type") == "message"
        and isinstance(message, dict)
        and message.get("role") == "user"
    ):
        return [content_text(message.get("content"))]
    return []


def delivered(record: dict[str, object], prefix: str) -> bool:
    return any(text.lstrip().startswith(prefix) for text in received_texts(record))


type Reported = Callable[[Session], Path | None]


def nothing_reported(session: Session) -> Path | None:
    return None


class PiTranscripts:
    def __init__(self, agent_dir: Path | None, reported: Reported = nothing_reported) -> None:
        self.agent_dir = agent_dir
        self.reported = reported
        self._cursors = Cursors()
        self._found: dict[str, Path] = {}

    def directory(self, session: Session) -> Path | None:
        if self.agent_dir is None or session.cwd is None:
            return None
        return session_dir(self.agent_dir, session.cwd)

    def path(self, session: Session) -> Path | None:
        if session.transcript_path is not None and session.transcript_path.is_file():
            return session.transcript_path
        known = self._found.pop(session.session_id, None)
        if known is not None and known.is_file():
            self._found[session.session_id] = known
            return known
        directory = self.directory(session)
        found = self.reported(session) or (
            find_session_file(directory, session.session_id) if directory else None
        )
        if found is not None and found.is_file():
            self._found[session.session_id] = found
        return found

    def size(self, session: Session) -> int | None:
        path = self.path(session)
        if path is None:
            directory = self.directory(session)
            return 0 if directory is not None and directory.is_dir() else None
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
