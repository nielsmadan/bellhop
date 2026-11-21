import contextlib
import os
import pwd
import socket
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from bellhop.config import AgentConfig, Config
from bellhop.errors import BellhopError
from bellhop.models import Binding, PushResult, Session
from bellhop.routes import endpoint_routes
from bellhop.routes.codex import CONTROL_SOCKET, CodexRoute
from bellhop.routes.endpoint import Pongs
from bellhop.routes.fake import FakeRoute
from bellhop.transcripts import PlainTranscripts
from bellhop.wiring import (
    TracedReader,
    TracedRoute,
    claude_dir,
    endpoint_transcript,
    fake_routes,
    pi_agent_dir,
    session_homes,
    wire,
)
from conftest import (
    CLAUDE_SESSION,
    NOW,
    accepting_claude,
    claude_peer,
    make_session,
    pong_line,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, Path("/home/me/.pi/agent")),
        ("~", Path("/home/me")),
        ("~/pi-agent", Path("/home/me/pi-agent")),
        ("/opt/pi-agent", Path("/opt/pi-agent")),
        ("relative/agent", None),
    ],
)
def test_pi_agent_dir_follows_pi_and_refuses_a_relative_value(
    value: str | None, expected: Path | None
) -> None:
    env = {"HOME": "/home/me"} | ({"PI_CODING_AGENT_DIR": value} if value else {})

    assert pi_agent_dir(env) == expected


def test_a_runtime_dir_problem_means_no_reported_transcript(tmp_path: Path) -> None:
    def unsafe() -> Path:
        raise BellhopError("runtime directory is unsafe", code="runtime_dir_unsafe")

    reported = endpoint_transcript(endpoint_routes(unsafe, Pongs()))

    assert reported(make_session("p1", harness="pi", pid=4242)) is None


def test_the_route_and_the_reader_ask_an_endpoint_once_between_them(short_tmp: Path) -> None:
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(short_tmp / "claude-4242.sock"))
    server.listen(4)

    def answer() -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = server.accept()
                with conn:
                    received.append(conn.recv(4096))
                    conn.sendall(pong_line("c1", transcript="/t/c1.jsonl"))

    threading.Thread(target=answer, daemon=True).start()
    short_tmp.chmod(0o700)
    wiring = wire({"BELLHOP_RUNTIME_DIR": str(short_tmp)}, lambda: Config(Path("/c")))
    session = make_session("c1", harness="claude", pid=4242, transcript_path=None)
    try:
        route = wiring.routes(session, None)
        reader = wiring.readers(session, None)
        assert route is not None and reader is not None
        found = (route.reachable(session), reader.path(session))
    finally:
        server.close()

    assert found == (True, Path("/t/c1.jsonl"))
    assert received == [b"ping\n"]


def test_a_configured_agents_codex_home_comes_from_its_config_not_the_caller(
    tmp_path: Path,
) -> None:
    agent_home = tmp_path / "agent-codex"
    rollout = agent_home / "sessions" / "2026" / "11" / "14"
    rollout.mkdir(parents=True)
    thread = "01a1d948-f680-7000-8000-000000000001"
    found = rollout / f"rollout-2026-11-14T09-00-00-{thread}.jsonl"
    found.write_text("")
    config = Config(
        Path("/c"),
        agents={"nanna": AgentConfig("nanna", "codex", env={"CODEX_HOME": str(agent_home)})},
    )
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "caller-codex")}
    wiring = wire(env, lambda: config)
    session = make_session(thread, harness="codex", pid=None, transcript_path=None)
    binding = Binding(
        "nanna", "codex", "live", thread, None, None, None, None, None, "bind", None, NOW
    )

    reader = wiring.readers(session, binding)
    route = wiring.routes(session, binding)

    assert reader is not None
    assert reader.path(session) == found
    assert route == CodexRoute(agent_home / CONTROL_SOCKET)


def test_an_unconfigured_targets_codex_home_is_the_callers(tmp_path: Path) -> None:
    wiring = wire({"CODEX_HOME": str(tmp_path / "caller")}, lambda: Config(Path("/c")))
    session = make_session("t1", harness="codex", pid=None)

    assert wiring.routes(session, None) == CodexRoute(tmp_path / "caller" / CONTROL_SOCKET)


def test_a_configured_agent_without_a_codex_home_uses_its_own_home_not_the_callers(
    tmp_path: Path,
) -> None:
    config = Config(Path("/c"), agents={"nanna": AgentConfig("nanna", "codex")})
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "caller-codex")}
    session = make_session("t1", harness="codex", pid=None)
    binding = Binding(
        "nanna", "codex", "live", "t1", None, None, None, None, None, "bind", None, NOW
    )

    route = wire(env, lambda: config).routes(session, binding)

    assert route == CodexRoute(tmp_path / ".codex" / CONTROL_SOCKET)


