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
from bellhop.files import open_private
from bellhop.paths import state_dir
from bellhop.trace import Emit, quiet

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


def exited_unreaped(server: subprocess.Popen[bytes]) -> bool:
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    try:
        return os.waitid(os.P_PID, server.pid, flags) is not None
    except ChildProcessError:
        return True


def stop(server: subprocess.Popen[bytes], clock: Callable[[], float] = time.monotonic) -> None:
    if server.returncode is not None:
        return
    signal_group(server, signal.SIGTERM)
    deadline = clock() + STOP_SECONDS
    while not exited_unreaped(server) and clock() < deadline:
        time.sleep(POLL_SECONDS)
    signal_group(server, signal.SIGKILL)
    server.wait()


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
    path.unlink(missing_ok=True)
    return open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)


def note(text: str) -> None:
    print(f"bellhop: codex session: {text}", file=sys.stderr, flush=True)


def start_server(plan: Plan, log_file: Path) -> subprocess.Popen[bytes]:
    log = server_log(log_file)
    try:
        return subprocess.Popen(
            plan.server, stdin=subprocess.DEVNULL, stdout=log, stderr=log, process_group=0
        )
    finally:
        os.close(log)


def exit_status(status: int) -> int:
    return 128 - status if status < 0 else status


@dataclass
class Attended:
    child: subprocess.Popen[bytes] | None = None
    signalled: int | None = None

    def forward(self, signum: int) -> bool:
        if self.child is None:
            return False
        self.signalled = signum
        with contextlib.suppress(ProcessLookupError):
            self.child.send_signal(signum)
        return True

    def attend(self, argv: Sequence[str]) -> int:
        signal.signal(signal.SIGINT, ignore)
        self.child = subprocess.Popen(argv)
        try:
            status = self.child.wait()
        finally:
            self.child = None
        return 128 + self.signalled if self.signalled is not None else exit_status(status)


def forward_signals(attended: Attended, idle: Callable[[int], None]) -> None:
    def handle(signum: int, frame: FrameType | None) -> None:
        if not attended.forward(signum):
            idle(signum)
            sys.exit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, handle)


def without_server(fallback: Sequence[str] | None, attended: Attended) -> int:
    if fallback is None:
        return FAILURE_EXIT
    note("starting codex without it; bellhop cannot reach this session")
    try:
        return attended.attend(fallback)
    except OSError as error:
        note(f"cannot run {fallback[0]}: {error.strerror or error}")
        return FAILURE_EXIT


def nothing(signum: int) -> None:
    return None


def run(
    plan: Plan,
    log_file: Path,
    *,
    fallback: Sequence[str] | None = None,
    trace: Emit = quiet,
    keep_log: bool = True,
) -> int:
    attended = Attended()
    try:
        server = start_server(plan, log_file)
    except OSError as error:
        if fallback is None:
            raise
        note(f"cannot start the app server: {error.strerror or error}")
        trace("failed", socket=str(plan.socket), error=str(error), fallback=True)
        forward_signals(attended, nothing)
        return without_server(fallback, attended)

    def clean_up(signum: int | None = None) -> None:
        stop(server)
        remove_socket(plan.socket, os.getuid())
        if signum is not None:
            trace("stop", socket=str(plan.socket), signal=signum)

    forward_signals(attended, clean_up)
    failed = False
    try:
        if wait_for_socket(plan.socket, server):
            trace("listening", socket=str(plan.socket), server=server.pid)
            code = attended.attend(plan.tui)
            trace("stop", socket=str(plan.socket), exit=code, signal=attended.signalled)
            return code
        failed = True
        note(f"the app server did not open {plan.socket}; see {log_file}")
        trace("failed", socket=str(plan.socket), log=str(log_file), fallback=fallback is not None)
    except KeyboardInterrupt:
        return 128 + signal.SIGINT
    finally:
        clean_up()
        if not keep_log and not failed:
            with contextlib.suppress(OSError):
                log_file.unlink(missing_ok=True)
    return without_server(fallback, attended)


def main(argv: Sequence[str], env: Mapping[str, str], caller_umask: int) -> int:
    plan = parse(argv)
    if plan is None:
        note("bad arguments")
        return USAGE_EXIT
    os.umask(caller_umask)
    return run(plan, log_path(env, plan.socket))
