import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from bellhop.testing.run import (
    SMOKE_SOCKET,
    Launched,
    Proc,
    SmokeError,
    SmokeRun,
    descendants,
    parse_ps,
    ps,
    remove_codex_sockets,
)


def test_descendants_walks_the_whole_tree_from_the_roots() -> None:
    procs = [
        Proc(1, 0, "init"),
        Proc(10, 1, "tmux"),
        Proc(11, 10, "sops"),
        Proc(12, 11, "claude"),
        Proc(13, 12, "relay"),
        Proc(20, 1, "other"),
    ]

    assert descendants(procs, {11}) == {11, 12, 13}


def test_ps_lists_this_process() -> None:
    assert any(proc.pid == os.getpid() for proc in ps())


def test_a_run_lives_under_the_cache_with_a_short_runtime_dir(tmp_path: Path) -> None:
    run = SmokeRun(tmp_path, {"PATH": os.defpath}, run_id="20261113-000000-abcd")

    assert run.socket == SMOKE_SOCKET == "bellhop-smoke"
    assert run.layout.root == tmp_path / "20261113-000000-abcd"
    assert run.layout.runtime_dir == Path("/tmp/bellhop-smoke-20261113-000000-abcd")
    assert len(str(run.layout.runtime_dir / "claude-99999.sock")) < 104
    assert run.env["HOME"] == str(run.layout.home)
    assert run.tmux.socket == "bellhop-smoke"


def test_survivors_match_tracked_pids_by_command(tmp_path: Path) -> None:
    run = SmokeRun(tmp_path, {"PATH": os.defpath})
    me = next(proc for proc in ps() if proc.pid == os.getpid())
    run.tracked = {me.pid: me, 1: Proc(1, 0, "not the real command", me.started)}

    assert run.survivors() == [me]


FAKES = Path(__file__).resolve().parent / "fakes"
CLAUDE_READY = "❯ \n  ⏸ manual mode on · ? for shortcuts\n"


def test_a_run_installs_hls_launches_waits_and_tears_down_through_tmux(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "tmux").symlink_to(FAKES / "tmux")
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\necho '2.1.325 (Claude Code)'\n")
    claude.chmod(0o755)
    hls_log = tmp_path / "hls.log"
    fake_hls = tmp_path / "hooklinesinker"
    fake_hls.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {hls_log}\n")
    fake_hls.chmod(0o755)
    environ = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"}
    run = SmokeRun(
        tmp_path / "cache",
        environ,
        sops_exec=tmp_path / "sops-exec",
        hls_source=fake_hls,
        user_home=tmp_path / "user",
    )
    hls = run.layout.hls
    hls.parent.mkdir(parents=True)
    hls.symlink_to(fake_hls)
    run.layout.root.mkdir(parents=True, exist_ok=True)
    (run.layout.root / "fake-screen.txt").write_text(CLAUDE_READY)
    tmux_log = run.layout.root / "fake-tmux.log"

    run.start()
    launched = run.launch("claude", "c1", prompt="Reply OK")
    answered = run.wait_ready("c1")
    leftovers = run.teardown()

    assert (launched.pane_pid, launched.pane_id) == (99999999, "%0")
    assert answered == []
    assert leftovers == []
    assert hls_log.read_text().splitlines() == [
        "install --consumer bellhop-smoke",
        "hooks install --agent claude",
        "hooks install --agent codex",
        "hooks install --agent pi",
    ]
    commands = [line.split()[4] for line in tmux_log.read_text().splitlines()]
    assert commands[0] == "kill-server"
    assert "new-session" in commands
    assert commands[-2:] == ["kill-server", "list-sessions"]
    new_session = next(line for line in tmux_log.read_text().splitlines() if "new-session" in line)
    assert "AGENT_REQUIRE_SECRETS=1" in new_session
    assert new_session.endswith(
        f"--session-id {launched.session_id} --model haiku --permission-mode default Reply OK"
    )
    assert not run.layout.runtime_dir.exists()


def test_only_codex_sockets_named_by_the_isolated_home_are_removed(tmp_path: Path) -> None:
    shared = tmp_path / "codex-daemon-501"
    shared.mkdir()
    ours = shared / "aaaa"
    ours.write_text("")
    (shared / "aaaa.lock").write_text("")
    users = shared / "bbbb"
    users.write_text("")
    elsewhere = tmp_path / "elsewhere.sock"
    elsewhere.write_text("")
    codex = tmp_path / "home" / ".codex"
    (codex / "app-server-daemon").mkdir(parents=True)
    (codex / "app-server-control").mkdir()
    (codex / "app-server-daemon" / "daemon-updater.sock").symlink_to(ours)
    (codex / "app-server-control" / "app-server-control.sock").symlink_to(elsewhere)

    remove_codex_sockets(codex, shared)

    assert sorted(path.name for path in shared.iterdir()) == ["bbbb"]
    assert elsewhere.exists()


