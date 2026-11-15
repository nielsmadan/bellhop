import argparse
from collections.abc import Sequence
from importlib.metadata import version
from typing import NoReturn

from bellhop.codes import USAGE
from bellhop.errors import BellhopError

MISSING_COMMAND = "missing command; see 'bellhop --help'"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise BellhopError(message, code=USAGE)


def bellhop_version() -> str:
    return version("bellhop")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="bellhop",
        description="Deliver messages into coding-agent sessions.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)
    sessions = commands.add_parser(
        "sessions", help="list live Claude, Codex and Pi sessions", allow_abbrev=False
    )
    sessions.add_argument("--json", action="store_true", help="print one JSON object")
    return parser


def parse(argv: Sequence[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def json_requested(argv: Sequence[str]) -> bool:
    return "--json" in argv
