import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bellhop.codex_session import (
    parse,
    remove_socket,
    server_log,
    session_argv,
    stop,
    wait_for_socket,
)

SERVER = """
import os, sys, time
link, target, pidfile = sys.argv[1:4]
open(pidfile, "w").write(str(os.getpid()))
os.makedirs(os.path.dirname(target), exist_ok=True)
open(target, "w").close()
open(target + ".lock", "w").close()
os.symlink(target, link)
time.sleep(60)
"""


def daemon_target(daemons: Path, link: Path) -> Path:
    name = hashlib.sha256(str(link.parent.resolve() / link.name).encode()).hexdigest()
    return daemons / f"codex-daemon-{os.getuid()}" / name


READY_SECONDS = 60
MARKING_TUI = """
import sys, time
open(sys.argv[1], "w").close()
time.sleep(60)
"""


def gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def wrapper(tmp_path: Path, server: str, tui: list[str]) -> tuple[list[str], Path, Path, Path]:
    link = tmp_path / "rt" / "codex-nanna-abcd.sock"
    link.parent.mkdir()
    target = daemon_target(tmp_path / "daemons", link)
    pidfile = tmp_path / "server.pid"
    serve = [sys.executable, "-c", server, str(link), str(target), str(pidfile)]
    argv = [sys.executable, "-I", "-m", "bellhop", *session_argv(link, serve, tui)]
    return argv, link, target, pidfile


def env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}


def test_the_arguments_round_trip() -> None:
    argv = session_argv(Path("/rt/s.sock"), ["codex", "app-server", "--"], ["codex", "--", "hi"])

    plan = parse(argv[1:])

    assert plan is not None
    assert (plan.socket, plan.server, plan.tui) == (
        Path("/rt/s.sock"),
        ["codex", "app-server", "--"],
        ["codex", "--", "hi"],
    )


def test_bad_arguments_do_not_parse() -> None:
    assert [parse(["/s", "x", "a"]), parse(["/s", "2", "a", "b"]), parse(["/s"])] == [None] * 3


def test_when_the_tui_exits_the_server_stops_and_its_socket_is_removed(tmp_path: Path) -> None:
    tui = [sys.executable, "-c", "import sys; sys.exit(3)"]
    argv, link, target, pidfile = wrapper(tmp_path, SERVER, tui)

    code = subprocess.run(argv, env=env(tmp_path), timeout=30, check=False).returncode

    assert code == 3
    assert gone(int(pidfile.read_text())) is True
    assert (
        link.is_symlink(),
        target.exists(),
        target.with_name(f"{target.name}.lock").exists(),
    ) == (
        False,
        False,
        False,
    )
    assert (tmp_path / "state" / "bellhop" / "codex-nanna.log").exists() is True


def started(marker: Path, process: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + READY_SECONDS
    while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return marker.exists()


def test_a_hangup_stops_the_server_too(tmp_path: Path) -> None:
    marker = tmp_path / "tui-started"
    tui = [sys.executable, "-c", MARKING_TUI, str(marker)]
    argv, link, _, pidfile = wrapper(tmp_path, SERVER, tui)
    process = subprocess.Popen(argv, env=env(tmp_path), process_group=0)
    if not started(marker, process):
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(10)
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(pidfile.read_text()), signal.SIGKILL)
        pytest.fail("the wrapper never became ready")

    process.send_signal(signal.SIGHUP)
    code = process.wait(10)

    assert (code, gone(int(pidfile.read_text())), link.is_symlink()) == (
        128 + signal.SIGHUP,
        True,
        False,
    )


def test_waiting_for_the_socket_gives_up_at_its_deadline(tmp_path: Path) -> None:
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ticks = iter(range(0, 1000, 5))
    try:
        opened = wait_for_socket(tmp_path / "never.sock", server, clock=lambda: float(next(ticks)))
    finally:
        server.kill()
        server.wait(5)

    assert opened is False


def test_waiting_for_the_socket_ends_when_the_server_exits(tmp_path: Path) -> None:
    server = subprocess.Popen([sys.executable, "-c", "pass"])
    server.wait(10)

    assert wait_for_socket(tmp_path / "never.sock", server) is False


