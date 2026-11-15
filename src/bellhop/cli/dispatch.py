import argparse
from collections.abc import Mapping

from bellhop.cli.parsing import MISSING_COMMAND
from bellhop.codes import USAGE
from bellhop.errors import BellhopError


def dispatch(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    raise BellhopError(MISSING_COMMAND, code=USAGE)
