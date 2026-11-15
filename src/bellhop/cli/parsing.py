import argparse
import re
from collections.abc import Sequence
from importlib.metadata import version
from typing import NoReturn

from bellhop.codes import USAGE
from bellhop.errors import BellhopError
from bellhop.models import HARNESSES, URGENCIES

MISSING_COMMAND = "missing command; see 'bellhop --help'"
HARNESS_NAMES = tuple(HARNESSES)
INTEGRATIONS = (*HARNESS_NAMES, "skill")
EXPORTS = ("claude", "pi", "skill")


DASHED = re.compile(r"unrecognized arguments: -[^-]")


def _key(value: str) -> str:
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        hint = "; put -- before a message that starts with '-'" if DASHED.search(message) else ""
        raise BellhopError(f"{message}{hint}", code=USAGE)


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
    send = _command(commands, "send", "deliver a message to an agent or a session")
    send.add_argument("target", help="agent name, session id, id prefix (4+) or title")
    send.add_argument("text", nargs="?", help="the message; read from stdin when omitted")
    send.add_argument(
        "--urgency",
        choices=tuple(URGENCIES),
        default="idle",
        help="now: interrupt; idle: wait until the session is idle (default); hold: keep for inbox",
    )
    send.add_argument(
        "--wake", action="store_true", help="start the agent when no session of it is running"
    )
    send.add_argument(
        "--or-hold", action="store_true", help="store as hold when the target has no route"
    )
    send.add_argument("--from", dest="sender", metavar="NAME", help="the sender's name")
    send.add_argument("--key", type=_key, help="idempotency key; a repeated key pushes nothing")
    send.add_argument(
        "--confirm", type=_seconds, metavar="SECONDS", help="wait for the receiver's transcript"
    )
    status = _command(commands, "status", "show stored messages")
    status.add_argument("ids", nargs="+", metavar="MESSAGE_ID", help="ids that send printed")
    inbox = _command(commands, "inbox", "read hold messages for this session and its agent")
    inbox.add_argument("--target", help="read another target's hold messages")
    inbox.add_argument("--peek", action="store_true", help="leave the messages unread")
    _command(commands, "sessions", "list live Claude, Codex and Pi sessions")
    _command(commands, "whoami", "show the session this command runs in")
    _command(commands, "agents", "list configured and bound agents")
    bind = _command(commands, "bind", "bind this session to an agent name")
    bind.add_argument("agent", help="lowercase letters, digits, '_' and '-'")
    launch = _command(commands, "launch", "start a configured agent unless it is running")
    launch.add_argument("agent", help="a configured agent name")
    launch.add_argument("--prompt", metavar="TEXT", help="the first message for the agent")
    launch.add_argument("--cause", metavar="TEXT", help="why it was started; kept on the binding")
    launch.add_argument("--from", dest="sender", metavar="NAME", help="the prompt's sender")
    launch.add_argument("--key", type=_key, help="idempotency key for the prompt")
    launch.add_argument(
        "--no-wait", action="store_true", help="return once it is started, before it is ready"
    )
    integration = commands.add_parser(
        "integration",
        help="show or export a harness integration",
        description="show or export a harness integration",
        allow_abbrev=False,
    )
    actions = integration.add_subparsers(
        dest="action", metavar="ACTION", parser_class=_Parser, required=True
    )
    show = _command(actions, "show", "print what to install and where")
    show.add_argument("harness", choices=INTEGRATIONS, help="what to show: %(choices)s")
    export = _command(actions, "export", "copy the integration to a directory you control")
    export.add_argument("harness", choices=EXPORTS, help="what to copy: %(choices)s")
    export.add_argument("--to", required=True, metavar="DIR", help="the directory to copy into")
    doctor = _command(commands, "doctor", "check hls, directories, store and sockets")
    doctor.add_argument("--prune", action="store_true", help="delete old messages and dead sockets")
    return parser


def _command(
    commands: "argparse._SubParsersAction[_Parser]", name: str, summary: str
) -> argparse.ArgumentParser:
    command = commands.add_parser(name, help=summary, description=summary, allow_abbrev=False)
    command.add_argument("--json", action="store_true", help="print one JSON object")
    return command


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number of seconds") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"'{text}' is negative")
    return value


def parse(argv: Sequence[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def json_requested(argv: Sequence[str]) -> bool:
    options = argv[: argv.index("--")] if "--" in argv else argv
    return "--json" in options
