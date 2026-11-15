import argparse
from collections.abc import Callable, Mapping

from bellhop.cli.parsing import MISSING_COMMAND
from bellhop.cli.presentation import print_diagnostic, print_json, print_lines, session_lines
from bellhop.codes import USAGE
from bellhop.errors import BellhopError
from bellhop.hls import list_sessions
from bellhop.jsonout import ok_payload, session_object


def _sessions(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    result = list_sessions(env)
    if args.json:
        sessions = [session_object(session) for session in result.sessions]
        print_json(ok_payload(result.warnings, {"sessions": sessions}))
        return 0
    for warning in result.warnings:
        print_diagnostic(f"warning: {warning}")
    print_lines(session_lines(result.sessions))
    return 0


COMMANDS: Mapping[str, Callable[[argparse.Namespace, Mapping[str, str]], int]] = {
    "sessions": _sessions,
}


def run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    command = COMMANDS.get(args.command or "")
    if command is None:
        raise BellhopError(MISSING_COMMAND, code=USAGE)
    return command(args, env)
