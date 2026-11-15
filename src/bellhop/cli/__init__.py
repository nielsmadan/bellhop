import os
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    caller_umask = os.umask(0o077)
    if arguments == ["_relay"]:
        from bellhop.relay import main as relay_main

        return relay_main(os.environ)
    if arguments == ["_drain"]:
        from bellhop.drain import main as drain_main

        return drain_main(os.environ)
    if arguments[:1] == ["_codex_session"]:
        from bellhop.codex_session import main as codex_session_main

        return codex_session_main(arguments[1:], os.environ, caller_umask)
    from bellhop.cli.command import run_command

    return run_command(arguments, caller_umask)
