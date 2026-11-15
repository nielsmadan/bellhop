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
    return error.exit_code


def _main(argv: Sequence[str]) -> int:
    try:
        args = parse(argv)
        if args.version:
            print(f"bellhop {bellhop_version()}")
            return 0
        return run(args, os.environ)
    except BellhopError as error:
        return _report(error, argv)
    except KeyboardInterrupt:
        return _report(BellhopError("interrupted", code=INTERRUPTED), argv)
    except BrokenPipeError:
        raise
    except Exception as error:  # noqa: BLE001
        message = f"internal error: {type(error).__name__}: {error}"
        return _report(BellhopError(message, code=INTERNAL_ERROR), argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        exit_code = _main(arguments)
        sys.stdout.flush()
    except BrokenPipeError:
        silence_stdout()
        return 1
    return exit_code
