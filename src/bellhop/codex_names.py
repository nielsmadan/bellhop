import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from bellhop.models import Session

INDEX_NAME = "session_index.jsonl"
READ_LIMIT = 8 << 20
HOME_DEPTH = 4

type Names = Callable[[Path], dict[str, str]]


def transcript_home(session: Session) -> Path | None:
    path = session.transcript_path
    if session.harness != "codex" or path is None:
        return None
    parents = path.parents
    if len(parents) > HOME_DEPTH and parents[HOME_DEPTH - 1].name == "sessions":
        return parents[HOME_DEPTH]
    return None


def index_lines(path: Path) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - READ_LIMIT))
            return handle.read(READ_LIMIT).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def entry(line: str) -> tuple[str, str] | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    thread, name = value.get("id"), value.get("thread_name")
    if isinstance(thread, str) and isinstance(name, str) and name.strip():
        return thread, name.strip()
    return None


def thread_names(home: Path) -> dict[str, str]:
    entries = [entry(line) for line in index_lines(home / INDEX_NAME)]
    return dict(found for found in entries if found is not None)


def named(
    sessions: Sequence[Session], default_home: Path | None, names: Names = thread_names
) -> tuple[Session, ...]:
    homes = {
        session.session_id: transcript_home(session) or default_home
        for session in sessions
        if session.harness == "codex" and session.title is None
    }
    known = {home: names(home) for home in set(homes.values()) if home is not None}

    def titled(session: Session) -> Session:
        home = homes.get(session.session_id)
        title = known[home].get(session.session_id) if home is not None else None
        return replace(session, title=title) if title else session

    return tuple(titled(session) for session in sessions)
