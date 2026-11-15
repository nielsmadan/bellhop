import contextlib
import fcntl
import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bellhop.relay import (
    Channel,
    Listener,
    Relay,
    Request,
    SessionInfo,
    belongs_to,
    claude_pid,
    forwarder,
    mcp_reply,
    parse_request,
    read_registry,
    reply,
    serve,
    session_info,
    watch_parent,
)
from bellhop.routes.unix import exchange, ping

SESSION = "c1a0de00-0000-4000-8000-00000000c1de"
TOKEN = "child-token-not-for-logs"  # noqa: S105
INFO = SessionInfo(SESSION, Path("/c/projects/-w/c1.jsonl"))


@dataclass
class Forwarded:
    calls: list[tuple[str, str]] = field(default_factory=list)
    failure: str | None = None

    def __call__(self, text: str, priority: str) -> str | None:
        self.calls.append((text, priority))
        return self.failure


def deliver(when: str, body: str, target: str = SESSION, count: int | None = None) -> Request:
    size = len(body.encode()) if count is None else count
    return parse_request(f"deliver {when} {target} {size}\n{body}".encode())


def test_ping_reports_the_session_and_forwards_nothing() -> None:
    forwarded = Forwarded()

    answer = reply(parse_request(b"ping\n"), INFO, forwarded)

    assert answer == (
        f'ok pong {{"wire":2,"session":"{SESSION}","transcript":"/c/projects/-w/c1.jsonl"}}'
    )
    assert forwarded.calls == []


@pytest.mark.parametrize(("when", "priority"), [("boundary", "later"), ("interrupt", "next")])
def test_a_delivery_is_forwarded_at_its_priority(when: str, priority: str) -> None:
    forwarded = Forwarded()

    answer = reply(deliver(when, "héllo ✓"), INFO, forwarded)

    assert answer == f"ok {priority}"
    assert forwarded.calls == [("héllo ✓", priority)]


@pytest.mark.parametrize(
    ("request_", "info", "answer"),
    [
        (deliver("boundary", "hi", count=40), INFO, "error truncated"),
        (deliver("boundary", "hi", target="other"), INFO, f"error session_mismatch {SESSION}"),
        (deliver("boundary", "hi"), SessionInfo(None, None), "error session_mismatch unknown"),
        (deliver("boundary", ""), INFO, "error empty message"),
        (parse_request(b"deliver boundary\nhi"), INFO, "error wire_version"),
        (parse_request(b"deliver soon s 2\nhi"), INFO, "error unknown header deliver soon s 2"),
        (Request(f"deliver boundary {SESSION} ²", "hi", 2), INFO, "error wire_version"),
        (
            parse_request(f"deliver boundary {SESSION} 2 extra\nhi".encode()),
            INFO,
            f"error unknown header deliver boundary {SESSION} 2 extra",
        ),
    ],
    ids=[
        "truncated",
        "another session",
        "nameless session",
        "empty",
        "wire 1",
        "unknown",
        "a count that is not ascii digits",
        "an extra field",
    ],
)
def test_a_delivery_that_cannot_be_trusted_is_refused_unforwarded(
    request_: Request, info: SessionInfo, answer: str
) -> None:
    forwarded = Forwarded()

    assert reply(request_, info, forwarded) == answer
    assert forwarded.calls == []


def test_a_frame_claude_could_not_take_leaves_the_message_waiting() -> None:
    forwarded = Forwarded(failure="claude's messaging socket: Connection refused")

    answer = reply(deliver("boundary", "hi"), INFO, forwarded)

    assert answer == "busy claude's messaging socket: Connection refused"


def test_the_registry_names_the_session_and_its_transcript(tmp_path: Path) -> None:
    entry = {"pid": 4242, "sessionId": SESSION, "cwd": "/Users/me/work.dir"}

    assert session_info(entry, tmp_path) == SessionInfo(
        SESSION, tmp_path / "projects" / "-Users-me-work-dir" / f"{SESSION}.jsonl"
    )
    assert session_info(None, tmp_path) == SessionInfo(None, None)
    assert session_info({"sessionId": SESSION}, tmp_path) == SessionInfo(SESSION, None)


@pytest.mark.parametrize(
    "entry", [{"pid": 5151, "sessionId": SESSION}, {"sessionId": SESSION}], ids=["other", "none"]
)
def test_a_registry_entry_that_does_not_name_our_pid_is_not_ours(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "4242.json").write_text(json.dumps(entry))

    assert read_registry(tmp_path, 4242) is None


