import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from bellhop.cli.waking import server_alive
from bellhop.codes import INVALID_CONFIG, USAGE
from bellhop.codex_session import parse
from bellhop.config import AgentConfig, Config
from bellhop.drain import drainer_starts
from bellhop.errors import BellhopError
from bellhop.identity import Proc
from bellhop.launch import (
    START_FAILED,
    STARTUP,
    Look,
    StartSetup,
    check_start,
    child_env,
    claude_mcp_config,
    decide,
    find_thread,
    plan_start,
    reachable,
    scrubbed,
    start_checker,
)
from bellhop.models import Binding, Session
from bellhop.routes.fake import FakeRoute
from bellhop.store import begin_start, connect
from bellhop.tmux import Tmux, tmux_executable
from conftest import (
    CLAUDE_TRUST,
    NOW,
    STARTED,
    FakeTmux,
    make_message,
    make_session,
)
from fake_app_server import app_server

CLAUDE = AgentConfig(
    "enki", "claude", Path("/work"), ("/opt/fleet/claude",), {"CLAUDE_CONFIG_DIR": "/fleet/cc"}
)
CODEX = AgentConfig(
    "nanna",
    "codex",
    Path("/work"),
    ("/opt/fleet/codex",),
    {"CODEX_HOME": "/fleet/codex"},
    ("/opt/fleet/codex", "app-server"),
)
PI = AgentConfig("utu", "pi", None, ("/opt/fleet/pi",))
CALLER = {
    "HOME": "/home/u",
    "PATH": "/usr/bin",
    "LANG": "en_US.UTF-8",
    "LC_CTYPE": "UTF-8",
    "TMPDIR": "/tmp/u",
    "XDG_STATE_HOME": "/state",
    "BELLHOP_RUNTIME_DIR": "/rt",
    "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/1.sock",
    "CLAUDE_CODE_MESSAGING_TOKEN": "t0ken",
    "CLAUDE_CODE_SESSION_ID": "caller-session",
    "CLAUDE_CONFIG_DIR": "/caller/claude",
    "CODEX_HOME": "/caller/codex",
    "CODEX_THREAD_ID": "caller-thread",
    "PI_CODING_AGENT_DIR": "/caller/pi",
    "PI_SESSION_ID": "caller-pi",
    "ANTHROPIC_API_KEY": "sk-1",
    "OPENAI_API_KEY": "sk-2",
    "GH_TOKEN": "gh-1",
    "SSH_AUTH_SOCK": "/agent.sock",
    "TMUX": "/tmp/tmux-501/default,1,0",
}
SID = "11111111-2222-4333-8444-555555555555"
HINT = "tmux attach -t =enki"
CLAUDE_READY = "❯ \n  ? for shortcuts\n"


