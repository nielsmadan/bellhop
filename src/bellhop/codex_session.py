import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from bellhop.codes import FAILURE_EXIT, USAGE_EXIT
from bellhop.paths import state_dir

SOCKET_WAIT_SECONDS = 15.0
STOP_SECONDS = 5.0
POLL_SECONDS = 0.1


@dataclass(frozen=True)
class Plan:
    socket: Path
    server: list[str]
    tui: list[str]


def session_argv(socket: Path, server: Sequence[str], tui: Sequence[str]) -> list[str]:
    return ["_codex_session", str(socket), str(len(server)), *server, *tui]


def parse(argv: Sequence[str]) -> Plan | None:
    if len(argv) < 3 or not argv[1].isdigit():  # noqa: PLR2004
        return None
    count = int(argv[1])
    rest = list(argv[2:])
    if count < 1 or len(rest) <= count:
        return None
    return Plan(Path(argv[0]), rest[:count], rest[count:])


def daemon_target(link: Path, uid: int) -> Path | None:
    try:
        target = Path(os.readlink(link))
    except OSError:
        return None
    expected = hashlib.sha256(str(link.parent.resolve() / link.name).encode()).hexdigest()
    if target.parent.name != f"codex-daemon-{uid}" or target.name != expected:
        return None
    return target


def remove_socket(link: Path, uid: int) -> None:
    target = daemon_target(link, uid)
    if target is not None:
        for path in (target, target.with_name(f"{target.name}.lock")):
            path.unlink(missing_ok=True)
    if link.is_symlink() or link.is_socket():
        link.unlink(missing_ok=True)


def ignore(signum: int, frame: FrameType | None) -> None:
    return None


def signal_group(server: subprocess.Popen[bytes], signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(server.pid, signum)


def stop(server: subprocess.Popen[bytes]) -> None:
    signal_group(server, signal.SIGTERM)
    try:
        server.wait(STOP_SECONDS)
    except subprocess.TimeoutExpired:
        signal_group(server, signal.SIGKILL)
        server.wait(STOP_SECONDS)
    signal_group(server, signal.SIGKILL)


def wait_for_socket(
    socket: Path,
    server: subprocess.Popen[bytes],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = clock() + SOCKET_WAIT_SECONDS
    while clock() < deadline and server.poll() is None:
        if socket.exists():
            return True
        sleep(POLL_SECONDS)
    return False


def log_path(env: Mapping[str, str], socket: Path) -> Path:
    return state_dir(env) / f"{socket.stem.rsplit('-', 1)[0]}.log"


def server_log(path: Path) -> int:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    return os.open(path, flags, 0o600)


def note(text: str) -> None:
    print(f"bellhop: codex session: {text}", file=sys.stderr, flush=True)


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    plan = parse(argv)
    if plan is None:
        note("bad arguments")
        return USAGE_EXIT
    log_file = log_path(env, plan.socket)
    log = server_log(log_file)
    try:
        server = subprocess.Popen(
            plan.server, stdin=subprocess.DEVNULL, stdout=log, stderr=log, process_group=0
        )
    finally:
        os.close(log)

    def finish(signum: int, frame: FrameType | None) -> None:
        stop(server)
        remove_socket(plan.socket, os.getuid())
        sys.exit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, finish)
    signal.signal(signal.SIGINT, ignore)
    try:
        if not wait_for_socket(plan.socket, server):
            note(f"the app server did not open {plan.socket}; see {log_file}")
            return FAILURE_EXIT
        return subprocess.call(plan.tui)
    finally:
        stop(server)
        remove_socket(plan.socket, os.getuid())
