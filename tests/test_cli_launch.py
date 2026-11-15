import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bellhop.cli import main
from bellhop.cli.context import Context
from bellhop.cli.launching import wait_for_start
from bellhop.drain import Deps, State, tick
from bellhop.identity import Proc
from bellhop.launch import (
    START_FAILED,
    Progress,
    agent_lock,
    claude_mcp_config,
    start_checker,
)
from bellhop.models import Binding, Harness
from bellhop.routes.fake import FakeRoute
from bellhop.store import (
    ABANDONED_DETAIL,
    begin_start,
    connect,
    finish_start,
    get_binding,
    get_messages,
    launch_message,
)
from bellhop.tmux import Place, Tmux, TmuxTimeout
from conftest import (
    ALPHA,
    CLAUDE_TRUST,
    NOW,
    STARTED,
    Bell,
    FakeTmux,
    check_golden,
    make_message,
    make_session,
    payload_message,
)

SID = "11111111-2222-4333-8444-555555555555"
CALLER_SECRETS = {
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
    "LANG": "en_US.UTF-8",
    "LC_CTYPE": "UTF-8",
    "TMPDIR": "/tmp/u",
}


AGENTS = """
tmux_socket = "bellhop-test"

[agents.enki]
harness = "claude"
cwd = "{work}"
command = ["/opt/fleet/claude"]
env = {{ CLAUDE_CONFIG_DIR = "/fleet/cc" }}

[agents.ninurta]
harness = "claude"
cwd = "{work}"
command = ["/opt/fleet/claude"]

[agents.nanna]
harness = "codex"
cwd = "{work}"
command = ["/opt/fleet/codex"]
env = {{ CODEX_HOME = "/fleet/codex" }}

[agents.utu]
harness = "pi"
"""


@pytest.fixture
def agents(bell: Bell, fake_tmux: FakeTmux) -> Iterator[Bell]:
    work = bell.tmp / "work"
    work.mkdir()
    config = bell.tmp / "config-home" / "bellhop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(AGENTS.format(work=work))
    yield bell


def binding_row(bell: Bell) -> dict[str, object]:
    _, payload = bell.run("agents")
    row = next(row for row in payload["agents"] if row["agent"] == "enki")
    binding = row["binding"]
    assert isinstance(binding, dict)
    return binding


