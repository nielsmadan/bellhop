import contextlib
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from bellhop.models import PushResult
from bellhop.routes import REPLY_LIMIT, endpoint_push, parse_reply, route_finder
from bellhop.routes.fake import FakeRoute
from bellhop.transcripts import PlainTranscripts, reader_finder
from conftest import make_session

type Handler = Callable[[socket.socket], None]


@pytest.fixture
def endpoint(tmp_path: Path) -> Iterator[Callable[[Handler], Path]]:
    servers: list[socket.socket] = []
    path = Path("/tmp") / f"bh-test-{tmp_path.name[-20:]}.sock"

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

    result = endpoint_push(path, "bellhop-push 1", "x", timeout=0.5)

    assert result == PushResult("unknown", "no reply within 0.5s")
    assert time.monotonic() - started < 1.5


def test_an_endless_reply_line_is_cut_at_the_limit(endpoint: Callable[[Handler], Path]) -> None:
    def flood(conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            conn.sendall(b"ok " + b"y" * 10_000 + b"\n")

    result = endpoint_push(endpoint(flood), "bellhop-push 1", "x")

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

    result = endpoint_push(path, "deliver boundary", "[bellhop bh_1 from enki] hi")

    assert result == PushResult("accepted", "forwarded")
    assert received == [b"deliver boundary\n[bellhop bh_1 from enki] hi"]


def test_a_missing_or_stale_endpoint_wrote_nothing_and_is_busy(tmp_path: Path) -> None:
    stale = Path("/tmp") / f"bh-stale-{tmp_path.name[-16:]}.sock"
    left = socket.socket(socket.AF_UNIX)
    left.bind(str(stale))
    left.close()
    try:
        results = [
            endpoint_push(path, "deliver boundary", "hi")
            for path in (tmp_path / "missing.sock", stale)
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

    result = endpoint_push(endpoint(drop), "deliver boundary", "hi")

    assert result == PushResult("unknown", "connection dropped after the write")


def test_no_reply_in_time_is_unknown(endpoint: Callable[[Handler], Path]) -> None:
    gate = threading.Event()

    def stall(conn: socket.socket) -> None:
        while conn.recv(4096):
            pass
        gate.wait(5)

    result = endpoint_push(endpoint(stall), "deliver boundary", "hi", timeout=0.2)
    gate.set()

    assert result == PushResult("unknown", "no reply within 0.2s")


def test_the_fake_route_takes_outcomes_in_order_and_logs_pushes(tmp_path: Path) -> None:
    config = tmp_path / "routes.json"
    log = tmp_path / "pushes.jsonl"
    config.write_text(json.dumps({"log": str(log), "default": {"outcomes": ["busy", "accepted"]}}))
    find = route_finder({"BELLHOP_FAKE_ROUTES": str(config)})
    session = make_session("s1")

    first = find(session)
    assert first is not None
    assert first.push(session, "one", interrupt=False).outcome == "busy"
    second = find(session)
    assert second is not None
    assert second.push(session, "two", interrupt=True).outcome == "accepted"
    assert [json.loads(line)["text"] for line in log.read_text().splitlines()] == ["one", "two"]


def test_the_fake_route_can_be_absent_per_session(tmp_path: Path) -> None:
    config = tmp_path / "routes.json"
    config.write_text(json.dumps({"sessions": {"s1": {"none": True}}}))

    assert route_finder({"BELLHOP_FAKE_ROUTES": str(config)})(make_session("s1")) is None
    assert route_finder({})(make_session("s1")) is None


def test_an_echoing_fake_route_writes_the_transcript_the_plain_reader_reads(tmp_path: Path) -> None:
    transcript = tmp_path / "t.txt"
    transcript.write_text("before [bellhop bh_1 from x]\n")
    session = make_session("s1", transcript_path=transcript)
    reader = PlainTranscripts()
    offset = reader.size(session)
    assert offset is not None

    FakeRoute(echo=True).push(session, "[bellhop bh_1 from enki] hi", interrupt=False)

    assert reader.contains(session, "[bellhop bh_1 from", since=offset) is True
    assert reader.contains(session, "[bellhop bh_2 from", since=0) is False
    assert reader_finder({"BELLHOP_FAKE_ROUTES": "x"})(session) is not None
    assert reader_finder({})(session) is None
