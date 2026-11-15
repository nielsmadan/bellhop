import re
import shlex
from pathlib import Path

import pytest

from bellhop.cli.parsing import parse

USER_DOCS = Path(__file__).resolve().parents[1] / "docs" / "user"
SHELL_BLOCK = re.compile(r"```sh\n(.*?)```", re.DOTALL)


def documented_commands() -> list[tuple[str, str]]:
    return [
        (path.name, line.split("  #")[0].strip())
        for path in sorted(USER_DOCS.glob("*.md"))
        for block in SHELL_BLOCK.findall(path.read_text(encoding="utf-8"))
        for line in block.splitlines()
        if line.startswith("bellhop ")
    ]


def test_the_user_docs_show_bellhop_commands() -> None:
    assert {name for name, _ in documented_commands()} >= {"getting-started.md", "sending.md"}


@pytest.mark.parametrize(("doc", "command"), documented_commands())
def test_every_documented_command_parses(doc: str, command: str) -> None:
    args = parse(shlex.split(command)[1:])

    assert args.command is not None or args.version
