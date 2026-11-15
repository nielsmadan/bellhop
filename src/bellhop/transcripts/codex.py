import glob
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from bellhop.models import Session
from bellhop.transcripts.common import Cursors, content_text, first_record

UUID_DIGITS, VERSION_AT, UUID7_VERSION = 32, 12, "7"


def created_days(thread_id: str, zone: tzinfo | None = None) -> list[Path]:
    digits = thread_id.replace("-", "")
    if len(digits) != UUID_DIGITS or digits[VERSION_AT] != UUID7_VERSION:
        return []
    try:
        created = datetime.fromtimestamp(int(digits[:12], 16) / 1000, zone)
    except (ValueError, OverflowError, OSError):
        return []
    days = (created + timedelta(days=step) for step in (0, -1, 1))
    return [Path(f"{day:%Y}") / f"{day:%m}" / f"{day:%d}" for day in days]


def rollout(sessions: Path, thread_id: str, zone: tzinfo | None = None) -> Path | None:
    if not thread_id or "/" in thread_id:
        return None
    pattern = f"rollout-*-{glob.escape(thread_id)}.jsonl"
    for day in created_days(thread_id, zone):
        dated = sorted(path for path in (sessions / day).glob(pattern) if path.is_file())
        if dated:
            return dated[-1]
    return None


def belongs(path: Path, session: Session) -> bool:
    header = first_record(path)
    if header is None or header.get("type") != "session_meta":
        return True
    payload = header.get("payload")
    return isinstance(payload, dict) and payload.get("id") == session.session_id


def delivered(record: dict[str, object], prefix: str) -> bool:
    payload = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(payload, dict):
        return False
    if payload.get("type") != "message" or payload.get("role") != "user":
        return False
    return content_text(payload.get("content")).lstrip().startswith(prefix)


class CodexTranscripts:
    def __init__(self, codex_home: Path | None) -> None:
        self.codex_home = codex_home
        self._cursors = Cursors()
        self._found: dict[str, Path] = {}

    def path(self, session: Session) -> Path | None:
        if session.transcript_path is not None and session.transcript_path.is_file():
            return session.transcript_path
        known = self._found.pop(session.session_id, None)
        if known is not None and known.is_file():
            self._found[session.session_id] = known
            return known
        sessions = self.codex_home / "sessions" if self.codex_home is not None else None
        found = rollout(sessions, session.session_id) if sessions and sessions.is_dir() else None
        if found is not None:
            self._found[session.session_id] = found
        return found

    def size(self, session: Session) -> int | None:
        path = self.path(session)
        try:
            return path.stat().st_size if path is not None else None
        except OSError:
            return None

    def contains(
        self,
        session: Session,
        prefix: str,
        *,
        since: int,
        path: Path | None = None,
        prompted: bool = False,
    ) -> bool:
        path = path or self.path(session)
        if path is None or not path.is_file() or not belongs(path, session):
            return False
        return self._cursors.scan(path, prefix, since, delivered)