def test_a_session_id_that_is_not_an_id_names_no_session(tmp_path: Path) -> None:
    entry = {"pid": 1, "sessionId": "../../elsewhere", "cwd": "/w"}

    assert session_info(entry, tmp_path) == SessionInfo(None, None)


def test_the_channel_never_shows_its_token() -> None:
    assert TOKEN not in repr(Channel("/tmp/peer.sock", TOKEN))


def test_the_relay_never_shows_its_environment() -> None:
    channel = Channel("/tmp/peer.sock", TOKEN)
    relay = Relay({"CLAUDE_CODE_MESSAGING_TOKEN": TOKEN}, channel, 1)

    assert TOKEN not in repr(relay)


def test_a_forward_with_no_time_left_is_refused_without_reaching_claude(short_tmp: Path) -> None:
    with claude_peer(short_tmp / "peer.sock") as received:
        failure = forwarder(Channel(str(short_tmp / "peer.sock"), TOKEN))("hi", "next", 0.0)
        time.sleep(0.2)

    assert failure == "the relay ran out of time before forwarding"
    assert received == []


def test_a_request_that_arrives_late_is_answered_busy_in_time(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bellhop.relay.ANSWER_BY_SECONDS", 0.3)
    listener = serving(short_tmp)
    calls: list[float] = []

    def forward(text: str, priority: str, seconds: float) -> str | None:
        calls.append(seconds)
        return None if seconds > 0 else "the relay ran out of time before forwarding"

    threading.Thread(target=serve, args=(listener, lambda: INFO, forward), daemon=True).start()
    body = "hi"
    try:
        with socket.socket(socket.AF_UNIX) as conn:
            conn.connect(str(listener.path))
            time.sleep(0.5)
            conn.sendall(f"deliver boundary {SESSION} 2\n{body}".encode())
            conn.shutdown(socket.SHUT_WR)
            conn.settimeout(2.0)
            answer = conn.recv(200)
    finally:
        listener.close()

    assert answer == b"busy the relay ran out of time before forwarding\n"
    assert calls[0] <= 0


def registry(entries: dict[int, dict[str, object]]) -> Callable[[Path, int], object]:
    return lambda claude_dir, pid: entries.get(pid)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def choose(entries: dict[int, dict[str, object]], started_as: str | None = SESSION) -> int:
    clock = Clock()
    return claude_pid(
        Path("/c"),
        [100, 200, 300],
        started_as,
        read=registry(entries),  # type: ignore[arg-type]
        clock=clock,
        sleep=clock.sleep,
    )


def test_the_relay_serves_its_direct_parent_when_it_has_an_entry() -> None:
    assert choose({100: {"sessionId": "x"}, 300: {"sessionId": SESSION}}) == 100


def test_an_enclosing_claude_is_taken_only_when_it_is_the_session_the_relay_started_in() -> None:
    mine: dict[int, dict[str, object]] = {300: {"sessionId": SESSION}}

    assert choose(mine) == 300
    assert choose({300: {"sessionId": "the-outer-claude"}}) == 100
    assert choose(mine, started_as=None) == 100


def test_the_parent_gets_the_whole_wait_before_any_ancestor_is_considered() -> None:
    clock = Clock()
    outer: dict[str, object] = {"sessionId": SESSION}

    def late_parent(claude_dir: Path, pid: int) -> dict[str, object] | None:
        if pid == 100:
            return {"sessionId": SESSION} if clock.now >= 3.0 else None
        return outer if pid == 300 else None

    found = claude_pid(
        Path("/c"), [100, 200, 300], SESSION, read=late_parent, clock=clock, sleep=clock.sleep
    )

    assert found == 100


def test_with_no_registry_entry_the_relay_names_its_socket_after_its_parent(
    tmp_path: Path,
) -> None:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    found = claude_pid(tmp_path, [100, 200], SESSION, clock=lambda: clock[0], sleep=sleep)

    assert found == 100
    assert clock[0] >= 10.0


def test_the_relay_stops_when_its_parent_changes() -> None:
    parents = iter([500, 500, 1])
    stopped: list[bool] = []

    watch_parent(
        500,
        lambda: stopped.append(True),
        current_parent=lambda: next(parents),
        sleep=lambda s: None,
    )

    assert stopped == [True]


def test_the_relay_answers_mcp_as_a_server_with_no_tools() -> None:
    initialize = {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}

    assert mcp_reply(initialize) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "bellhop", "version": "1"},
        },
    }
    assert mcp_reply({"id": 2, "method": "tools/list"}) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"tools": []},
    }
    assert mcp_reply({"id": 3, "method": "ping"}) == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_an_mcp_notification_gets_no_answer_and_an_unknown_method_an_error() -> None:
    assert mcp_reply({"method": "notifications/initialized"}) is None
    unknown = mcp_reply({"id": 9, "method": "resources/list"})
    assert unknown is not None
    assert unknown["error"] == {"code": -32601, "message": "method not found: resources/list"}


