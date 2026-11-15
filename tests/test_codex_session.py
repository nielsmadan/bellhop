import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bellhop.codex_session import parse, remove_socket, session_argv, stop, wait_for_socket

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


def test_a_hangup_stops_the_server_too(tmp_path: Path) -> None:
    tui = [sys.executable, "-c", "import time; time.sleep(60)"]
    argv, link, _, pidfile = wrapper(tmp_path, SERVER, tui)
    process = subprocess.Popen(argv, env=env(tmp_path))
    deadline = time.monotonic() + 10
    while not link.is_symlink() and time.monotonic() < deadline:
        time.sleep(0.05)

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
