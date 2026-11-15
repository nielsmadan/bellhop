import json
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path

from bellhop.models import Session

TEXT_BLOCKS = ("text", "input_text", "output_text")
CURSOR_LIMIT = 1024

type Reported = Callable[[Session], Path | None]


def nothing_reported(session: Session) -> Path | None:
    return None


def complete_lines(path: Path, start: int) -> tuple[list[bytes], int]:
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    end = data.rfind(b"\n") + 1
    return data[:end].splitlines(), start + end


def parse_records(lines: Sequence[bytes]) -> list[dict[str, object]]:
    parsed = [_record(line) for line in lines]
    return [record for record in parsed if record is not None]


def _record(line: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") in TEXT_BLOCKS
        )
    return ""


def first_record(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as handle:
            return _record(handle.readline())
    except OSError:
        return None


class Cursors:
    def __init__(self) -> None:
        self._at: OrderedDict[tuple[Path, str], int] = OrderedDict()

    def scan(
        self,
        path: Path,
        prefix: str,
        since: int,
        delivered: Callable[[dict[str, object], str], bool],
    ) -> bool:
        key = (path, prefix)
        start = max(since, self._at.pop(key, since))
        try:
            if path.stat().st_size < start:
                start = since
            lines, end = complete_lines(path, start)
        except OSError:
            return False
        wanted = prefix.encode()
        candidates = [line for line in lines if wanted in line]
        if any(delivered(record, prefix) for record in parse_records(candidates)):
            return True
        self._at[key] = end
        while len(self._at) > CURSOR_LIMIT:
            self._at.popitem(last=False)
        return False