@pytest.mark.parametrize(("state_home", "honoured"), [(None, False), ("/srv/state", True)])
def test_fake_routes_act_only_on_a_non_default_state_dir(
    state_home: str | None, honoured: bool
) -> None:
    env = {"HOME": "/home/me", "BELLHOP_FAKE_ROUTES": "/t/routes.json"}
    if state_home:
        env["XDG_STATE_HOME"] = state_home

    assert (fake_routes(env) is not None) == honoured


def test_a_session_target_uses_its_own_homes_whatever_the_drainer_was_started_with(
    tmp_path: Path,
) -> None:
    own = tmp_path / "own-codex"
    thread = "01a1d948-f680-7000-8000-000000000001"
    rollout = (
        own / "sessions" / "2026" / "11" / "14" / f"rollout-2026-11-14T09-00-00-{thread}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text("")
    pi_own = tmp_path / "own-pi"
    pi_file = pi_own / "sessions" / "--work-x--" / "s.jsonl"
    pi_file.parent.mkdir(parents=True)
    pi_file.write_text("")
    drainer_env = {
        "HOME": str(tmp_path),
        "CODEX_HOME": str(tmp_path / "drainer-codex"),
        "PI_CODING_AGENT_DIR": str(tmp_path / "drainer-pi"),
    }
    wiring = wire(drainer_env, lambda: Config(Path("/c")))
    codex = make_session(thread, harness="codex", pid=None, transcript_path=rollout)
    pi = make_session("p1", harness="pi", pid=None, transcript_path=pi_file)

    readers = [wiring.readers(session, None) for session in (codex, pi)]

    assert wiring.routes(codex, None) == CodexRoute(own / CONTROL_SOCKET)
    assert session_homes(pi) == {"PI_CODING_AGENT_DIR": str(pi_own)}
    assert [r.path(s) if r else None for r, s in zip(readers, (codex, pi), strict=True)] == [
        rollout,
        pi_file,
    ]


@pytest.mark.parametrize(
    "state_home",
    [
        lambda home, real: f"{home}/.local/state/../state",
        lambda home, real: f"{real}/.local/state",
    ],
)
def test_fake_routes_are_ignored_on_any_spelling_of_the_real_state_dir(
    tmp_path: Path, state_home: Callable[[str, str], str]
) -> None:
    real = pwd.getpwuid(os.getuid()).pw_dir
    env = {
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": state_home(str(tmp_path), real),
        "BELLHOP_FAKE_ROUTES": "/t/routes.json",
    }

    assert fake_routes(env) is None


def test_fake_routes_are_ignored_through_a_link_to_the_real_state_dir(tmp_path: Path) -> None:
    (tmp_path / ".local" / "state").mkdir(parents=True)
    (tmp_path / "linked").symlink_to(tmp_path / ".local" / "state")
    env = {
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / "linked"),
        "BELLHOP_FAKE_ROUTES": "/t/routes.json",
    }

    assert fake_routes(env) is None


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def __call__(self, event: str, /, **fields: object) -> None:
        self.events.append((event, fields))


def test_a_probe_is_recorded_once_until_its_answer_changes() -> None:
    seen = Recorder()
    probed: dict[str, tuple[str, bool, str | None]] = {}
    session = make_session("s1")
    down = TracedRoute(FakeRoute(is_reachable=False), seen, probed)

    answers = [down.reachable(session), down.reachable(session)]
    answers.append(TracedRoute(FakeRoute(), seen, probed).reachable(session))

    assert answers == [False, False, True]
    assert [(fields["reachable"], fields["reason"]) for _, fields in seen.events] == [
        (False, "the fake route is set unreachable"),
        (True, None),
    ]


def test_a_push_is_recorded_with_its_message_and_outcome() -> None:
    seen = Recorder()
    route = TracedRoute(FakeRoute(outcomes=["busy"]), seen, {}, clock=lambda: 5.0)

    result = route.push(make_session("s1"), "[bellhop bh_abc from enki] hi", interrupt=True)

    assert result.outcome == "busy"
    assert seen.events == [
        (
            "push",
            {
                "message": "bh_abc",
                "session": "s1",
                "harness": "claude",
                "route": "fake",
                "interrupt": True,
                "phase": "idle",
                "outcome": "busy",
                "detail": "fake busy",
                "ms": 0,
            },
        )
    ]


def test_a_push_that_raises_is_recorded_and_still_raises() -> None:
    class Broken(FakeRoute):
        def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
            raise OSError("socket gone")

    seen = Recorder()

    with pytest.raises(OSError, match="socket gone"):
        TracedRoute(Broken(), seen, {}).push(make_session("s1"), "x", interrupt=False)

    assert [(event, fields["outcome"], fields["detail"]) for event, fields in seen.events] == [
        ("push", "raised", "OSError: socket gone")
    ]


