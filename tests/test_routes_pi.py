import json
import socket
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from bellhop.models import PushResult
from bellhop.routes.pi import PiRoute
from conftest import make_session

type Handler = Callable[[socket.socket], bytes]


@pytest.fixture
def runtime(short_tmp: Path) -> Path:
    path = short_tmp / "rt"
    path.mkdir(mode=0o700)
    return path


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
            with conn:
                data = b""
                while chunk := conn.recv(4096):
                    data += chunk
                received.append(data)
                conn.sendall(handler(conn))

    threading.Thread(target=run, daemon=True).start()
    return server


def pong(session: str | None, wire: int = 2) -> Handler:
    reply = json.dumps({"wire": wire, "session": session, "transcript": "/t/p1.jsonl"})
    return lambda conn: f"ok pong {reply}\n".encode()


def test_a_pi_session_is_reached_at_its_pid_socket_and_ping_delivers_nothing(
    runtime: Path,
) -> None:
    received: list[bytes] = []
    server = serve(runtime / "pi-4242.sock", pong(None), received)
    route = PiRoute(lambda: runtime)
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
        result = PiRoute(lambda: runtime).push(
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
    route = PiRoute(lambda: runtime)

    assert route.reachable(session) is False
    assert route.push(session, "x", interrupt=False).outcome == "busy"


@pytest.mark.parametrize(
    ("handler", "reachable"),
    [
        (pong("p1"), True),
        (pong("p-other"), False),
        (pong("p1", wire=1), False),
        (lambda conn: b"ok pong\n", False),
    ],
    ids=["its session", "another session", "older wire", "wire 1 extension"],
)
def test_a_pi_socket_serves_only_its_current_session_on_wire_2(
    runtime: Path, handler: Handler, reachable: bool
) -> None:
    server = serve(runtime / "pi-4242.sock", handler, [])
    try:
        assert (
            PiRoute(lambda: runtime).reachable(make_session("p1", harness="pi", pid=4242))
            is reachable
        )
    finally:
        server.close()


def test_the_route_reports_the_transcript_the_extension_names(runtime: Path) -> None:
    server = serve(runtime / "pi-4242.sock", pong("p1"), [])
    route = PiRoute(lambda: runtime)
    try:
        here = route.transcript(make_session("p1", harness="pi", pid=4242))
        elsewhere = route.transcript(make_session("p9", harness="pi", pid=4242))
    finally:
        server.close()

    assert (here, elsewhere) == (Path("/t/p1.jsonl"), None)


def test_a_mismatch_reply_leaves_the_message_waiting(runtime: Path) -> None:
    server = serve(runtime / "pi-4242.sock", lambda conn: b"error session_mismatch p2\n", [])
    try:
        result = PiRoute(lambda: runtime).push(
            make_session("p1", harness="pi", pid=4242), "x", interrupt=False
        )
    finally:
        server.close()

    assert result == PushResult("busy", "session_mismatch p2")
