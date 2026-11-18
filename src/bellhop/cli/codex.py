import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from bellhop.cli.presentation import print_diagnostic
from bellhop.codes import FAILURE_EXIT
from bellhop.codex_session import Plan, run
from bellhop.config import load_config
from bellhop.errors import BellhopError
from bellhop.paths import state_dir
from bellhop.routes.unix import endpoint_path
from bellhop.runtime import resolve_runtime_dir
from bellhop.trace import tracer, working_directory

PROGRAM = "codex"
INTERACTIVE = frozenset({"resume", "fork"})
SUBCOMMANDS = frozenset(
    {
        "agents",
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "plugin",
        "app-server",
        "remote-control",
        "app",
        "completion",
        "update",
        "doctor",
        "sandbox",
        "debug",
        "apply",
        "a",
        "queue",
        "archive",
        "delete",
        "migrate-rollouts",
        "migrate-nfs-db",
        "unarchive",
        "cloud",
        "exec-server",
        "features",
        "help",
    }
)
SERVER_OPTIONS = frozenset({"-c", "--config", "--enable", "--disable"})
VALUE_OPTIONS = SERVER_OPTIONS | {
    "--remote-auth-token-env",
    "-i",
    "--image",
    "-m",
    "--model",
    "--local-provider",
    "-s",
    "--sandbox",
    "-C",
    "--cd",
    "--add-dir",
    "-a",
    "--ask-for-approval",
}
MANY_VALUES = frozenset({"-i", "--image"})
OWN_SERVER = frozenset(
    {"-h", "--help", "-V", "--version", "--remote", "--no-daemon", "--oss", "-p", "--profile"}
)
SHORT = 2


def option_name(argument: str) -> str:
    return argument.split("=", 1)[0] if argument.startswith("--") else argument[:SHORT]


def holds_value(argument: str) -> bool:
    return "=" in argument if argument.startswith("--") else len(argument) > SHORT


def is_option(argument: str) -> bool:
    return argument.startswith("-") and argument != "-"


def scan(arguments: Sequence[str]) -> tuple[list[str], str | None, bool]:
    server: list[str] = []
    positionals: list[str] = []
    own = False
    index = 0
    while index < len(arguments) and arguments[index] != "--":
        argument = arguments[index]
        if not is_option(argument):
            positionals.append(argument)
            index += 1
            continue
        name = option_name(argument)
        own = own or name in OWN_SERVER
        takes_next = name in VALUE_OPTIONS and not holds_value(argument)
        if name in SERVER_OPTIONS:
            server += arguments[index : index + 1 + takes_next]
        index += 1 + takes_next
        while name in MANY_VALUES and index < len(arguments) and not is_option(arguments[index]):
            index += 1
    return server, positionals[0] if positionals else None, own


def wrapped(arguments: Sequence[str]) -> list[str] | None:
    server, positional, own = scan(arguments)
    if own or (positional in SUBCOMMANDS and positional not in INTERACTIVE):
        return None
    return server


def plan(arguments: Sequence[str], overrides: Sequence[str], socket: Path) -> Plan:
    listen = f"unix://{socket}"
    return Plan(
        socket,
        [PROGRAM, "app-server", *overrides, "--listen", listen],
        [PROGRAM, "--remote", listen, *arguments],
    )


def plain(arguments: Sequence[str]) -> NoReturn:
    sys.stdout.flush()
    try:
        os.execvp(PROGRAM, [PROGRAM, *arguments])  # noqa: S606
    except OSError as error:
        print_diagnostic(f"cannot run {PROGRAM}: {error.strerror or error}")
        sys.exit(FAILURE_EXIT)


def main(arguments: Sequence[str], env: Mapping[str, str], caller_umask: int) -> int:
    os.umask(caller_umask)
    overrides = wrapped(arguments)
    if overrides is None:
        plain(arguments)
    trace = tracer(env, "codex")
    try:
        runtime = resolve_runtime_dir(env, load_config(env))
        socket = endpoint_path(runtime, "codex", os.getpid())
        log_file = state_dir(env) / f"codex-session-{os.getpid()}.log"
    except Exception as error:  # noqa: BLE001
        problem = (
            str(error) if isinstance(error, BellhopError) else f"{type(error).__name__}: {error}"
        )
        print_diagnostic(f"{problem}; starting codex without an app server bellhop can reach")
        trace("failed", error=problem, fallback=True)
        plain(arguments)
    started = plan(arguments, overrides, socket)
    trace("start", socket=str(socket), server=started.server, cwd=working_directory())
    return run(started, log_file, fallback=[PROGRAM, *arguments], trace=trace, keep_log=False)
