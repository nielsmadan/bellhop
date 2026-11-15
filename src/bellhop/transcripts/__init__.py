from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from bellhop.models import Binding, Session
from bellhop.transcripts.claude import ClaudeTranscripts
from bellhop.transcripts.codex import CodexTranscripts
from bellhop.transcripts.common import Reported, nothing_reported
from bellhop.transcripts.pi import PiTranscripts


class TranscriptReader(Protocol):
    def path(self, session: Session) -> Path | None: ...

    def size(self, session: Session) -> int | None: ...

    def contains(
        self, session: Session, prefix: str, *, since: int, path: Path | None = None
    ) -> bool: ...


type ReaderFinder = Callable[[Session, Binding | None], TranscriptReader | None]


@dataclass(frozen=True)
class ReaderSetup:
    pi_agent_dir: Path | None
    fake: bool = False
    reported: Reported = nothing_reported
    codex_home: Path | None = None


class PlainTranscripts:
    def path(self, session: Session) -> Path | None:
        return session.transcript_path

    def size(self, session: Session) -> int | None:
        path = session.transcript_path
        try:
            return path.stat().st_size if path is not None else None
        except OSError:
            return None

    def contains(
        self, session: Session, prefix: str, *, since: int, path: Path | None = None
    ) -> bool:
        path = path or session.transcript_path
        if path is None:
            return False
        try:
            with path.open("rb") as handle:
                handle.seek(since)
                return prefix.encode() in handle.read()
        except OSError:
            return False


def reader_finder(setup: ReaderSetup) -> ReaderFinder:
    if setup.fake:
        plain = PlainTranscripts()
        return lambda session, binding: plain
    readers: Mapping[str, TranscriptReader] = {
        "pi": PiTranscripts(setup.pi_agent_dir, setup.reported),
        "codex": CodexTranscripts(setup.codex_home),
        "claude": ClaudeTranscripts(setup.reported),
    }
    return lambda session, binding: readers.get(session.harness)