def starting(**changes: object) -> Binding:
    base = Binding(
        "enki", "claude", "starting", SID, None, None, None, None, "enki:%0", "launch", None, NOW
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def look(pane: str | None = CLAUDE_READY, **changes: object) -> Look:
    base = Look(pane, HINT, [make_session(SID, pid=5151)], None, lambda session, binding: True)
    return replace(base, **changes)  # type: ignore[arg-type]


def test_the_child_environment_is_the_allowlist_plus_the_agents_own_env() -> None:
    assert child_env(CALLER, CLAUDE) == {
        "HOME": "/home/u",
        "PATH": "/usr/bin",
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "UTF-8",
        "TMPDIR": "/tmp/u",
        "XDG_STATE_HOME": "/state",
        "BELLHOP_RUNTIME_DIR": "/rt",
        "TERM": "tmux-256color",
        "CLAUDE_CONFIG_DIR": "/fleet/cc",
    }


def test_the_child_runs_under_env_minus_i_so_no_shell_or_server_environment_applies() -> None:
    argv = scrubbed({"B": "two words", "A": "$(touch x)"}, ["/opt/a b", "'quoted' ; rm -rf ~"])

    assert argv == [
        "/usr/bin/env",
        "-i",
        "A=$(touch x)",
        "B=two words",
        "/opt/a b",
        "'quoted' ; rm -rf ~",
    ]


def test_a_command_that_starts_with_an_assignment_is_refused() -> None:
    with pytest.raises(BellhopError) as raised:
        scrubbed({}, ["X=1", "/bin/claude"])

    assert raised.value.code == INVALID_CONFIG


def test_claude_gets_its_session_id_the_relay_config_and_the_prompt_after_dashes() -> None:
    start = plan_start(
        CLAUDE, "[bellhop bh_1 from x] hi", runtime_dir=Path, python="py", new_id=lambda: SID
    )

    assert (start.argv, start.session_id, start.socket) == (
        [
            "/opt/fleet/claude",
            "--session-id",
            SID,
            "--mcp-config",
            str(claude_mcp_config()),
            "--",
            "[bellhop bh_1 from x] hi",
        ],
        SID,
        None,
    )
    assert claude_mcp_config().is_file()


def test_pi_gets_its_session_id_and_the_prompt() -> None:
    start = plan_start(PI, None, runtime_dir=Path, python="py", new_id=lambda: SID)

    assert start.argv == ["/opt/fleet/pi", "--session-id", SID]


def test_codex_runs_its_own_app_server_and_tui_under_the_session_wrapper() -> None:
    start = plan_start(
        CODEX,
        "[bellhop bh_1 from x] hi",
        runtime_dir=lambda: Path("/rt"),
        python="/py",
        token=lambda: "abcd",
        new_id=lambda: SID,
    )
    socket = Path("/rt/codex-nanna-abcd.sock")

    assert start.argv[:5] == ["/py", "-I", "-m", "bellhop", "_codex_session"]
    plan = parse(start.argv[5:])
    assert plan is not None
    assert (plan.socket, plan.server, plan.tui, start.socket, start.session_id) == (
        socket,
        ["/opt/fleet/codex", "app-server", "--listen", f"unix://{socket}"],
        ["/opt/fleet/codex", "--remote", f"unix://{socket}", "[bellhop bh_1 from x] hi"],
        socket,
        None,
    )


def test_codex_without_a_prompt_cannot_be_launched() -> None:
    with pytest.raises(BellhopError) as raised:
        plan_start(CODEX, None, runtime_dir=Path, python="py", new_id=lambda: SID)

    assert raised.value.code == USAGE


@pytest.mark.parametrize(
    ("binding", "retry", "server_alive", "decision"),
    [
        (None, False, False, "start"),
        (starting(state="live"), False, False, "send"),
        (starting(), False, False, "queue"),
        (starting(state="failed", detail=f"{START_FAILED}: x"), False, False, "fail"),
        (starting(state="failed", detail=f"{START_FAILED}: x"), True, False, "start"),
        (starting(state="failed", detail="process exited"), False, False, "start"),
        (
            starting(harness="codex", state="failed", detail="app server exited"),
            False,
            True,
            "send",
        ),
        (
            starting(harness="codex", state="failed", detail="app server exited"),
            False,
            False,
            "start",
        ),
    ],
)
def test_what_a_wake_does_follows_the_agents_binding(
    binding: Binding | None, retry: bool, server_alive: bool, decision: str
) -> None:
    assert decide(binding, retry=retry, server_alive=server_alive, now=NOW) == decision


def test_a_start_is_ready_once_hls_lists_it_and_its_route_answers() -> None:
    progress = check_start(starting(), look(), NOW)

    assert (progress.state, progress.binding.state, progress.binding.pid) == ("ready", "live", 5151)
    assert progress.binding.process_started_at == STARTED


def test_a_codex_start_takes_its_thread_from_the_app_server_and_keeps_no_pid() -> None:
    binding = starting(harness="codex", session_id=None, app_server_socket="/rt/c.sock")
    thread = make_session("thread-1", harness="codex", pid=6161)

    progress = check_start(
        binding,
        look("› \n? for shortcuts\nContext 1% used\n", sessions=[thread], thread="thread-1"),
        NOW,
    )

    assert progress.binding == replace(
        binding,
        state="live",
        session_id="thread-1",
        app_server_pid=6161,
        process_started_at=STARTED,
    )


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"sessions": []}, f"waiting for hooklinesinker to list {SID}"),
        ({"reachable": lambda session, binding: False}, "waiting for its route to answer"),
        (
            {"sessions": [], "pane": "Do you want to proceed?\n"},
            "a permission prompt is open",
        ),
    ],
)
def test_a_start_waits_while_it_is_not_listed_or_not_reachable(
    changes: dict[str, object], detail: str
) -> None:
    progress = check_start(starting(), look(**changes), NOW)  # type: ignore[arg-type]

    assert (progress.state, progress.detail, progress.binding) == ("waiting", detail, starting())


def test_a_codex_start_waits_for_its_thread() -> None:
    binding = starting(harness="codex", session_id=None)

    progress = check_start(binding, look(sessions=[]), NOW)

    assert progress.detail == "waiting for the codex app server to open the thread"


@pytest.mark.parametrize(
    ("pane", "detail"),
    [
        (CLAUDE_TRUST, f"stopped at the trust-folder screen; attach with: {HINT}"),
        (
            "Select login method:\n",
            f"stopped at a login screen (Select login method); attach with: {HINT}",
        ),
        (None, "the agent's tmux pane closed before it was ready"),
    ],
)
def test_a_start_that_stops_at_a_screen_fails_at_once_with_how_to_attach(
    pane: str | None, detail: str
) -> None:
    progress = check_start(starting(), look(pane, sessions=[]), NOW)

    assert (progress.state, progress.binding.state, progress.binding.detail) == (
        "failed",
        "failed",
        f"{START_FAILED}: {detail}",
    )