def _fake_run(tmp_path: Path) -> SmokeRun:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if not (bin_dir / "tmux").exists():
        (bin_dir / "tmux").symlink_to(FAKES / "tmux")
        claude = bin_dir / "claude"
        claude.write_text("#!/bin/sh\necho '2.1.325 (Claude Code)'\n")
        claude.chmod(0o755)
    fake_hls = tmp_path / "hooklinesinker"
    fake_hls.write_text("#!/bin/sh\nexit 0\n")
    fake_hls.chmod(0o755)
    run = SmokeRun(
        tmp_path / "cache",
        {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}", "HOME": str(tmp_path / "user")},
        hls_source=fake_hls,
    )
    run.layout.hls.parent.mkdir(parents=True)
    run.layout.hls.symlink_to(fake_hls)
    run.layout.root.mkdir(parents=True, exist_ok=True)
    (run.layout.root / "fake-screen.txt").write_text(CLAUDE_READY)
    run.start()
    return run


def _sleeper(*argv: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([*argv], stdin=subprocess.DEVNULL, start_new_session=True)


def test_teardown_kills_only_processes_the_run_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bellhop.testing.run.EXIT_GRACE_SECONDS", 0.5)
    run = _fake_run(tmp_path)
    local_sleep = run.layout.root / "bin" / "sleep"
    local_sleep.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy("/bin/sleep", local_sleep)
    adopted = _sleeper("/bin/sleep", "300")
    under_root = _sleeper(str(local_sleep), "300")
    names_the_run = _sleeper(
        "/bin/sh", "-c", "sleep 300; :", str(run.layout.root / "logs" / "x.log")
    )
    stale_pane = _sleeper("/bin/sh", "-c", "sleep 300; :")
    try:
        run.launched["gone"] = Launched(
            "gone", "claude", run.layout.work, None, stale_pane.pid, "%9"
        )
        run.adopt(adopted.pid)

        leftovers = run.teardown()

        assert leftovers == []
        assert adopted.wait(5) == -signal.SIGKILL
        assert under_root.wait(5) == -signal.SIGKILL
        assert names_the_run.poll() is None
        assert stale_pane.poll() is None
    finally:
        for proc in (adopted, under_root, names_the_run, stale_pane):
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)


def test_a_pid_file_in_the_runtime_dir_adopts_that_process(tmp_path: Path) -> None:
    run = _fake_run(tmp_path)
    relay = _sleeper("/bin/sleep", "300")
    try:
        (run.layout.runtime_dir / "relay.pid").write_text(f"{relay.pid}\n")
        run.track()

        assert relay.pid in run.tracked
    finally:
        relay.kill()
        relay.wait(5)
        run.teardown()


def _launched(run: SmokeRun) -> None:
    run.launch("claude", "c1")


def test_wait_ready_answers_a_startup_dialog_and_then_returns(tmp_path: Path) -> None:
    run = _fake_run(tmp_path)
    (run.layout.root / "fake-screen.txt").write_text("Do you trust the files in this folder?\n")
    (run.layout.root / "fake-after-keys.txt").write_text(CLAUDE_READY)
    _launched(run)

    assert run.wait_ready("c1", timeout=10) == ["trust-folder"]
    run.teardown()


def test_wait_ready_gives_up_on_a_dialog_that_keeps_coming_back(tmp_path: Path) -> None:
    run = _fake_run(tmp_path)
    (run.layout.root / "fake-screen.txt").write_text("Do you trust the files in this folder?\n")
    _launched(run)

    with pytest.raises(SmokeError, match="keeps showing the trust-folder dialog"):
        run.wait_ready("c1", timeout=30)
    run.teardown()


def test_wait_ready_reports_a_login_screen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bellhop.testing.run.AUTH_HOLD_SECONDS", 0.2)
    run = _fake_run(tmp_path)
    (run.layout.root / "fake-screen.txt").write_text("Select login method:\n")
    _launched(run)

    with pytest.raises(SmokeError, match="shows a login screen: Select login method"):
        run.wait_ready("c1", timeout=10)
    run.teardown()


def test_wait_ready_saves_the_pane_of_a_session_that_died(tmp_path: Path) -> None:
    run = _fake_run(tmp_path)
    (run.layout.root / "fake-screen.txt").write_text("MCP config file not found\n")
    (run.layout.root / "fake-dead.txt").write_text("1\n")
    _launched(run)

    with pytest.raises(SmokeError, match="exited before it was ready"):
        run.wait_ready("c1", timeout=10)
    assert "MCP config file not found" in (run.layout.logs / "c1-exited.txt").read_text()
    run.teardown()


def test_wait_ready_times_out_on_a_screen_that_never_settles(tmp_path: Path) -> None:
    run = _fake_run(tmp_path)
    (run.layout.root / "fake-screen.txt").write_text("loading\n")
    _launched(run)

    with pytest.raises(SmokeError, match="was not ready within 1s"):
        run.wait_ready("c1", timeout=1)
    run.teardown()


def test_parse_ps_keeps_the_start_time_as_part_of_the_identity() -> None:
    [proc] = parse_ps("  42     1 Sat Nov 14 00:12:34 2026     /bin/sleep 300\n")

    assert proc == Proc(42, 1, "/bin/sleep 300", "Sat Nov 14 00:12:34 2026")
    assert proc.identity == (42, "Sat Nov 14 00:12:34 2026", "/bin/sleep 300")