def test_only_a_daemon_socket_named_for_the_link_is_removed(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere" / "keep.sock"
    other.parent.mkdir()
    other.touch()
    link = tmp_path / "s.sock"
    link.symlink_to(other)

    remove_socket(link, os.getuid())

    assert (link.is_symlink(), other.exists()) == (False, True)


SPAWNING_SERVER = """
import os, subprocess, sys, time
link, target, pidfile = sys.argv[1:4]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(pidfile, "w").write(str(child.pid))
os.makedirs(os.path.dirname(target), exist_ok=True)
open(target, "w").close()
os.symlink(target, link)
child.wait()
"""
SIGINT_TUI = """
import signal, sys
open(sys.argv[1], "w").write(str(signal.getsignal(signal.SIGINT) == signal.SIG_IGN))
"""


def test_the_server_s_whole_process_group_stops_with_the_tui(tmp_path: Path) -> None:
    argv, _, _, pidfile = wrapper(tmp_path, SPAWNING_SERVER, ["/usr/bin/true"])

    subprocess.run(argv, env=env(tmp_path), timeout=30, check=False)

    assert gone(int(pidfile.read_text())) is True


def test_the_tui_does_not_inherit_an_ignored_sigint(tmp_path: Path) -> None:
    seen = tmp_path / "sigint"
    tui = [sys.executable, "-c", SIGINT_TUI, str(seen)]
    argv, _, _, _ = wrapper(tmp_path, SERVER, tui)

    subprocess.run(argv, env=env(tmp_path), timeout=30, check=False)

    assert seen.read_text() == "False"


def test_stopping_a_server_twice_never_signals_a_group_the_second_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0
    )
    signalled: list[int] = []
    real = os.killpg

    def recording(pgid: int, signum: int) -> None:
        signalled.append(signum)
        real(pgid, signum)

    monkeypatch.setattr(os, "killpg", recording)
    stop(server)
    first = list(signalled)
    stop(server)

    assert (first[0], signalled) == (signal.SIGTERM, first)


UMASK_TUI = """
import os, sys
mask = os.umask(0)
open(sys.argv[1], "w").write(oct(mask))
"""


def test_the_server_and_tui_get_the_caller_s_umask_not_bellhop_s(tmp_path: Path) -> None:
    seen = tmp_path / "umask"
    argv, _, _, _ = wrapper(tmp_path, SERVER, [sys.executable, "-c", UMASK_TUI, str(seen)])

    subprocess.run(argv, env=env(tmp_path), timeout=30, check=False, umask=0o022)

    assert seen.read_text() == "0o22"


def test_the_server_log_replaces_a_planted_link_and_leaves_its_target(tmp_path: Path) -> None:
    target = tmp_path / "zshrc"
    target.write_text("mine\n")
    log = tmp_path / "state" / "codex-nanna.log"
    log.parent.mkdir()
    log.symlink_to(target)

    os.close(server_log(log))

    assert (target.read_text(), log.is_symlink()) == ("mine\n", False)


TRAPPING_TUI = """
import signal, sys, time
marker, caught = sys.argv[1:3]
def leave(signum, frame):
    open(caught, "w").write(str(signum))
    sys.exit(0)
signal.signal(signal.SIGTERM, leave)
open(marker, "w").close()
time.sleep(60)
"""


def test_a_termination_reaches_the_tui_so_it_can_leave_by_itself(tmp_path: Path) -> None:
    marker, caught = tmp_path / "tui-started", tmp_path / "tui-caught"
    tui = [sys.executable, "-c", TRAPPING_TUI, str(marker), str(caught)]
    argv, link, _, pidfile = wrapper(tmp_path, SERVER, tui)
    process = subprocess.Popen(argv, env=env(tmp_path), process_group=0)
    if not started(marker, process):
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(10)
        pytest.fail("the wrapper never became ready")

    process.send_signal(signal.SIGTERM)
    code = process.wait(10)

    assert caught.read_text() == str(int(signal.SIGTERM))
    assert (code, gone(int(pidfile.read_text())), link.is_symlink()) == (
        128 + signal.SIGTERM,
        True,
        False,
    )


def test_a_tui_killed_by_a_signal_gives_the_shell_s_exit_code(tmp_path: Path) -> None:
    tui = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
    argv, _, _, _ = wrapper(tmp_path, SERVER, tui)

    code = subprocess.run(argv, env=env(tmp_path), timeout=30, check=False).returncode

    assert code == 128 + signal.SIGKILL
