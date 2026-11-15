import os
import sys
from collections.abc import Sequence

from bellhop.cli.dispatch import run
from bellhop.cli.parsing import bellhop_version, json_requested, parse
from bellhop.cli.presentation import print_diagnostic, print_json, silence_stdout
from bellhop.codes import INTERNAL_ERROR, INTERRUPTED
from bellhop.errors import BellhopError
from bellhop.jsonout import error_payload


def _report(error: BellhopError, argv: Sequence[str]) -> int:
    if json_requested(argv):
        print_json(error_payload(error))
    else:
        print_diagnostic(str(error))
        for candidate in error.candidates or []:
            print_diagnostic(f"  {candidate}")
    return error.exit_code


def _main(argv: Sequence[str], caller_umask: int) -> int:
    try:
        args = parse(argv)
        if args.version:
            print(f"bellhop {bellhop_version()}")
            return 0
        return run(args, os.environ, caller_umask)
    except BellhopError as error:
        return _report(error, argv)
    except KeyboardInterrupt:
        return _report(BellhopError("interrupted", code=INTERRUPTED), argv)
    except BrokenPipeError:
        raise
    except Exception as error:  # noqa: BLE001
        message = f"internal error: {type(error).__name__}: {error}"
        return _report(BellhopError(message, code=INTERNAL_ERROR), argv)


def run_command(arguments: Sequence[str], caller_umask: int) -> int:
    try:
        exit_code = _main(arguments, caller_umask)
        sys.stdout.flush()
    except BrokenPipeError:
        silence_stdout()
        return 1
    return exit_code