def test_a_transcript_is_recorded_only_when_it_shows_the_message(tmp_path: Path) -> None:
    seen = Recorder()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("[bellhop bh_abc from enki] hi\n")
    session = make_session("s1", transcript_path=transcript)
    reader = TracedReader(PlainTranscripts(), seen)

    found = [
        reader.contains(session, "[bellhop bh_zzz from", since=0),
        reader.contains(session, "[bellhop bh_abc from", since=0),
    ]

    assert found == [False, True]
    assert seen.events == [
        (
            "seen",
            {
                "message": "bh_abc",
                "session": "s1",
                "harness": "claude",
                "transcript": str(transcript),
            },
        )
    ]
    assert (reader.path(session), reader.size(session)) == (transcript, 30)


def test_wiring_with_a_tracer_records_what_its_routes_do(tmp_path: Path) -> None:
    seen = Recorder()
    routes = tmp_path / "routes.json"
    routes.write_text('{"default": {"reachable": false}}')
    env = {"BELLHOP_FAKE_ROUTES": str(routes), "XDG_STATE_HOME": str(tmp_path / "state")}
    wiring = wire(env, lambda: Config(Path("/c")), seen)
    session = make_session("s1")

    route = wiring.routes(session, None)
    reader = wiring.readers(session, None)

    assert route is not None and reader is not None
    assert (route.name, route.can_interrupt, route.queues_until_idle) == ("fake", True, True)
    assert (route.reachable(session), route.replaced(session), route.busy(session)) == (
        False,
        False,
        None,
    )
    assert route.obstacle(session) == "the fake route is set unreachable"
    assert [event for event, _ in seen.events] == ["probe"]


def test_a_probe_is_recorded_again_when_the_route_that_answers_changes() -> None:
    seen = Recorder()
    probed: dict[str, tuple[str, bool, str | None]] = {}
    session = make_session("s1")

    for name in ("claude-relay", "claude-relay", "claude-socket"):
        TracedRoute(FakeRoute(name=name), seen, probed).reachable(session)

    assert [fields["route"] for _, fields in seen.events] == ["claude-relay", "claude-socket"]


def test_a_claude_sessions_config_directory_comes_from_its_transcript(tmp_path: Path) -> None:
    own = tmp_path / "other" / "projects" / "-work-c1" / "c1.jsonl"
    elsewhere = tmp_path / "other" / "exports" / "-work-c1" / "c1.jsonl"

    assert session_homes(make_session("c1", transcript_path=own)) == {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "other")
    }
    assert session_homes(make_session("c1", transcript_path=elsewhere)) == {}
    assert session_homes(make_session("c1")) == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, Path("/home/me/.claude")), ("/cfg", Path("/cfg")), ("relative/claude", None)],
)
def test_claudes_config_directory_is_the_configured_one_or_the_default(
    value: str | None, expected: Path | None
) -> None:
    env = {"HOME": "/home/me"} | ({"CLAUDE_CONFIG_DIR": value} if value is not None else {})

    assert claude_dir(env) == expected


def test_the_wired_finder_reaches_an_accepting_claude_over_its_socket(
    tmp_path: Path, short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = short_tmp / "rt"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(runtime))
    home, sock = short_tmp / "claude", short_tmp / "4242.sock"
    accepting_claude(home, sock)
    env = {**os.environ, "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(home)}
    wiring = wire(env, lambda: Config(Path("/c")))
    session = make_session(CLAUDE_SESSION)

    with claude_peer(sock):
        route = wiring.routes(session, None)
        assert route is not None
        found = (route.reachable(session), route.name)

    assert found == (True, "claude-socket")


def test_a_configured_agents_claude_directory_comes_from_its_config_not_the_caller(
    tmp_path: Path, short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = short_tmp / "rt"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(runtime))
    home, sock = short_tmp / "agent-claude", short_tmp / "4242.sock"
    accepting_claude(home, sock)
    config = Config(
        Path("/c"),
        agents={"enki": AgentConfig("enki", "claude", env={"CLAUDE_CONFIG_DIR": str(home)})},
    )
    env = {**os.environ, "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "callers")}
    wiring = wire(env, lambda: config)
    session = make_session(CLAUDE_SESSION)
    binding = Binding(
        "enki", "claude", "live", CLAUDE_SESSION, None, None, None, None, None, "bind", None, NOW
    )

    with claude_peer(sock):
        own, callers = wiring.routes(session, binding), wiring.routes(session, None)
        assert own is not None and callers is not None
        found = (own.reachable(session), callers.reachable(session))

    assert found == (True, False)
