import os
import socket
import threading
from pathlib import Path

import pytest

from bellhop import codex_rpc
from bellhop.codex_rpc import (
    STARTING,
    Turn,
    active_turn,
    connect,
    frame,
    thread_status,
    unframe,
)
from fake_app_server import app_server


@pytest.mark.parametrize("size", [5, 300, 70_000])
def test_a_masked_frame_of_any_length_unframes_to_its_payload(size: int) -> None:
    payload = os.urandom(size)

    parsed = unframe(frame(payload, b"\x01\x02\x03\x04") + b"rest")

    assert parsed == (True, 1, payload, b"rest")


def test_a_partial_frame_waits_for_more() -> None:
    whole = frame(b"x" * 300, b"\x00\x00\x00\x00")

    assert unframe(whole[:3]) is None
    assert unframe(whole[:-1]) is None


def test_a_refused_upgrade_is_a_connection_error(short_tmp: Path) -> None:
    path = short_tmp / "http.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)

    def refuse() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")

    threading.Thread(target=refuse, daemon=True).start()
    try:
        with pytest.raises(ConnectionError, match="websocket upgrade refused"):
            connect(path, 2.0)
    finally:
        listener.close()


def test_the_client_handshakes_and_lists_loaded_threads(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=["t1", "t2"]) as server:
        client = connect(path, 2.0)
        try:
            loaded = client.loaded()
        finally:
            client.close()

    assert loaded == ["t1", "t2"]
    assert [method for method, _ in server.calls][:2] == ["initialize", "initialized"]


def test_turn_state_helpers_read_the_server_shapes() -> None:
    assert thread_status({"thread": {"status": {"type": "active", "activeFlags": []}}}) is None
    assert thread_status({"thread": {"status": {"type": "idle"}}}) == Turn("idle")
    assert thread_status(
        {"thread": {"status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}}
    ) == Turn("waiting")
    assert active_turn({"data": [{"id": "t9", "status": "inProgress"}]}) == Turn("running", "t9")
    assert active_turn({"data": [{"id": "t8", "status": "completed"}]}) == STARTING


def test_a_socket_link_with_a_long_path_is_followed_to_its_target(
    short_tmp: Path, tmp_path: Path
) -> None:
    target = short_tmp / "app.sock"
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    link = deep / "app-server-control.sock"
    link.symlink_to(target)
    with app_server(target, loaded=["t1"]):
        client = connect(link, 2.0)
        try:
            assert client.loaded() == ["t1"]
        finally:
            client.close()


def test_the_client_answers_server_requests_and_pings_while_it_waits(short_tmp: Path) -> None:
    path = short_tmp / "app.sock"
    with app_server(path, loaded=["t1"], chatter=True) as server, connect(path, 2.0) as client:
        loaded = client.loaded()

    assert loaded == ["t1"]
    assert server.answered[0] == {
        "id": "srv-1",
        "error": {"code": -32601, "message": "unsupported"},
    }
    assert server.pongs[0] == b"are you there"
    assert server.unmasked == 0


@pytest.mark.parametrize(
    ("reply", "limit", "error"),
    [
        (b"[" * 200_000, codex_rpc.MESSAGE_LIMIT, ValueError),
        (b'{"id": 1, "result": {"pad": "' + b"x" * 5000 + b'"}}', 4096, ConnectionError),
    ],
    ids=["nested too deeply", "longer than the limit"],
)
def test_a_reply_the_client_cannot_hold_is_an_error_it_reports(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: bytes,
    limit: int,
    error: type[Exception],
) -> None:
    monkeypatch.setattr(codex_rpc, "MESSAGE_LIMIT", limit)
    path = short_tmp / "app.sock"
    with app_server(path, raw_initialize=reply), pytest.raises(error):
        connect(path, 2.0)


@pytest.mark.parametrize(
    ("unsafe", "refusal"),
    [("open directory", "is not a private directory"), ("not a socket", "is not a socket owned")],
    ids=["open directory", "not a socket"],
)
def test_the_client_refuses_a_socket_it_cannot_trust(
    short_tmp: Path, unsafe: str, refusal: str
) -> None:
    shared = short_tmp / "shared"
    shared.mkdir(mode=0o700)
    target = shared / "app.sock"
    plain = shared / "plain-file"
    plain.write_text("")
    link = short_tmp / "app-server-control.sock"
    if unsafe == "open directory":
        shared.chmod(0o777)
    with app_server(target, loaded=["t1"]):
        link.symlink_to(plain if unsafe == "not a socket" else target)
        with pytest.raises(ConnectionError, match=refusal):
            connect(link, 2.0)
