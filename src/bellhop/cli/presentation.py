import os
import sys
from collections.abc import Mapping, Sequence

from bellhop.jsonout import dumps
from bellhop.models import Session

SHORT_ID = 8

_CONTROL_ESCAPES = {
    code: f"\\x{code:02x}" for code in (*range(0x20), *range(0x7F, 0xA0)) if code != ord("\t")
} | {ord("\n"): "\\n", ord("\r"): "\\r"}


def escape_controls(text: str) -> str:
    return text.translate(_CONTROL_ESCAPES)


def _row(session: Session) -> tuple[str, str, str, str]:
    title = escape_controls(session.title) if session.title else "-"
    return (session.session_id[:SHORT_ID], session.harness, session.phase, title)


def session_lines(sessions: Sequence[Session]) -> list[str]:
    if not sessions:
        return ["no live sessions"]
    rows = [("ID", "HARNESS", "PHASE", "TITLE"), *(_row(session) for session in sessions)]
    padded = range(len(rows[0]) - 1)
    widths = [max(len(row[column]) for row in rows) for column in padded]
    return [
        "  ".join([*(row[column].ljust(widths[column]) for column in padded), row[-1]])
        for row in rows
    ]


def print_json(payload: Mapping[str, object]) -> None:
    print(dumps(payload))


def print_lines(lines: Sequence[str]) -> None:
    for line in lines:
        print(line)


def print_diagnostic(message: str) -> None:
    print(f"bellhop: {escape_controls(message)}", file=sys.stderr)


def silence_stdout() -> None:
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        pass
    finally:
        os.close(null)
