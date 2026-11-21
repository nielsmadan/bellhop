import contextlib
import socket
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from bellhop.models import PushResult
from bellhop.routes import endpoint_routes
from bellhop.routes.endpoint import PONG_SECONDS, EndpointRoute, Pongs
from bellhop.routes.pi import NAME
from conftest import make_session, pong_line

type Handler = Callable[[socket.socket], bytes]


@pytest.fixture
def runtime(short_tmp: Path) -> Path:
    path = short_tmp / "rt"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture(autouse=True)
def patient_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bellhop.routes.endpoint.PING_SECONDS", 10.0)


def pi_route(runtime: Path, pongs: Pongs | None = None) -> EndpointRoute:
    return EndpointRoute("pi", NAME, lambda: runtime, pongs or Pongs())


def serve(path: Path, handler: Handler, received: list[bytes]) -> socket.socket:
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)

    def run() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn, contextlib.suppress(OSError):
                data = b""
                while chunk := conn.recv(4096):
                    data += chunk
                received.append(data)
                conn.sendall(handler(conn))

    threading.Thread(target=run, daemon=True).start()
    return server


def pong(session: str | None, wire: int = 2) -> Handler:
    return lambda conn: pong_line(session, wire=wire, transcript="/t/p1.jsonl")


def test_a_pi_session_is_reached_at_its_pid_socket_and_ping_delivers_nothing(
    runtime: Path,
) -> None:
    received: list[bytes] = []
    server = serve(runtime / "pi-4242.sock", pong("p1"), received)
    route = pi_route(runtime)
    try:
        assert route.reachable(make_session("p1", harness="pi", pid=4242)) is True
        assert route.reachable(make_session("p2", harness="pi", pid=5151)) is False
    finally:
        server.close()

    assert received == [b"ping\n"]


@pytest.mark.parametrize(
    ("interrupt", "header"),
    [(False, b"deliver boundary p1 24"), (True, b"deliver interrupt p1 24")],
)
def test_boundary_and_interrupt_map_to_their_headers(
    runtime: Path, interrupt: bool, header: bytes
) -> None:
    received: list[bytes] = []
    server = serve(runtime / "pi-4242.sock", lambda conn: b"ok followUp\n", received)
    try:
        result = pi_route(runtime).push(
            make_session("p1", harness="pi", pid=4242),
            "[bellhop bh_1 from x] hi",
            interrupt=interrupt,
        )
    finally:
        server.close()

    assert result == PushResult("accepted", "followUp")
    assert received == [header + b"\n[bellhop bh_1 from x] hi"]


def test_a_session_without_a_pid_has_no_socket(runtime: Path) -> None:
    session = make_session("p1", harness="pi", pid=None)
    route = pi_route(runtime)

    assert route.reachable(session) is False
    assert route.push(session, "x", interrupt=False).outcome == "busy"


@pytest.mark.parametrize(
    ("handler", "reachable"),
    [
        (pong("p1"), True),
        (pong("p-other"), False),
        (pong(None), False),
        (pong("p1", wire=1), False),
        (lambda conn: b"ok pong\n", False),
    ],
    ids=["its session", "another session", "no session named", "older wire", "wire 1 extension"],
)
def test_a_pi_socket_serves_only_its_current_session_on_wire_2(
    runtime: Path, handler: Handler, reachable: bool
) -> None:
    server = serve(runtime / "pi-4242.sock", handler, [])
    try:
        assert pi_route(runtime).reachable(make_session("p1", harness="pi", pid=4242)) is reachable
    finally:
        server.close()


@pytest.mark.parametrize(
    ("handler", "replaced"),
    [
        (pong("p-new"), True),
        (pong("p1"), False),
        (pong(None), False),
        (pong("p-new", wire=1), False),
        (lambda conn: b"error no\n", False),
    ],
    ids=["another session", "its session", "no session named", "older wire", "no pong"],
)
def test_a_session_is_replaced_only_when_the_extension_names_another(
    runtime: Path, handler: Handler, replaced: bool
) -> None:
    server = serve(runtime / "pi-4242.sock", handler, [])
    try:
        found = pi_route(runtime).replaced(make_session("p1", harness="pi", pid=4242))
    finally:
        server.close()

    assert found is replaced


def test_one_pong_answers_every_question_asked_within_a_second(runtime: Path) -> None:
    received: list[bytes] = []
    clock = [100.0]
    server = serve(runtime / "pi-4242.sock", pong("p-new"), received)
    route = pi_route(runtime, Pongs(clock=lambda: clock[0]))
    session = make_session("p1", harness="pi", pid=4242)
    try:
        first = (route.reachable(session), route.replaced(session), route.transcript(session))
        clock[0] += PONG_SECONDS
        later = route.replaced(session)
    finally:
        server.close()

    assert (first, later) == ((False, True, None), True)
    assert received == [b"ping\n", b"ping\n"]


def test_the_route_reports_the_transcript_the_extension_names(runtime: Path) -> None:
    server = serve(runtime / "pi-4242.sock", pong("p1"), [])
    route = pi_route(runtime)
    try:
        here = route.transcript(make_session("p1", harness="pi", pid=4242))
        elsewhere = route.transcript(make_session("p9", harness="pi", pid=4242))
    finally:
        server.close()

    assert (here, elsewhere) == (Path("/t/p1.jsonl"), None)


def test_a_mismatch_reply_leaves_the_message_waiting(runtime: Path) -> None:
    server = serve(runtime / "pi-4242.sock", lambda conn: b"error session_mismatch p2\n", [])
    try:
        result = pi_route(runtime).push(
            make_session("p1", harness="pi", pid=4242), "x", interrupt=False
        )
    finally:
        server.close()

    assert result == PushResult("busy", "session_mismatch p2")


OLD_WIRE = (
    "the relay at {path} speaks another wire version; "
    "restart that claude so it starts the installed bellhop's relay"
)


def claude_route(runtime: Path) -> EndpointRoute:
    return endpoint_routes(lambda: runtime, Pongs())["claude"]


def test_a_missing_relay_says_where_it_looked_and_how_to_start_one(runtime: Path) -> None:
    session = make_session("c1", harness="claude", pid=4242)

    assert claude_route(runtime).obstacle(session) == (
        f"no bellhop relay listens at {runtime}/claude-4242.sock; "
        "start claude with bellhop's MCP config (`bellhop integration show claude`)"
    )


def test_a_session_without_a_pid_cannot_be_looked_up(runtime: Path) -> None:
    session = make_session("c1", harness="claude", pid=None)

    assert claude_route(runtime).obstacle(session) == (
        "hooklinesinker reports no pid for this claude session"
    )


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (pong("other"), "the relay at {path} serves session other"),
        (pong("c1", wire=1), OLD_WIRE),
        (lambda conn: b"", "the relay at {path} does not answer"),
    ],
)
def test_a_relay_that_cannot_serve_the_session_says_why(
    runtime: Path, handler: Handler, reason: str
) -> None:
    path = runtime / "claude-4242.sock"
    server = serve(path, handler, [])
    try:
        found = claude_route(runtime).obstacle(make_session("c1", harness="claude", pid=4242))
    finally:
        server.close()

    assert found == reason.format(path=path)
