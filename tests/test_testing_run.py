import hashlib
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from bellhop.testing.run import (
    SMOKE_SOCKET,
    Proc,
    SmokeError,
    SmokeRun,
    codex_socket_targets,
    descendants,
    parse_ps,
    protected,
    ps,
    remove_codex_sockets,
)

FAKES = Path(__file__).resolve().parent / "fakes"
CLAUDE_READY = "❯ \n  ⏸ manual mode on · ? for shortcuts\n"
SPAWNED: list[subprocess.Popen[bytes]] = []
RUNS: list[SmokeRun] = []


@pytest.fixture(autouse=True)
def reap() -> Iterator[None]:
    yield
    while RUNS:
        run = RUNS.pop()
        if run._socket_lock is not None:
            run.teardown()
    while SPAWNED:
        proc = SPAWNED.pop()
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)


def spawn(*argv: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen([*argv], stdin=subprocess.DEVNULL, start_new_session=True)
    SPAWNED.append(proc)
    return proc


def child_of(parent: subprocess.Popen[bytes]) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        children = [proc.pid for proc in ps() if proc.ppid == parent.pid]
        if children:
            return children[0]
        time.sleep(0.05)
    raise AssertionError(f"pid {parent.pid} never started a child")


def gone(pid: int) -> bool:
    mine = next((proc for proc in SPAWNED if proc.pid == pid), None)
    if mine is not None:
        try:
            mine.wait(5)
        except subprocess.TimeoutExpired:
            return False
        return True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if all(proc.pid != pid for proc in ps()):
            return True
        time.sleep(0.05)
    return False


def fake_run(tmp_path: Path, **changes: object) -> SmokeRun:
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        (bin_dir / "tmux").symlink_to(FAKES / "tmux")
        claude = bin_dir / "claude"
        claude.write_text("#!/bin/sh\necho '2.1.325 (Claude Code)'\n")
        claude.chmod(0o755)
        fake_hls = tmp_path / "hooklinesinker"
        fake_hls.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {tmp_path / 'hls.log'}\n")
        fake_hls.chmod(0o755)
    settings: dict[str, object] = {
        "cache": tmp_path / "cache",
        "environ": {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}", "HOME": str(tmp_path / "user")},
        "hls_source": tmp_path / "hooklinesinker",
        "base": tmp_path / "base",
    }
    run = SmokeRun(**(settings | changes))  # type: ignore[arg-type]
    RUNS.append(run)
    run.layout.root.mkdir(parents=True, exist_ok=True)
    if not run.layout.hls.exists():
        run.layout.hls.parent.mkdir(parents=True, exist_ok=True)
        run.layout.hls.symlink_to(tmp_path / "hooklinesinker")
    (run.layout.root / "fake-screen.txt").write_text(CLAUDE_READY)
    return run


def write(run: SmokeRun, name: str, text: str) -> None:
    (run.layout.root / name).write_text(text)


def launch_pane(run: SmokeRun, name: str) -> subprocess.Popen[bytes]:
    pane = spawn("/bin/sh", "-c", "sleep 300 & wait")
    write(run, "fake-pane-pid.txt", f"{pane.pid}\n")
    run.launch("claude", name)
    return pane


def test_descendants_walks_the_whole_tree_from_the_roots() -> None:
    procs = [Proc(1, 0, "init"), Proc(11, 1, "sops"), Proc(12, 11, "claude"), Proc(13, 12, "relay")]

    assert descendants(procs, {11}) == {11, 12, 13}


def test_parse_ps_keeps_the_start_time_as_part_of_the_identity() -> None:
    [proc] = parse_ps("  42     1 Sat Nov 14 00:12:34 2026     /bin/sleep 300\n")

    assert proc.identity == (42, "Sat Nov 14 00:12:34 2026", "/bin/sleep 300")


def test_the_process_floor_protects_init_and_the_caller_and_its_ancestors() -> None:
    never = protected(ps())

    assert {0, 1, os.getpid(), os.getppid()} <= never


@pytest.mark.parametrize("run_id", ["", "..", "../../x", "20261113-000000-zzzz"])
def test_a_run_id_must_look_like_one(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(SmokeError, match="is not YYYYMMDD-HHMMSS-xxxx"):
        SmokeRun(tmp_path, {"PATH": os.defpath}, run_id=run_id)


def test_paths_come_from_the_given_environment_and_base(tmp_path: Path) -> None:
    run = SmokeRun(
        tmp_path, {"HOME": "/home/me"}, run_id="20261113-000000-abcd", base=tmp_path / "base"
    )

    assert run.socket == SMOKE_SOCKET == "bellhop-smoke"
    assert run.layout.runtime_dir == tmp_path / "base" / "bellhop-smoke-20261113-000000-abcd"
    assert run.paths.sops_exec == Path("/home/me/ac/bin/sops-exec")
    assert run.paths.codex_auth == Path("/home/me/.codex/auth.json")
    assert SmokeRun(tmp_path, {}, sops_exec=Path()).paths.sops_exec == Path()


def test_a_live_pane_the_run_created_is_torn_down_with_its_children(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    pane = launch_pane(run, "c1")
    child = child_of(pane)
    write(run, "fake-panes.txt", f"c1 0 {pane.pid}\n")

    leftovers = run.teardown()

    assert leftovers == []
    assert gone(pane.pid)
    assert gone(child)


def test_dead_reused_and_foreign_panes_are_never_roots(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    dead = launch_pane(run, "dead")
    reused = launch_pane(run, "reused")
    run.panes["reused"] = replace(run.panes["reused"], started="Mon Jan  1 00:00:00 2001")
    foreign = spawn("/bin/sh", "-c", "sleep 300 & wait")
    write(
        run, "fake-panes.txt", f"dead 1 {dead.pid}\nreused 0 {reused.pid}\nother 0 {foreign.pid}\n"
    )

    run.teardown()

    assert (dead.poll(), reused.poll(), foreign.poll()) == (None, None, None)


def test_a_pid_file_in_the_runtime_dir_adopts_nothing(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    stranger = spawn("/bin/sleep", "300")
    write(run, "fake-panes.txt", "")
    (run.layout.runtime_dir / "relay.pid").write_text(f"{stranger.pid}\n")

    run.teardown()

    assert stranger.poll() is None


def test_adopt_refuses_init_the_caller_and_its_parent(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    for pid in (1, os.getpid(), os.getppid(), 99999999):
        with pytest.raises(SmokeError, match=f"cannot adopt pid {pid}"):
            run.adopt(pid)


def test_teardown_sends_sigterm_before_sigkill(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    marker = tmp_path / "terminated"
    polite = spawn(
        "/bin/sh", "-c", f"trap 'touch {marker}; exit 0' TERM; while :; do sleep 0.05; done"
    )
    time.sleep(0.2)
    run.adopt(polite.pid)

    assert run.teardown() == []
    assert polite.wait(5) == 0
    assert marker.exists()


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bellhop.testing.run.EXIT_GRACE_SECONDS", 0.3)
    run = fake_run(tmp_path)
    run.start()
    stubborn = spawn("/bin/sh", "-c", "trap '' TERM; while :; do sleep 0.05; done")
    time.sleep(0.2)
    run.adopt(stubborn.pid)

    assert run.teardown() == []
    assert stubborn.wait(5) == -signal.SIGKILL


def test_a_process_running_a_binary_from_the_run_root_is_owned(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    local = run.layout.root / "bin" / "sleep"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.symlink_to("/bin/sleep")
    daemon = spawn(str(local), "300")
    named = spawn("/bin/sh", "-c", "sleep 300; :", str(run.layout.root / "logs" / "x.log"))
    time.sleep(0.3)
    assert (daemon.poll(), named.poll()) == (None, None)

    run.teardown()

    assert gone(daemon.pid)
    assert named.poll() is None


def test_only_codex_sockets_named_by_their_link_are_removed(tmp_path: Path) -> None:
    shared = tmp_path / "codex-daemon-501"
    shared.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    proper = shared / hashlib.sha256(str(runtime.resolve() / "srv.sock").encode()).hexdigest()
    for path in (proper, proper.with_name(proper.name + ".lock"), shared / "users-daemon"):
        path.write_text("")
    (runtime / "srv.sock").symlink_to(proper)
    (runtime / "misnamed.sock").symlink_to(shared / "users-daemon")
    outside = tmp_path / "elsewhere"
    outside.write_text("")
    (runtime / "outside.sock").symlink_to(outside)

    assert codex_socket_targets(runtime, shared) == [proper]
    remove_codex_sockets(runtime, shared)

    assert sorted(path.name for path in shared.iterdir()) == ["users-daemon"]
    assert outside.exists()


def test_two_runs_on_the_same_socket_exclude_each_other(tmp_path: Path) -> None:
    first = fake_run(tmp_path, run_id="20261113-000000-aaaa")
    second = fake_run(tmp_path, run_id="20261113-000000-bbbb")
    first.start()

    with pytest.raises(SmokeError, match="another smoke run holds"):
        second.start()
    first.teardown()
    second.start()
    assert second.teardown() == []


def test_start_sweeps_what_an_earlier_run_left_behind(tmp_path: Path) -> None:
    earlier = fake_run(tmp_path, run_id="20261113-000000-aaaa")
    earlier.layout.runtime_dir.mkdir(parents=True)
    local = earlier.layout.root / "bin" / "sleep"
    local.parent.mkdir(parents=True)
    local.symlink_to("/bin/sleep")
    orphan = spawn(str(local), "300")
    bystander = spawn("/bin/sleep", "300")
    run = fake_run(tmp_path, run_id="20261113-000000-bbbb")
    time.sleep(0.3)
    assert (orphan.poll(), bystander.poll()) == (None, None)

    run.start()

    assert gone(orphan.pid)
    assert bystander.poll() is None
    assert not earlier.layout.runtime_dir.exists()
    assert run.teardown() == []


def test_teardown_finishes_and_reports_when_a_step_fails(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    (run.layout.codex_dir / "app-server-daemon").mkdir(parents=True)

    leftovers = run.teardown()

    assert len(leftovers) == 1
    assert leftovers[0].startswith("codex daemon stop failed: AgentSetupError: codex not found")
    assert not run.layout.runtime_dir.exists()


def test_a_sigterm_inside_the_run_still_tears_down(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    before = signal.getsignal(signal.SIGTERM)

    with pytest.raises(KeyboardInterrupt), run:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)

    assert not run.layout.runtime_dir.exists()
    assert signal.getsignal(signal.SIGTERM) is before


def test_the_server_check_asks_tmux(tmp_path: Path) -> None:
    run = fake_run(tmp_path)

    assert run.tmux.server_alive() is False
    write(run, "fake-server-up.txt", "")
    assert run.tmux.server_alive() is True


def test_a_run_installs_hls_launches_and_records_the_pane(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    pane = launch_pane(run, "c1")

    assert run.wait_ready("c1") == []
    assert run.panes["c1"].pid == pane.pid
    assert (tmp_path / "hls.log").read_text().splitlines() == [
        "install --consumer bellhop-smoke",
        "hooks install --agent claude",
        "hooks install --agent codex",
        "hooks install --agent pi",
    ]
    log = (run.layout.root / "fake-tmux.log").read_text().splitlines()
    new_session = next(line for line in log if " new-session " in line)
    assert "AGENT_REQUIRE_SECRETS=1" in new_session
    assert new_session.endswith(
        f"--session-id {run.launched['c1'].session_id} --model haiku --permission-mode default"
    )
    assert run.teardown() == []


def test_wait_ready_answers_a_startup_dialog_and_then_returns(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    write(run, "fake-screen.txt", "Do you trust the files in this folder?\n")
    write(run, "fake-after-keys.txt", CLAUDE_READY)
    run.launch("claude", "c1")

    assert run.wait_ready("c1", timeout=10) == ["trust-folder"]
    run.teardown()


def test_wait_ready_gives_up_on_a_dialog_that_keeps_coming_back(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    write(run, "fake-screen.txt", "Do you trust the files in this folder?\n")
    run.launch("claude", "c1")

    with pytest.raises(SmokeError, match="keeps showing the trust-folder dialog"):
        run.wait_ready("c1", timeout=30)
    run.teardown()


def test_wait_ready_reports_a_login_screen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bellhop.testing.run.AUTH_HOLD_SECONDS", 0.2)
    run = fake_run(tmp_path)
    run.start()
    write(run, "fake-screen.txt", "Select login method:\n")
    run.launch("claude", "c1")

    with pytest.raises(SmokeError, match="shows a login screen: Select login method"):
        run.wait_ready("c1", timeout=10)
    run.teardown()


def test_wait_ready_saves_the_pane_of_a_session_that_died(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    write(run, "fake-screen.txt", "MCP config file not found\n")
    write(run, "fake-dead.txt", "1\n")
    run.launch("claude", "c1")

    with pytest.raises(SmokeError, match="exited before it was ready"):
        run.wait_ready("c1", timeout=10)
    assert "MCP config file not found" in (run.layout.logs / "c1-exited.txt").read_text()
    run.teardown()


def test_wait_ready_times_out_on_a_screen_that_never_settles(tmp_path: Path) -> None:
    run = fake_run(tmp_path)
    run.start()
    write(run, "fake-screen.txt", "loading\n")
    run.launch("claude", "c1")

    with pytest.raises(SmokeError, match="was not ready within 1s"):
        run.wait_ready("c1", timeout=1)
    run.teardown()


def test_a_with_run_refused_the_lock_touches_nothing_shared(tmp_path: Path) -> None:
    first = fake_run(tmp_path, run_id="20261113-000000-aaaa")
    second = fake_run(tmp_path, run_id="20261113-000000-bbbb")
    first.start()

    with pytest.raises(SmokeError, match="another smoke run holds"), second:
        pass

    second_log = second.layout.root / "fake-tmux.log"
    assert not second_log.exists() or "kill-server" not in second_log.read_text()
    assert second.leftovers == []
    assert first.teardown() == []


def test_the_sweep_skips_a_symlinked_run_entry_and_leaves_its_processes(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "bin").mkdir(parents=True)
    (elsewhere / "bin" / "sleep").symlink_to("/bin/sleep")
    stranger = spawn(str(elsewhere / "bin" / "sleep"), "300")
    time.sleep(0.3)
    assert stranger.poll() is None
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "20261113-000000-aaaa").symlink_to(elsewhere)
    run = fake_run(tmp_path, run_id="20261113-000000-bbbb")

    run.start()

    assert run.leftovers == [
        f"skipped {tmp_path / 'cache' / '20261113-000000-aaaa'}: it is a symlink"
    ]
    assert stranger.poll() is None
    run.teardown()


def test_the_sweep_skips_a_symlinked_runtime_dir(tmp_path: Path) -> None:
    earlier = fake_run(tmp_path, run_id="20261113-000000-aaaa")
    target = tmp_path / "not-a-runtime-dir"
    target.mkdir()
    (target / "keep.txt").write_text("")
    earlier.layout.runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    earlier.layout.runtime_dir.symlink_to(target)
    run = fake_run(tmp_path, run_id="20261113-000000-bbbb")

    run.start()

    assert run.leftovers == [f"skipped {earlier.layout.runtime_dir}: it is a symlink"]
    assert (target / "keep.txt").exists()
    run.teardown()


def test_the_sweep_reports_a_process_it_cannot_prove_was_the_earlier_runs(tmp_path: Path) -> None:
    earlier = fake_run(tmp_path, run_id="20261113-000000-aaaa")
    named = spawn("/bin/sh", "-c", "sleep 300; :", str(earlier.layout.runtime_dir / "srv.sock"))
    time.sleep(0.2)
    run = fake_run(tmp_path, run_id="20261113-000000-bbbb")

    run.start()

    assert any(note.startswith(f"left pid {named.pid} ") for note in run.leftovers)
    assert named.poll() is None
    run.teardown()


def test_signals_during_teardown_do_not_cut_it_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = fake_run(tmp_path)

    def interrupted_daemon_stop(self: SmokeRun) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.2)

    monkeypatch.setattr(SmokeRun, "_stop_daemon", interrupted_daemon_stop)
    before = signal.getsignal(signal.SIGINT)

    with run:
        pass

    assert run.leftovers == []
    assert not run.layout.runtime_dir.exists()
    assert signal.getsignal(signal.SIGINT) is before


def test_the_sweep_notes_survive_a_with_run(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "20261113-000000-aaaa").symlink_to(elsewhere)
    run = fake_run(tmp_path, run_id="20261113-000000-bbbb")

    with run:
        pass

    assert run.leftovers == [
        f"skipped {tmp_path / 'cache' / '20261113-000000-aaaa'}: it is a symlink"
    ]


def test_the_sweep_does_not_follow_a_symlinked_codex_home_inside_an_earlier_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = fake_run(tmp_path, run_id="20261113-000000-aaaa")
    real_codex = tmp_path / "user" / ".codex"
    (real_codex / "app-server-daemon").mkdir(parents=True)
    (real_codex / "app-server-daemon" / "daemon.pid").write_text("{}")
    earlier.layout.home.mkdir(parents=True, exist_ok=True)
    earlier.layout.codex_dir.symlink_to(real_codex)
    run = fake_run(tmp_path, run_id="20261113-000000-bbbb")
    scanned: list[Path] = []
    monkeypatch.setattr("bellhop.testing.run.remove_codex_sockets", scanned.append)

    notes = run.sweep_earlier_runs()

    assert real_codex / "app-server-daemon" not in {path.resolve() for path in scanned}
    prefix = "skipped the codex cleanup of 20261113-000000-aaaa"
    daemon_pid = earlier.layout.codex_dir / "app-server-daemon" / "daemon.pid"
    assert notes == [
        f"{prefix}: {earlier.layout.codex_dir} is or lies under a symlink",
        f"{prefix}: {earlier.layout.codex_dir / 'app-server-daemon'} is or lies under a symlink",
        f"{prefix}: {daemon_pid} is or lies under a symlink",
    ]
    assert (real_codex / "app-server-daemon" / "daemon.pid").exists()


def test_the_sweep_skips_an_earlier_run_owned_by_another_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = fake_run(tmp_path, run_id="20261113-000000-aaaa")
    run = fake_run(tmp_path, run_id="20261113-000000-bbbb")
    owner = earlier.layout.root.lstat().st_uid
    monkeypatch.setattr("bellhop.testing.run.os.getuid", lambda: owner + 1)

    notes = run.sweep_earlier_runs()

    assert notes == [f"skipped {earlier.layout.root}: it is owned by uid {owner}"]


def test_the_sweep_does_not_stop_a_daemon_whose_pid_file_is_a_link(tmp_path: Path) -> None:
    earlier = fake_run(tmp_path, run_id="20261113-000000-aaaa")
    daemon_dir = earlier.layout.codex_dir / "app-server-daemon"
    daemon_dir.mkdir(parents=True)
    real_pid = tmp_path / "user-daemon.pid"
    real_pid.write_text("{}")
    (daemon_dir / "daemon.pid").symlink_to(real_pid)
    run = fake_run(tmp_path, run_id="20261113-000000-bbbb")

    notes = run.sweep_earlier_runs()

    link = daemon_dir / "daemon.pid"
    assert notes == [
        f"skipped the codex cleanup of {earlier.run_id}: {link} is or lies under a symlink"
    ]
