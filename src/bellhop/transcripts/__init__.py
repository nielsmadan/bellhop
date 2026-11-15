from collections.abc import Callable, Mapping
from typing import Protocol

from bellhop.models import FAKE_ROUTES_ENV, Session


class TranscriptReader(Protocol):
    def size(self, session: Session) -> int | None: ...

    def contains(self, session: Session, prefix: str, *, since: int) -> bool: ...


type ReaderFinder = Callable[[Session], TranscriptReader | None]


class PlainTranscripts:
    def size(self, session: Session) -> int | None:
        path = session.transcript_path
        try:
            return path.stat().st_size if path is not None else None
        except OSError:
            return None

    def contains(self, session: Session, prefix: str, *, since: int) -> bool:
        path = session.transcript_path
        if path is None:
            return False
        try:
            with path.open("rb") as handle:
                handle.seek(since)
                return prefix.encode() in handle.read()
        except OSError:
            return False


def reader_finder(env: Mapping[str, str]) -> ReaderFinder:
    if env.get(FAKE_ROUTES_ENV):
        reader = PlainTranscripts()
        return lambda session: reader
    return lambda session: None
