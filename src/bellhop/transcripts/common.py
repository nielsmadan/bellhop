import json
from collections.abc import Sequence
from pathlib import Path

TEXT_BLOCKS = ("text", "input_text", "output_text")


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
