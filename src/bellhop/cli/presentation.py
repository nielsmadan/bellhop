import os
import sys
from collections.abc import Mapping, Sequence

from bellhop.jsonout import dumps

_CONTROL_ESCAPES = {
    code: f"\\x{code:02x}" for code in (*range(0x20), *range(0x7F, 0xA0)) if code != ord("\t")
} | {ord("\n"): "\\n", ord("\r"): "\\r"}


def escape_controls(text: str) -> str:
    return text.translate(_CONTROL_ESCAPES)


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
