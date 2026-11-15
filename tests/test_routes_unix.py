import contextlib
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from bellhop.errors import BellhopError
from bellhop.models import PushResult
from bellhop.routes.unix import (
    REPLY_LIMIT,
    Pong,
    endpoint_path,
    exchange,
    parse_pong,
    parse_reply,
    ping,
)

type Handler = Callable[[socket.socket], None]


@pytest.fixture
def endpoint(short_tmp: Path) -> Iterator[Callable[[Handler], Path]]:
    servers: list[socket.socket] = []
    path = short_tmp / "e.sock"

    def serve(handler: Handler) -> Path:
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(1)
        servers.append(server)

        def run() -> None:
            conn, _ = server.accept()
            with conn:
                handler(conn)

        threading.Thread(target=run, daemon=True).start()
        return path

    yield serve
    for server in servers:
        server.close()
    path.unlink(missing_ok=True)


def reads(header: list[bytes]) -> Handler:
    def handle(conn: socket.socket) -> None:
        data = b""
        while chunk := conn.recv(4096):
            data += chunk
        header.append(data)
        conn.sendall(b"ok forwarded\n")

    return handle


def test_a_reply_that_trickles_past_the_deadline_is_unknown(
    endpoint: Callable[[Handler], Path],
) -> None:
    def trickle(conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            for _ in range(20):
                conn.sendall(b"o")
                time.sleep(0.1)

    path = endpoint(trickle)
    started = time.monotonic()

    result = exchange(path, "deliver boundary", "x", timeout=0.5)

    assert result == PushResult("unknown", "no reply within 0.5s")
    assert time.monotonic() - started < 1.5


def test_an_endless_reply_line_is_cut_at_the_limit(endpoint: Callable[[Handler], Path]) -> None:
    def flood(conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            conn.sendall(b"ok " + b"y" * 10_000 + b"\n")

    result = exchange(endpoint(flood), "deliver boundary", "x")

    assert (result.outcome, len(result.detail)) == ("accepted", REPLY_LIMIT - 3)


def test_a_reply_line_maps_to_an_outcome() -> None:
    assert parse_reply("ok forwarded via child-env\n") == PushResult(
        "accepted", "forwarded via child-env"
    )
    assert parse_reply("busy compacting") == PushResult("busy", "compacting")
    assert parse_reply("error empty message") == PushResult("rejected", "empty message")
    assert parse_reply("what") == PushResult("unknown", "unrecognized reply: what")


def test_the_wire_is_one_header_line_then_the_envelope(endpoint: Callable[[Handler], Path]) -> None:
    received: list[bytes] = []
    path = endpoint(reads(received))

    result = exchange(path, "deliver boundary", "[bellhop bh_1 from enki] hi")

    assert result == PushResult("accepted", "forwarded")
    assert received == [b"deliver boundary\n[bellhop bh_1 from enki] hi"]


def test_a_missing_or_stale_endpoint_wrote_nothing_and_is_busy(
    tmp_path: Path, short_tmp: Path
) -> None:
    stale = short_tmp / "stale.sock"
    left = socket.socket(socket.AF_UNIX)
    left.bind(str(stale))
    left.close()
    try:
        results = [
            exchange(path, "deliver boundary", "hi") for path in (tmp_path / "missing.sock", stale)
        ]
    finally:
        stale.unlink()

    assert [result.outcome for result in results] == ["busy", "busy"]
    assert results[1].detail == "endpoint not listening: Connection refused"


def test_a_connection_dropped_after_the_write_is_unknown(
    endpoint: Callable[[Handler], Path],
) -> None:
    def drop(conn: socket.socket) -> None:
        while conn.recv(4096):
            pass

    result = exchange(endpoint(drop), "deliver boundary", "hi")

    assert result == PushResult("unknown", "connection dropped after the write")


def test_no_reply_in_time_is_unknown(endpoint: Callable[[Handler], Path]) -> None:
    gate = threading.Event()

    def stall(conn: socket.socket) -> None:
        while conn.recv(4096):
            pass
        gate.wait(5)

    result = exchange(endpoint(stall), "deliver boundary", "hi", timeout=0.2)
    gate.set()

    assert result == PushResult("unknown", "no reply within 0.2s")


def test_ping_answers_without_delivering(endpoint: Callable[[Handler], Path]) -> None:
    received: list[bytes] = []

    def pong(conn: socket.socket) -> None:
        data = b""
        while chunk := conn.recv(4096):
            data += chunk
        received.append(data)
        conn.sendall(b'ok pong {"wire":2,"session":"s1","transcript":"/t/s1.jsonl"}\n')

    assert ping(endpoint(pong)) == Pong(2, "s1", Path("/t/s1.jsonl"))
    assert received == [b"ping\n"]


def test_ping_to_a_missing_socket_has_no_answer(tmp_path: Path) -> None:
    assert ping(tmp_path / "missing.sock") is None


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("pong", Pong(None, None, None)),
        ('pong {"wire":1}', Pong(1, None, None)),
        ('pong {"wire":true}', Pong(None, None, None)),
        ("pong not json", Pong(None, None, None)),
    ],
)
def test_a_pong_without_a_usable_wire_version_is_not_current(detail: str, expected: Pong) -> None:
    assert parse_pong(detail) == expected
    assert expected.current is False


def test_a_relative_transcript_path_is_dropped() -> None:
    reported = parse_pong('pong {"wire":2,"session":"s1","transcript":"relative.jsonl"}')

    assert reported == Pong(2, "s1", None)


def test_a_session_mismatch_is_busy_never_rejected() -> None:
    assert parse_reply("error session_mismatch s2") == PushResult("busy", "session_mismatch s2")


def test_multibyte_text_arrives_intact(endpoint: Callable[[Handler], Path]) -> None:
    received: list[bytes] = []

    result = exchange(endpoint(reads(received)), "deliver interrupt", "héllo ✓ 日本")

    assert result == PushResult("accepted", "forwarded")
    assert received[0].decode() == "deliver interrupt\nhéllo ✓ 日本"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b"busy compacting\n", PushResult("busy", "compacting")),
        (b"error empty message\n", PushResult("rejected", "empty message")),
    ],
)
def test_busy_and_error_replies(
    endpoint: Callable[[Handler], Path], reply: bytes, expected: PushResult
) -> None:
    def answer(conn: socket.socket) -> None:
        while conn.recv(4096):
            pass
        conn.sendall(reply)

    assert exchange(endpoint(answer), "deliver boundary", "hi") == expected


def test_endpoint_paths_name_harness_and_pid_and_respect_the_socket_limit() -> None:
    assert endpoint_path(Path("/tmp/bellhop-501"), "pi", 4242) == Path(
        "/tmp/bellhop-501/pi-4242.sock"
    )
    with pytest.raises(BellhopError) as caught:
        endpoint_path(Path("/" + "x" * 100), "pi", 4242)
    assert caught.value.code == "runtime_dir_unsafe"
