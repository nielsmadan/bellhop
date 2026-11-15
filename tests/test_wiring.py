import contextlib
import socket
import threading
from pathlib import Path

import pytest

from bellhop.config import AgentConfig, Config
from bellhop.errors import BellhopError
from bellhop.models import Binding
from bellhop.routes import endpoint_routes
from bellhop.routes.codex import CONTROL_SOCKET, CodexRoute
from bellhop.routes.endpoint import Pongs
from bellhop.wiring import endpoint_transcript, pi_agent_dir, wire
from conftest import NOW, make_session, pong_line


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