def test_a_start_not_ready_after_90_seconds_fails_with_how_to_attach() -> None:
    progress = check_start(starting(), look(sessions=[]), NOW + timedelta(seconds=91))

    assert progress.binding.detail == (
        f"{START_FAILED}: not ready after 90s (waiting for hooklinesinker to list {SID}); "
        f"attach with: {HINT}"
    )


def test_the_checker_reads_the_pane_of_the_agents_tmux_session(
    tmp_path: Path, fake_tmux: FakeTmux
) -> None:
    conn = connect(tmp_path / "s.sqlite")
    fake_tmux.show(CLAUDE_TRUST)
    check = start_checker(
        conn, lambda s, b: FakeRoute(), lambda socket: Tmux(socket, fake_tmux.executable)
    )

    progress = check(starting(tmux_target="-L bellhop-test enki:%0"), [], NOW)
    conn.close()

    assert progress.binding.detail == (
        f"{START_FAILED}: stopped at the trust-folder screen; attach with: "
        "tmux -L bellhop-test attach -t =enki"
    )
    assert fake_tmux.calls()[-1] == ["-L", "bellhop-test", "capture-pane", "-p", "-J", "-t", "%0"]


def test_the_launched_codex_thread_is_the_one_whose_preview_is_the_prompt(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    previews = {
        "t-extra": "",
        "t-other": "[bellhop bh_999 from x] no",
        "t-ours": "[bellhop bh_1 from x] hi",
    }

    with app_server(path, loaded=list(previews), previews=previews):
        found = find_thread(path, "bh_1")

    assert found == "t-ours"


def test_an_app_server_that_does_not_answer_has_no_thread_yet(short_tmp: Path) -> None:
    assert find_thread(short_tmp / "missing.sock", "bh_1") is None


def test_the_checker_finds_a_codex_thread_through_its_app_server(
    short_tmp: Path, tmp_path: Path, fake_tmux: FakeTmux
) -> None:
    conn = connect(tmp_path / "s.sqlite")
    socket = short_tmp / "app.sock"
    binding = starting(harness="codex", session_id=None, app_server_socket=str(socket))
    begin_start(
        conn, binding, make_message(1, target_kind="agent", target="enki", route="launch"), NOW
    )
    fake_tmux.show("› Ask Codex\n? for shortcuts\nContext 1% used\n")
    check = start_checker(
        conn, lambda s, b: FakeRoute(), lambda socket: Tmux(socket, fake_tmux.executable)
    )
    thread = make_session("t-ours", harness="codex", pid=6161)

    with app_server(
        socket, loaded=["t-ours"], previews={"t-ours": "[bellhop bh_000000000001 from x] hi"}
    ):
        progress = check(binding, [thread], NOW)
    conn.close()

    assert (progress.state, progress.binding.session_id, progress.binding.app_server_pid) == (
        "ready",
        "t-ours",
        6161,
    )


@pytest.mark.parametrize(("after", "state"), [(10, "waiting"), (91, "failed")])
def test_a_start_whose_tmux_session_is_not_recorded_yet_waits_then_fails(
    tmp_path: Path, after: int, state: str
) -> None:
    conn = connect(tmp_path / "s.sqlite")
    check = start_checker(conn, lambda s, b: FakeRoute(), Tmux)

    progress = check(starting(tmux_target=None), [], NOW + timedelta(seconds=after))
    conn.close()

    assert progress.state == state


def test_a_route_that_cannot_be_checked_is_not_reachable() -> None:
    class Unsafe(FakeRoute):
        def reachable(self, session: Session) -> bool:
            raise BellhopError("runtime directory is unsafe", code="runtime_dir_unsafe")

    assert reachable(lambda s, b: Unsafe(), make_session(SID), starting()) is False


def test_a_start_still_waits_just_inside_its_90_seconds() -> None:
    progress = check_start(starting(), look(sessions=[]), NOW + timedelta(seconds=89))

    assert progress.state == "waiting"


def test_a_listed_and_reachable_start_is_ready_whatever_its_first_prompt_says() -> None:
    pane = "❯ [bellhop bh_1 from x] the deploy said: Please run /login and Invalid API key\n"

    assert check_start(starting(), look(pane), NOW).state == "ready"


def test_a_start_left_starting_past_its_deadline_can_be_started_again_by_launch() -> None:
    stale = NOW + STARTUP + timedelta(seconds=31)

    assert (
        decide(starting(), retry=True, server_alive=False, now=stale),
        decide(starting(), retry=False, server_alive=False, now=stale),
        decide(starting(), retry=True, server_alive=False, now=NOW + STARTUP),
    ) == ("start", "queue", "queue")


@pytest.mark.parametrize(("after", "state"), [(10, "waiting"), (91, "failed")])
def test_a_tmux_that_does_not_answer_waits_then_fails_the_start(
    tmp_path: Path, after: int, state: str
) -> None:
    conn = connect(tmp_path / "s.sqlite")
    broken = Tmux("bellhop-test", str(tmp_path / "no-tmux"))
    check = start_checker(conn, lambda s, b: FakeRoute(), lambda socket: broken)

    progress = check(starting(), [], NOW + timedelta(seconds=after))
    conn.close()

    assert (progress.state, "tmux did not run" in progress.detail) == (state, True)


def test_a_codex_server_is_alive_only_while_its_recorded_process_runs() -> None:
    binding = starting(
        harness="codex", state="failed", app_server_pid=6161, process_started_at=STARTED
    )
    running = {6161: Proc(6161, 1, STARTED)}

    assert (
        server_alive(binding, running),
        server_alive(binding, {}),
        server_alive(replace(binding, app_server_pid=None), running),
    ) == (True, False, False)


def test_a_listed_start_waiting_for_its_route_is_never_failed_by_its_pane_text() -> None:
    pane = "❯ [bellhop bh_1 from x] the deploy said: Please run /login\n"

    progress = check_start(starting(), look(pane, reachable=lambda s, b: False), NOW)

    assert (progress.state, progress.detail) == ("waiting", "waiting for its route to answer")


def test_a_start_setup_keeps_the_callers_environment_out_of_its_repr() -> None:
    setup = StartSetup({"GH_TOKEN": "secret-value"}, Tmux(), Path, "py", str)

    assert "secret-value" not in repr(setup)


@pytest.mark.parametrize(
    ("configured", "target"),
    [
        ("", "-S {tmp}/foreign.sock enki:%0"),
        ("fleet", "-L other enki:%0"),
        ("", "-L other enki:%0"),
    ],
)
def test_the_drainer_never_contacts_a_tmux_server_a_starting_row_chooses(
    tmp_path: Path, fake_tmux: FakeTmux, configured: str, target: str
) -> None:
    conn = connect(tmp_path / "s.sqlite")
    env = {"PATH": str(Path(fake_tmux.executable).parent), "TMUX_TMPDIR": str(tmp_path)}
    config = Config(Path("/c/config.toml"), tmux_socket=configured)
    check = drainer_starts(conn, lambda s, b: FakeRoute(), env, lambda: config)

    progress = check(starting(tmux_target=target.format(tmp=tmp_path)), [], NOW)
    conn.close()

    assert progress.state == "failed"
    assert progress.detail.endswith("is not the configured one")
    assert fake_tmux.calls() == []


@pytest.mark.parametrize("target", ["-L fleet enki:%0", "-S {tmp}/tmux-{uid}/fleet enki:%0"])
def test_the_drainer_checks_a_start_on_the_configured_server_however_it_is_spelled(
    tmp_path: Path, fake_tmux: FakeTmux, target: str
) -> None:
    conn = connect(tmp_path / "s.sqlite")
    fake_tmux.show(CLAUDE_TRUST)
    env = {"PATH": str(Path(fake_tmux.executable).parent), "TMUX_TMPDIR": str(tmp_path)}
    config = Config(Path("/c/config.toml"), tmux_socket="fleet")
    check = drainer_starts(conn, lambda s, b: FakeRoute(), env, lambda: config)

    progress = check(starting(tmux_target=target.format(tmp=tmp_path, uid=os.getuid())), [], NOW)
    conn.close()

    assert progress.detail.startswith(f"{START_FAILED}: stopped at the trust-folder screen")


def test_without_tmux_on_path_the_drainer_tries_the_system_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = tmp_path / "usr-bin-tmux"
    system.write_text("#!/bin/sh\n")
    system.chmod(0o755)
    monkeypatch.setattr("bellhop.tmux.SYSTEM_TMUX", (str(tmp_path / "absent"), str(system)))

    assert tmux_executable({"PATH": str(tmp_path / "empty")}) == str(system)


def test_tmux_on_path_wins_over_the_system_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_path = tmp_path / "bin" / "tmux"
    on_path.parent.mkdir()
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(0o755)
    system = tmp_path / "usr-bin-tmux"
    system.write_text("#!/bin/sh\n")
    system.chmod(0o755)
    monkeypatch.setattr("bellhop.tmux.SYSTEM_TMUX", (str(system),))

    assert tmux_executable({"PATH": str(on_path.parent)}) == str(on_path)
