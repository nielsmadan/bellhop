import argparse
from collections.abc import Callable, Mapping

from bellhop.cli.context import Context
from bellhop.cli.doctor import doctor
from bellhop.cli.info import agents, bind, sessions, whoami
from bellhop.cli.messages import inbox, send, status
from bellhop.cli.parsing import MISSING_COMMAND
from bellhop.codes import USAGE
from bellhop.errors import BellhopError

COMMANDS: Mapping[str, Callable[[argparse.Namespace, Context], int]] = {
    "send": send,
    "status": status,
    "inbox": inbox,
    "sessions": sessions,
    "whoami": whoami,
    "agents": agents,
    "bind": bind,
    "doctor": doctor,
}


def run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    command = COMMANDS.get(args.command or "")
    if command is None:
        raise BellhopError(MISSING_COMMAND, code=USAGE)
    ctx = Context(env)
    try:
        code = command(args, ctx)
        ctx.kick()
        return code
    finally:
        ctx.close()
