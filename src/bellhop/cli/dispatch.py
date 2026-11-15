import argparse
from collections.abc import Callable, Mapping

from bellhop.cli.context import Context
from bellhop.cli.doctor import doctor
from bellhop.cli.info import agents, bind, sessions, whoami
from bellhop.cli.integration import export, show
from bellhop.cli.launching import launch
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
    "launch": launch,
    "doctor": doctor,
    "integration show": show,
    "integration export": export,
}


OWN_DRAINER_CHECK = frozenset({"doctor"})


def run(args: argparse.Namespace, env: Mapping[str, str], caller_umask: int) -> int:
    name = f"{args.command} {args.action}" if getattr(args, "action", None) else args.command
    command = COMMANDS.get(name or "")
    if command is None:
        raise BellhopError(MISSING_COMMAND, code=USAGE)
    ctx = Context(env, caller_umask=caller_umask)
    try:
        if args.command not in OWN_DRAINER_CHECK:
            ctx.kick()
        return command(args, ctx)
    finally:
        ctx.close()