def stored_binding(bell: Bell) -> Binding | None:
    conn = connect(bell.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    try:
        return get_binding(conn, "enki")
    finally:
        conn.close()


def test_a_wake_starts_a_stopped_agent_in_its_own_tmux_session_and_returns_at_once(
    agents: Bell, fake_tmux: FakeTmux
) -> None:
    began = time.monotonic()
    code, payload = agents.run("send", "enki", "look at $(this)", "--wake", "--from", "x")
    took = time.monotonic() - began

    message = payload_message(payload)
    binding = stored_binding(agents)
    assert binding is not None
    argv = fake_tmux.argv("enki")
    assert (code, message["state"], message["route"], took < 2) == (0, "pending", "launch", True)
    assert argv[argv.index("/opt/fleet/claude") :] == [
        "/opt/fleet/claude",
        "--session-id",
        binding.session_id,
        "--mcp-config",
        str(claude_mcp_config()),
        "--",
        f"[bellhop {message['id']} from x] look at $(this)",
    ]
    assert (binding.state, binding.cause, binding.tmux_target) == (
        "starting",
        "send --wake from x",
        f"-S {fake_tmux.directory / 'socket'} enki:%0",
    )
    check_golden("send-wake", payload)


def test_the_launched_agent_gets_none_of_the_callers_session_or_key_variables(
    agents: Bell, fake_tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = ("HOME", "PATH", "XDG_", "BELLHOP_", "FAKE_")
    for name in [name for name in os.environ if not name.startswith(own)]:
        monkeypatch.delenv(name)
    for name, value in CALLER_SECRETS.items():
        monkeypatch.setenv(name, value)

    agents.run("send", "enki", "hi", "--wake")

    argv = fake_tmux.argv("enki")
    names = {arg.split("=", 1)[0] for arg in argv[2 : argv.index("/opt/fleet/claude")]}
    assert argv[:2] == ["/usr/bin/env", "-i"]
    assert names == {
        "HOME",
        "PATH",
        "LANG",
        "LC_CTYPE",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "BELLHOP_RUNTIME_DIR",
        "BELLHOP_HOOKLINESINKER",
        "TERM",
        "CLAUDE_CONFIG_DIR",
    }
    assert "CLAUDE_CONFIG_DIR=/fleet/cc" in argv
    secrets = set(CALLER_SECRETS) - {"LANG", "LC_CTYPE", "TMPDIR"}
    assert secrets.isdisjoint(fake_tmux.client_env())


def test_a_live_agent_is_not_started_again(
    agents: Bell, fake_tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    agents.run("bind", "enki")

    code, sent = agents.run("send", "enki", "hi", "--wake")
    _, launched = agents.run("launch", "enki", "--prompt", "again")

    assert (code, payload_message(sent)["state"], payload_message(launched)["state"]) == (
        0,
        "sent",
        "sent",
    )
    assert launched["agent"]["binding"]["state"] == "live"
    assert fake_tmux.calls() == []


def test_a_dead_binding_is_replaced_by_a_new_start(
    agents: Bell, fake_tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    agents.run("bind", "enki")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setattr("bellhop.cli.context.process_table", dict)

    _, payload = agents.run("send", "enki", "hi", "--wake")

    assert (payload_message(payload)["route"], fake_tmux.sessions()) == ("launch", ["enki"])
    assert binding_row(agents)["state"] == "starting"


def test_a_failed_start_makes_the_next_wake_fail_fast_and_launch_retries(
    agents: Bell, fake_tmux: FakeTmux
) -> None:
    agents.run("send", "enki", "first", "--wake")
    conn = connect(agents.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    fake_tmux.show(CLAUDE_TRUST)
    check = start_checker(
        conn, lambda s, b: FakeRoute(), lambda socket: Tmux(socket, fake_tmux.executable)
    )
    deps = Deps(
        conn,
        list,
        lambda s, b: None,
        lambda s, b: None,
        dict,
        lambda: NOW,
        lambda s: None,
        check,
    )
    tick(deps, State())
    conn.close()

    code, failed = agents.run("send", "enki", "second", "--wake")
    retry_code, retried = agents.run("launch", "enki", "--no-wait", "--cause", "ci failed")

    socket = fake_tmux.directory / "socket"
    detail = (
        f"{START_FAILED}: stopped at the trust-folder screen; attach with: "
        f"tmux -S {socket} attach -t =enki"
    )
    assert (code, failed["error"]["code"]) == (1, "launch_failed")
    assert failed["error"]["message"] == (
        f"cannot wake 'enki': {detail}; run `bellhop launch enki` to retry"
    )
    assert (
        retry_code,
        retried["agent"]["binding"]["state"],
        retried["agent"]["binding"]["cause"],
    ) == (
        0,
        "starting",
        "ci failed",
    )
    assert fake_tmux.sessions() == ["enki", "enki-2"]
    assert [(m["state"], m["detail"]) for m in agents.stored()] == [
        ("sent", f"outcome unknown; {detail}"),
        ("failed", detail),
    ]
    check_golden("send-wake-failed", failed, {str(socket): "<tmux-socket>"})
    check_golden(
        "launch-started", retried, {retried["agent"]["binding"]["session_id"]: "<session-id>"}
    )


def test_a_tmux_that_refuses_the_session_fails_the_launch_and_its_message(
    agents: Bell, fake_tmux: FakeTmux
) -> None:
    fake_tmux.refuse()

    code, payload = agents.run("send", "enki", "hi", "--wake")

    assert (code, payload["error"]["code"], payload_message(payload)["state"]) == (
        1,
        "launch_failed",
        "failed",
    )
    assert binding_row(agents)["detail"] == (
        f"{START_FAILED}: tmux new-session failed: fake: server refused"
    )


def test_three_close_wakes_make_one_start_and_deliver_in_order(
    agents: Bell, fake_tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", None)
    threads = [
        threading.Thread(target=main, args=(["send", "enki", f"m{n}", "--wake", "--json"],))
        for n in range(3)
    ]
    with agent_lock(os.environ, "enki"):
        for thread in threads:
            thread.start()
        time.sleep(1.0)
        waiting = [thread.is_alive() for thread in threads]
    for thread in threads:
        thread.join(30)
    agents.capsys.readouterr()
    assert waiting == [True, True, True]

    conn = connect(agents.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    route = FakeRoute()
    session = make_session(SID)

    def ready(binding: Binding, sessions: object, now: object) -> Progress:
        return Progress(
            "ready",
            "",
            replace(binding, state="live", session_id=SID, pid=4242, process_started_at=STARTED),
        )

    deps = Deps(
        conn,
        lambda: [session],
        lambda s, b: route,
        lambda s, b: None,
        lambda: {4242: Proc(4242, 1, STARTED)},
        lambda: NOW,
        lambda s: None,
        ready,
    )
    state = State()
    tick(deps, state)
    deps = replace(
        deps,
        now=lambda: NOW + timedelta(seconds=5),
        sessions=lambda: [replace(session, observed_at=NOW + timedelta(seconds=5))],
    )
    tick(deps, state)
    stored = get_messages(conn, [str(m["id"]) for m in agents.stored()])
    conn.close()

    assert fake_tmux.sessions() == ["enki"]
    assert fake_tmux.commands().count("new-session") == 1
    assert [(m.route, m.state) for m in stored] == [
        ("launch", "sent"),
        ("fake", "sent"),
        ("fake", "sent"),
    ]
    assert [text.split("] ", 1)[1] for _, text, _ in route.pushes] == [m.body for m in stored[1:]]


def test_launch_waits_until_the_drainer_has_the_agent_live(
    agents: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bellhop.cli.launching.LAUNCH_POLL_SECONDS", 0.02)
    database = agents.tmp / "state-home" / "bellhop" / "bellhop.sqlite"

    def drainer() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if database.exists():
                conn = connect(database)
                binding = get_binding(conn, "enki")
                if binding is not None and binding.tmux_target is not None:
                    live = replace(binding, state="live", pid=4242, process_started_at=STARTED)
                    finish_start(conn, binding, live, NOW)
                    conn.close()
                    return
                conn.close()
            time.sleep(0.01)

    helper = threading.Thread(target=drainer)
    helper.start()
    code, payload = agents.run("launch", "enki", "--prompt", "hello")
    helper.join(10)

    session_id = payload["agent"]["binding"]["session_id"]
    assert (code, payload["agent"]["binding"]["state"], payload_message(payload)["state"]) == (
        0,
        "live",
        "sent",
    )
    check_golden("launch-live", payload, {session_id: "<session-id>"})


def test_launch_refuses_an_agent_that_is_not_configured(agents: Bell) -> None:
    code, payload = agents.run("launch", "nobody")

    assert (code, payload["error"]["code"]) == (3, "target_not_found")


def test_a_failed_wait_reports_launch_failed_with_the_message(
    agents: Bell, monkeypatch: pytest.MonkeyPatch, fake_tmux: FakeTmux
) -> None:
    fake_tmux.refuse()

    code, payload = agents.run("launch", "enki", "--prompt", "hello")

    assert (code, payload["error"]["code"], payload_message(payload)["state"]) == (
        1,
        "launch_failed",
        "failed",
    )
    check_golden("launch-failed", payload)


def test_a_wake_waits_for_the_agents_lock(agents: Bell, fake_tmux: FakeTmux) -> None:
    with agent_lock(os.environ, "enki"):
        waker = threading.Thread(target=main, args=(["send", "enki", "hi", "--wake", "--json"],))
        waker.start()
        waker.join(1.0)
        during = (waker.is_alive(), fake_tmux.sessions())
    waker.join(30)
    agents.capsys.readouterr()

    assert (during, fake_tmux.sessions()) == ((True, []), ["enki"])


def test_a_held_message_never_starts_the_agent(agents: Bell, fake_tmux: FakeTmux) -> None:
    code, payload = agents.run("send", "enki", "later", "--urgency", "hold", "--wake")

    assert (code, payload_message(payload)["urgency"], payload_message(payload)["route"]) == (
        0,
        "hold",
        "none",
    )
    assert fake_tmux.calls() == []


def test_a_codex_wake_runs_the_session_wrapper_and_records_its_app_server(
    agents: Bell, fake_tmux: FakeTmux
) -> None:
    _, payload = agents.run("send", "nanna", "hi", "--wake", "--from", "x")

    argv = fake_tmux.argv("nanna")
    wrapper = argv.index(sys.executable)
    conn = connect(agents.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    binding = get_binding(conn, "nanna")
    conn.close()
    assert binding is not None
    socket = str(binding.app_server_socket)
    assert argv[wrapper : wrapper + 6] == [
        sys.executable,
        "-I",
        "-m",
        "bellhop",
        "_codex_session",
        socket,
    ]
    assert argv[-4:] == [
        "/opt/fleet/codex",
        "--remote",
        f"unix://{socket}",
        f"[bellhop {payload_message(payload)['id']} from x] hi",
    ]
    assert "CODEX_HOME=/fleet/codex" in argv


def test_launching_an_agent_without_a_command_is_a_config_error(agents: Bell) -> None:
    code, payload = agents.run("launch", "utu")

    assert (code, payload["error"]["code"]) == (2, "invalid_config")


def test_launching_an_unknown_agent_suggests_close_names(agents: Bell) -> None:
    _, payload = agents.run("launch", "enkii")

    assert payload["error"]["candidates"] == ["enki"]


def test_launch_prints_where_the_agent_runs(
    agents: Bell, fake_tmux: FakeTmux, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["launch", "enki", "--no-wait", "--prompt", "hi", "--from", "ops"])

    first, second = capsys.readouterr().out.splitlines()
    assert (
        code,
        first.split()[:2],
        first.endswith(f"(tmux -S {fake_tmux.directory / 'socket'} enki:%0)"),
    ) == (
        0,
        ["enki", "starting"],
        True,
    )
    assert second.startswith("bh_") and second.endswith(" pending -> enki")


def test_a_launch_prompt_takes_its_sender_and_key_and_a_repeat_starts_nothing(
    agents: Bell, fake_tmux: FakeTmux
) -> None:
    _, first = agents.run(
        "launch", "enki", "--no-wait", "--prompt", "hi", "--from", "ops", "--key", "k1"
    )
    _, again = agents.run(
        "launch", "enki", "--no-wait", "--prompt", "hi", "--from", "ops", "--key", "k1"
    )

    assert (payload_message(first)["sender"], payload_message(again)["id"]) == (
        "ops",
        payload_message(first)["id"],
    )
    assert fake_tmux.commands().count("new-session") == 1
    assert again["warnings"] == []


def test_a_launch_key_reused_for_another_prompt_says_so(agents: Bell) -> None:
    _, first = agents.run("launch", "enki", "--no-wait", "--prompt", "hi", "--key", "k1")

    _, again = agents.run("launch", "enki", "--no-wait", "--prompt", "bye", "--key", "k1")

    stored_id = payload_message(first)["id"]
    assert again["warnings"] == [
        f"key 'k1' is already stored for {stored_id} with another target or text"
    ]


def test_a_wake_that_starts_its_agent_warns_that_confirm_does_not_wait(agents: Bell) -> None:
    _, payload = agents.run("send", "enki", "hi", "--wake", "--confirm", "5")

    assert payload["warnings"] == ["--confirm does not wait for a message that starts its agent"]


def test_every_start_kicks_the_drainer_even_after_the_command_already_did(
    agents: Bell, drainer_starts: list[str]
) -> None:
    agents.run("send", "enki", "first", "--wake")
    agents.run("send", "ninurta", "second", "--wake")

    assert len(drainer_starts) == 3


def test_waiting_for_a_start_gives_up_with_a_warning(agents: Bell) -> None:
    agents.run("launch", "enki", "--no-wait")
    ctx = Context(dict(os.environ))
    ticks = iter(range(0, 1000, 10))
    try:
        binding = wait_for_start(
            ctx, "enki", seconds=30, clock=lambda: float(next(ticks)), sleep=lambda s: None
        )
    finally:
        ctx.close()

    assert (binding is not None and binding.state, ctx.warnings) == (
        "starting",
        ["'enki' is still starting after 30s"],
    )


def test_a_tmux_client_never_follows_the_callers_tmux(
    agents: Bell, fake_tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")

    agents.run("send", "enki", "hi", "--wake")

    assert ["ENV TMUX"] not in [call[-1:] for call in fake_tmux.calls()]
    assert fake_tmux.sessions() == ["enki"]


def database(bell: Bell) -> sqlite3.Connection:
    return connect(bell.tmp / "state-home" / "bellhop" / "bellhop.sqlite")


def stale_start(bell: Bell, agent: str, harness: Harness, *, opened: bool) -> str:
    conn = database(bell)
    old = datetime.now(UTC) - timedelta(minutes=5)
    target = f"-L bellhop-test {agent}:%0" if opened else None
    binding = Binding(
        agent, harness, "starting", None, None, None, None, None, target, "launch", None, old
    )
    message = make_message(77, target_kind="agent", target=agent, route="launch", created_at=old)
    begin_start(conn, binding, message, old)
    if opened:
        conn.execute(
            "UPDATE messages SET detail = 'prompt passed to tmux' WHERE id = ?", (message.id,)
        )
        conn.commit()
    conn.close()
    return message.id


@pytest.mark.parametrize(("agent", "harness"), [("enki", "claude"), ("nanna", "codex")])
@pytest.mark.parametrize(
    ("opened", "state", "detail"),
    [
        (True, "sent", f"outcome unknown; {ABANDONED_DETAIL}"),
        (False, "failed", ABANDONED_DETAIL),
    ],
)
def test_a_launch_over_a_stale_start_settles_the_old_prompt_and_keeps_one_new_one(
    agents: Bell, agent: str, harness: Harness, opened: bool, state: str, detail: str
) -> None:
    old = stale_start(agents, agent, harness, opened=opened)

    _, payload = agents.run("launch", agent, "--prompt", "again", "--no-wait")

    conn = database(agents)
    [before] = get_messages(conn, [old])
    current = launch_message(conn, agent)
    conn.close()
    assert (before.state, before.detail) == (state, detail)
    assert current is not None and current.id == payload_message(payload)["id"]


@pytest.mark.parametrize(
    ("agent_config", "reason"),
    [
        (
            'cwd = "/no/such/dir"\ncommand = ["/opt/fleet/claude"]',
            "its cwd /no/such/dir is not a directory",
        ),
        ('command = ["/opt/fleet/claude"]', "there is no tmux on PATH or at the system locations"),
    ],
)
def test_a_wake_that_cannot_start_stores_its_message_and_leaves_the_binding(
    bell: Bell, monkeypatch: pytest.MonkeyPatch, agent_config: str, reason: str
) -> None:
    config = bell.tmp / "config-home" / "bellhop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[agents.enki]\nharness = "claude"\n{agent_config}\n')
    monkeypatch.setattr("bellhop.launch.tmux_executable", lambda env: None)

    code, payload = bell.run("send", "enki", "hi", "--wake")
    launch_code, launched = bell.run("launch", "enki")

    assert (code, payload_message(payload)["state"], payload["warnings"]) == (
        0,
        "pending",
        [f"cannot launch 'enki': {reason}; the message waits"],
    )
    assert (launch_code, launched["error"]["code"]) == (2, "invalid_config")
    _, listed = bell.run("agents")
    assert [row["binding"] for row in listed["agents"]] == [None]
    if "cwd" in agent_config:
        check_golden("send-wake-cannot-launch", payload)


def test_a_body_ending_in_a_semicolon_reaches_the_agent_whole(
    agents: Bell, fake_tmux: FakeTmux
) -> None:
    _, payload = agents.run("send", "enki", "run make;", "--wake", "--from", "x")

    assert (
        fake_tmux.argv("enki")[-1] == f"[bellhop {payload_message(payload)['id']} from x] run make;"
    )


@pytest.mark.parametrize(("early", "state"), [(False, "starting"), (True, "failed")])
def test_a_new_session_that_times_out_is_looked_up_before_it_is_called_failed(
    agents: Bell, fake_tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch, early: bool, state: str
) -> None:
    monkeypatch.setattr("bellhop.tmux.TMUX_SECONDS", 0.5)
    if early:
        fake_tmux.hang_before_starting()
    else:
        fake_tmux.hang()

    _, payload = agents.run("send", "enki", "hi", "--wake")

    binding = stored_binding(agents)
    assert binding is not None
    assert binding.state == state
    if early:
        assert (payload["error"]["code"], payload_message(payload)["state"]) == (
            "launch_failed",
            "sent",
        )
        assert "attach with: tmux -L bellhop-test attach -t =enki" in str(binding.detail)
    else:
        assert binding.tmux_target == f"-S {fake_tmux.directory / 'socket'} enki:%0"


def test_bind_waits_for_a_start_and_refuses_while_it_runs(
    agents: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents.run("send", "enki", "hi", "--wake")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)

    code, payload = agents.run("bind", "enki")

    assert (code, payload["error"]["code"], payload["error"]["message"]) == (
        1,
        "binding_conflict",
        "'enki' is starting; wait for it to go live, or run `bellhop launch enki`",
    )


def test_a_repeated_launch_key_answers_in_launch_s_shape(agents: Bell) -> None:
    agents.run("launch", "enki", "--no-wait", "--prompt", "hi", "--key", "k9")

    _, again = agents.run("launch", "enki", "--no-wait", "--prompt", "hi", "--key", "k9")

    assert (again["agent"]["agent"], again["agent"]["binding"]["state"]) == ("enki", "starting")


def test_a_list_sessions_timeout_stores_the_wake_without_touching_the_binding(
    agents: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hung(self: Tmux) -> set[str]:
        raise TmuxTimeout("tmux list-sessions gave no answer within 5s", code="launch_failed")

    monkeypatch.setattr(Tmux, "session_names", hung)

    _, payload = agents.run("send", "enki", "hi", "--wake")

    assert (payload_message(payload)["state"], stored_binding(agents), payload["warnings"]) == (
        "pending",
        None,
        ["cannot launch 'enki': tmux list-sessions gave no answer within 5s; the message waits"],
    )


def test_a_new_session_timeout_whose_lookup_also_fails_is_outcome_unknown(
    agents: Bell, fake_tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bellhop.tmux.TMUX_SECONDS", 0.5)
    fake_tmux.hang()

    def unanswered(self: Tmux, name: str) -> Place | None:
        raise TmuxTimeout("tmux display-message gave no answer within 0.5s", code="launch_failed")

    monkeypatch.setattr(Tmux, "locate", unanswered)

    _, payload = agents.run("send", "enki", "hi", "--wake")

    binding = stored_binding(agents)
    assert binding is not None
    assert (payload_message(payload)["state"], binding.state) == ("sent", "failed")
    assert "attach with: tmux -L bellhop-test attach -t =enki" in str(binding.detail)


def test_launching_a_live_agent_sends_whatever_its_start_settings_say(
    bell: Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bell.tmp / "config-home" / "bellhop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[agents.enki]\nharness = "claude"\ncwd = "/no/such/dir"\ncommand = ["/x"]\n')
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    bell.run("bind", "enki")

    code, payload = bell.run("launch", "enki", "--prompt", "hi")

    assert (code, payload_message(payload)["state"]) == (0, "sent")


@pytest.mark.parametrize("cause", ["runtime", "locale", "names", "tmux"])
def test_a_wake_stopped_before_tmux_opens_a_session_stores_its_message(
    agents: Bell, fake_tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    agent = "nanna" if cause == "runtime" else "enki"
    if cause == "runtime":
        unsafe = agents.tmp / "runtime-file"
        unsafe.write_text("")
        monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(unsafe))
    if cause == "locale":
        monkeypatch.setenv("LC_BAD-NAME", "x")
    if cause == "names":
        for name in ["enki", *(f"enki-{n}" for n in range(2, 10))]:
            fake_tmux.occupy(name)
    if cause == "tmux":
        broken = agents.tmp / "broken-bin"
        broken.mkdir()
        (broken / "tmux").write_text("#!/no/such/interpreter\n")
        (broken / "tmux").chmod(0o755)
        monkeypatch.setenv("PATH", f"{broken}{os.pathsep}{os.environ['PATH']}")

    code, payload = agents.run("send", agent, "hi", "--wake")

    conn = database(agents)
    binding = get_binding(conn, agent)
    conn.close()
    assert (code, payload_message(payload)["state"], binding) == (0, "pending", None)
    assert payload["warnings"][0].startswith(f"cannot launch '{agent}': ")


def test_bind_waits_for_the_agent_lock(agents: Bell, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ALPHA)
    monkeypatch.setattr(sys, "stdin", None)
    binder = threading.Thread(target=main, args=(["bind", "enki", "--json"],))
    with agent_lock(os.environ, "enki"):
        binder.start()
        binder.join(1.0)
        waiting = binder.is_alive()
    binder.join(30)
    agents.capsys.readouterr()

    assert (waiting, binding_row(agents)["session_id"]) == (True, ALPHA)


def test_a_wake_with_confirm_says_it_does_not_wait(agents: Bell) -> None:
    _, payload = agents.run("send", "enki", "hi", "--wake", "--from", "x", "--confirm", "5")

    assert payload["warnings"] == ["--confirm does not wait for a message that starts its agent"]
    check_golden("send-wake-confirm", payload)


def test_a_tmux_that_refuses_the_connection_stores_the_wake(
    agents: Bell, fake_tmux: FakeTmux
) -> None:
    fake_tmux.refuse_connection()

    code, payload = agents.run("send", "enki", "hi", "--wake")

    assert (code, payload_message(payload)["state"], stored_binding(agents)) == (0, "pending", None)
    assert "Operation not permitted" in payload["warnings"][0]


def test_the_tmux_client_runs_with_the_caller_s_umask(agents: Bell, fake_tmux: FakeTmux) -> None:
    previous = os.umask(0o022)
    try:
        agents.run("send", "enki", "hi", "--wake")
    finally:
        os.umask(previous)

    assert fake_tmux.client_umask() == "0022"


def test_a_send_to_an_agent_that_is_starting_says_nothing_about_waking_it(agents: Bell) -> None:
    agents.run("send", "enki", "first", "--wake")

    code, payload = agents.run("send", "enki", "second")

    assert (code, payload_message(payload)["state"], payload["warnings"]) == (0, "pending", [])


def test_a_cause_with_a_control_character_is_usage(agents: Bell, fake_tmux: FakeTmux) -> None:
    code, payload = agents.run("launch", "enki", "--no-wait", "--cause", "x\x1b[2J")

    assert (code, payload["error"]["code"], fake_tmux.commands()) == (2, "usage", [])
