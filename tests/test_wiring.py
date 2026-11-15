import contextlib
import socket
import threading
from pathlib import Path

import pytest

from bellhop.config import Config
from bellhop.errors import BellhopError
from bellhop.routes import endpoint_routes
from bellhop.routes.endpoint import Pongs
from bellhop.wiring import endpoint_transcript, pi_agent_dir, wire
from conftest import make_session, pong_line


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
