import os
import sys
import time
from collections.abc import Mapping, Sequence

from bellhop.cli.dispatch import run
from bellhop.cli.parsing import bellhop_version, json_requested, parse
from bellhop.cli.presentation import print_diagnostic, print_json, silence_stdout
from bellhop.codes import INTERNAL_ERROR, INTERRUPTED
from bellhop.errors import BellhopError
from bellhop.identity import SESSION_VARS
from bellhop.jsonout import error_payload
from bellhop.trace import Emit, tracer, working_directory

UNTRACED = frozenset({"log"})


def _report(error: BellhopError, argv: Sequence[str]) -> int:
    if json_requested(argv):
        print_json(error_payload(error))
    else:
        print_diagnostic(str(error))
        for candidate in error.candidates or []:
            print_diagnostic(f"  {candidate}")
    return error.exit_code


def _run(argv: Sequence[str], caller_umask: int, trace: Emit) -> int:
    args = parse(argv)
    if args.version:
        print(f"bellhop {bellhop_version()}")
        return 0
    return run(args, os.environ, caller_umask, trace)


def _outcome(argv: Sequence[str], caller_umask: int, trace: Emit) -> BellhopError | int:
    try:
        return _run(argv, caller_umask, trace)
    except BellhopError as error:
        return error
    except KeyboardInterrupt:
        return BellhopError("interrupted", code=INTERRUPTED)
    except BrokenPipeError:
        raise
    except Exception as error:  # noqa: BLE001
        message = f"internal error: {type(error).__name__}: {error}"
        return BellhopError(message, code=INTERNAL_ERROR)


def caller_session(env: Mapping[str, str]) -> str | None:
    return next((env[name] for name in SESSION_VARS if env.get(name)), None)


def command_fields(
    argv: Sequence[str], env: Mapping[str, str], outcome: BellhopError | int, seconds: float
) -> dict[str, object]:
    failed = isinstance(outcome, BellhopError)
    return {
        "argv": list(argv),
        "cwd": working_directory(),
        "caller": caller_session(env),
        "exit": outcome.exit_code if isinstance(outcome, BellhopError) else outcome,
        "code": outcome.code if isinstance(outcome, BellhopError) else None,
        "error": str(outcome) if failed else None,
        "ms": round(seconds * 1000),
    }


def _main(argv: Sequence[str], caller_umask: int) -> int:
    trace = tracer(os.environ, "cli")
    started = time.monotonic()
    outcome = _outcome(argv, caller_umask, trace)
    if argv[:1] and argv[0] not in UNTRACED:
        trace("command", **command_fields(argv, os.environ, outcome, time.monotonic() - started))
    return _report(outcome, argv) if isinstance(outcome, BellhopError) else outcome


def run_command(arguments: Sequence[str], caller_umask: int) -> int:
    try:
        exit_code = _main(arguments, caller_umask)
        sys.stdout.flush()
    except BrokenPipeError:
        silence_stdout()
        return 1
    return exit_code