@contextlib.contextmanager
def claude_peer(path: Path) -> Iterator[list[bytes]]:
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)

    def accept() -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = server.accept()
                with conn:
                    data = b""
                    while chunk := conn.recv(65536):
                        data += chunk
                    received.append(data)

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield received
    finally:
        server.close()


def frames(received: list[bytes]) -> list[list[dict[str, object]]]:
    return [[json.loads(line) for line in data.splitlines()] for data in received]


def wait_until(probe: object, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callable(probe) and probe():
            return True
        time.sleep(0.05)
    return False


def test_the_forwarder_writes_one_auth_line_and_one_user_frame(short_tmp: Path) -> None:
    with claude_peer(short_tmp / "peer.sock") as received:
        failure = forwarder(Channel(str(short_tmp / "peer.sock"), TOKEN))("hi", "next", 1.0)
        assert wait_until(lambda: received)

    ((auth, user),) = frames(received)
    assert failure is None
    assert auth == {"type": "auth", "token": TOKEN}
    assert (user["type"], user["priority"], user["message"]) == (
        "user",
        "next",
        {"role": "user", "content": "hi"},
    )


def test_a_closed_claude_socket_is_a_failure_that_never_names_the_token(short_tmp: Path) -> None:
    failure = forwarder(Channel(str(short_tmp / "missing.sock"), TOKEN))("hi", "later", 1.0)

    assert failure is not None
    assert failure.startswith("claude's messaging socket: ")
    assert TOKEN not in failure


@dataclass
class FlakyServer:
    real: socket.socket
    failures: int = 1

    def accept(self) -> tuple[socket.socket, object]:
        if self.failures:
            self.failures -= 1
            raise ConnectionAbortedError(53, "Software caused connection abort")
        return self.real.accept()


def serving(short_tmp: Path) -> Listener:
    listener = Listener(short_tmp / "claude-1.sock", short_tmp / "claude-1.lock")
    listener.claim()
    listener.bind()
    return listener


def test_the_relay_keeps_serving_after_an_accept_error(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = serving(short_tmp)
    assert listener.server is not None
    real = listener.server
    listener.server = FlakyServer(real)  # type: ignore[assignment]
    threading.Thread(target=serve, args=(listener, lambda: INFO, Forwarded()), daemon=True).start()
    try:
        found = ping(listener.path, timeout=2.0)
    finally:
        listener.server = real
        listener.close()

    assert found is not None
    assert found.session == SESSION


def test_a_request_gets_one_deadline_and_a_full_relay_turns_callers_away(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bellhop.relay.REQUEST_SECONDS", 0.5)
    monkeypatch.setattr("bellhop.relay.HANDLERS", 1)
    listener = serving(short_tmp)
    threading.Thread(target=serve, args=(listener, lambda: INFO, Forwarded()), daemon=True).start()
    trickle = socket.socket(socket.AF_UNIX)
    turned_away = socket.socket(socket.AF_UNIX)
    try:
        trickle.connect(str(listener.path))
        started = time.monotonic()
        turned_away.connect(str(listener.path))
        turned_away.settimeout(2.0)
        refused = turned_away.recv(100)
        refused_after = time.monotonic() - started
        closed_at = None
        trickle.settimeout(0.2)
        while closed_at is None and time.monotonic() - started < 3.0:
            with contextlib.suppress(OSError):
                trickle.sendall(b"p")
            with contextlib.suppress(TimeoutError):
                if trickle.recv(100) == b"":
                    closed_at = time.monotonic() - started
    finally:
        trickle.close()
        turned_away.close()
        listener.close()

    assert refused == b"busy relay is at capacity\n"
    assert refused_after < 0.3
    assert closed_at is not None
    assert closed_at < 1.5


def test_closing_twice_on_one_thread_does_not_hang(short_tmp: Path) -> None:
    listener = Listener(short_tmp / "claude-1.sock", short_tmp / "claude-1.lock")
    listener.claim()
    listener.bind()

    with listener.closing:
        listener.close()

    assert not listener.path.exists()


def test_a_runtime_dir_near_the_socket_path_limit_still_binds(short_tmp: Path) -> None:
    final_name = "claude-4242.sock"
    filler = 100 - len(str(short_tmp)) - 2 - len(final_name)
    directory = short_tmp / ("d" * filler)
    directory.mkdir()
    listener = Listener(directory / final_name, directory / "claude-4242.lock")
    listener.claim()
    listener.bind()
    try:
        bound = listener.path.exists()
    finally:
        listener.close()

    assert len(str(directory / final_name)) == 100
    assert bound is True


def test_a_second_listener_cannot_claim_the_socket(short_tmp: Path) -> None:
    first = Listener(short_tmp / "claude-1.sock", short_tmp / "claude-1.lock")
    second = Listener(short_tmp / "claude-1.sock", short_tmp / "claude-1.lock")

    assert first.claim() is True
    first.bind()
    assert second.claim() is False
    first.close()

    assert not (short_tmp / "claude-1.sock").exists()
    assert not (short_tmp / "claude-1.lock").exists()


def test_a_listener_leaves_a_socket_that_replaced_its_own(short_tmp: Path) -> None:
    listener = Listener(short_tmp / "claude-1.sock", short_tmp / "claude-1.lock")
    listener.claim()
    listener.bind()
    (short_tmp / "claude-1.sock").unlink()
    (short_tmp / "claude-1.sock").write_text("someone else's")

    listener.close()

    assert (short_tmp / "claude-1.sock").read_text() == "someone else's"


@dataclass
class RelayProcess:
    process: subprocess.Popen[str]
    socket: Path

    def initialize(self) -> dict[str, object]:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps({"id": 1, "method": "initialize"}) + "\n")
        self.process.stdin.flush()
        answer: dict[str, object] = json.loads(self.process.stdout.readline())
        return answer


@pytest.fixture
def relay_env(short_tmp: Path, tmp_path: Path) -> dict[str, str]:
    claude_dir = tmp_path / "claude"
    (claude_dir / "sessions").mkdir(parents=True)
    entry = {"pid": os.getpid(), "sessionId": SESSION, "cwd": "/w"}
    (claude_dir / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(entry))
    runtime = short_tmp / "rt"
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "CLAUDE_CONFIG_DIR": str(claude_dir),
        "BELLHOP_RUNTIME_DIR": str(runtime),
        "CLAUDE_CODE_MESSAGING_SOCKET": str(short_tmp / f"{os.getpid()}.sock"),
        "CLAUDE_CODE_MESSAGING_TOKEN": TOKEN,
    }


def start_relay(env: dict[str, str]) -> RelayProcess:
    process = subprocess.Popen(
        [sys.executable, "-m", "bellhop", "_relay"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return RelayProcess(process, Path(env["BELLHOP_RUNTIME_DIR"]) / f"claude-{os.getpid()}.sock")


def stop(relay: RelayProcess) -> tuple[int, str]:
    process = relay.process
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    code = process.wait(10)
    errors = process.stderr.read() if process.stderr is not None else ""
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    return code, errors


def test_the_relay_serves_its_session_and_cleans_up_when_claude_closes_stdin(
    relay_env: dict[str, str], short_tmp: Path
) -> None:
    with claude_peer(short_tmp / f"{os.getpid()}.sock") as received:
        relay = start_relay(relay_env)
        try:
            assert relay.initialize()["id"] == 1
            assert wait_until(relay.socket.exists)
            pong = ping(relay.socket, timeout=2.0)
            result = exchange(relay.socket, f"deliver interrupt {SESSION} 2", "hi")
            assert wait_until(lambda: received)
        finally:
            code, errors = stop(relay)

    assert pong is not None
    assert (pong.session, pong.current) == (SESSION, True)
    assert (result.outcome, result.detail) == ("accepted", "next")
    assert frames(received)[0][1]["message"] == {"role": "user", "content": "hi"}
    assert code == 0
    assert TOKEN not in errors
    assert sorted(path.name for path in relay.socket.parent.iterdir()) == []


def test_a_second_relay_in_the_same_claude_leaves_the_first_ones_socket(
    relay_env: dict[str, str],
) -> None:
    first = start_relay(relay_env)
    try:
        first.initialize()
        assert wait_until(first.socket.exists)
        inode = first.socket.stat().st_ino
        second = start_relay(relay_env)
        second.initialize()
        _, notes = stop(second)
        after = first.socket.stat().st_ino
        pong = ping(first.socket, timeout=2.0)
    finally:
        stop(first)

    assert "another relay serves" in notes
    assert after == inode
    assert pong is not None
    assert pong.session == SESSION


def test_the_relay_removes_its_socket_when_terminated(relay_env: dict[str, str]) -> None:
    relay = start_relay(relay_env)
    relay.initialize()
    assert wait_until(relay.socket.exists)

    relay.process.send_signal(signal.SIGTERM)
    code = relay.process.wait(10)
    stop(relay)

    assert code == 0
    assert not relay.socket.exists()


def test_a_waiting_relay_takes_over_when_the_first_one_goes(relay_env: dict[str, str]) -> None:
    first = start_relay(relay_env)
    first.initialize()
    assert wait_until(first.socket.exists)
    second = start_relay(relay_env)
    second.initialize()
    assert second.process.stderr is not None
    waiting, _, _ = select.select([second.process.stderr], [], [], 10)
    note = second.process.stderr.readline() if waiting else ""
    try:
        stop(first)
        took_over = wait_until(
            lambda: (
                (pong := ping(second.socket, timeout=0.5)) is not None and pong.session == SESSION
            )
        )
    finally:
        stop(second)

    assert "another relay serves" in note
    assert took_over is True


NEW_SESSION = "c1a0de00-0000-4000-8000-0000000c1ea2"


def test_the_relay_follows_its_session_when_claude_clears_it(
    relay_env: dict[str, str], tmp_path: Path
) -> None:
    entry = Path(relay_env["CLAUDE_CONFIG_DIR"]) / "sessions" / f"{os.getpid()}.json"
    relay = start_relay(relay_env)
    try:
        relay.initialize()
        assert wait_until(relay.socket.exists)
        before = ping(relay.socket, timeout=2.0)
        entry.write_text(json.dumps({"pid": os.getpid(), "sessionId": NEW_SESSION, "cwd": "/w"}))
        after = ping(relay.socket, timeout=2.0)
    finally:
        stop(relay)

    assert before is not None and after is not None
    assert (before.session, after.session) == (SESSION, NEW_SESSION)
    assert (
        after.transcript
        == Path(relay_env["CLAUDE_CONFIG_DIR"]) / "projects" / "-w" / f"{NEW_SESSION}.jsonl"
    )


def test_the_relay_waits_for_a_registry_entry_claude_writes_late(
    relay_env: dict[str, str],
) -> None:
    entry = Path(relay_env["CLAUDE_CONFIG_DIR"]) / "sessions" / f"{os.getpid()}.json"
    written = entry.read_text()
    entry.unlink()
    relay = start_relay(relay_env)
    try:
        relay.initialize()
        time.sleep(1.0)
        entry.write_text(written)
        assert wait_until(relay.socket.exists)
        found = ping(relay.socket, timeout=2.0)
    finally:
        stop(relay)

    assert relay.socket.name == f"claude-{os.getpid()}.sock"
    assert found is not None
    assert found.session == SESSION


def test_a_lock_replaced_between_open_and_flock_is_not_claimed(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = Listener(short_tmp / "claude-1.sock", short_tmp / "claude-1.lock")
    real_flock = fcntl.flock

    def replaced(descriptor: int, operation: int) -> None:
        listener.lock.unlink()
        listener.lock.write_text("")
        real_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", replaced)

    assert listener.claim() is False


def test_the_relay_socket_is_private_to_its_owner(short_tmp: Path) -> None:
    listener = Listener(short_tmp / "claude-1.sock", short_tmp / "claude-1.lock")
    listener.claim()
    listener.bind()
    try:
        mode = stat.S_IMODE(listener.path.stat().st_mode)
    finally:
        listener.close()

    assert mode == 0o600


def test_the_relay_never_listens_on_a_channel_inherited_from_another_claude(
    relay_env: dict[str, str], short_tmp: Path
) -> None:
    inherited = {**relay_env, "CLAUDE_CODE_MESSAGING_SOCKET": str(short_tmp / "1.sock")}
    relay = start_relay(inherited)
    relay.initialize()
    time.sleep(1.0)
    listening = relay.socket.exists()
    _, notes = stop(relay)

    assert listening is False
    assert f"is not claude {os.getpid()}'s own; not listening" in notes


def test_a_channel_belongs_to_the_claude_its_socket_is_named_after() -> None:
    assert belongs_to(Channel("/tmp/cc-socks/4242.sock", TOKEN), 4242) is True
    assert belongs_to(Channel("/tmp/cc-socks/5151.sock", TOKEN), 4242) is False
