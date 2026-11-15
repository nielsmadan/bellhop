import contextlib
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bellhop import runtime
from bellhop.cli import main
from bellhop.identity import Proc
from bellhop.models import Message, Session
from bellhop.runtime import ensure_runtime_dir
from bellhop.store import connect, get_messages

FAKE_HLS = Path(__file__).resolve().parent / "fakes" / "hooklinesinker"
STARTED = datetime(2026, 11, 13, 9, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 14, 9, 0, 0, tzinfo=UTC)


def make_session(session_id: str, **changes: object) -> Session:
    base = Session(
        session_id=session_id,
        binding_id=f"binding-{session_id}",
        harness="claude",
        title=f"title-{session_id}",
        cwd=Path(f"/work/{session_id}"),
        transcript_path=None,
        phase="idle",
        observed_at=datetime(2026, 11, 13, 9, 12, 3, tzinfo=UTC),
        pid=4242,
        process_started_at=STARTED,
        tmux_pane=None,
        tmux_socket=None,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


class FakeHls:
    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.path = FAKE_HLS
        self._directory = directory
        self._monkeypatch = monkeypatch
        self.log = directory / "calls.log"
        monkeypatch.setenv("FAKE_HLS_LOG", str(self.log))

    def respond(
        self,
        records: Sequence[object],
        *,
        problems: Sequence[object] = (),
        protocol: object = 1,
    ) -> None:
        output = self._directory / "sessions.json"
        payload = {"protocol": protocol, "sessions": list(records), "problems": list(problems)}
        output.write_text(json.dumps(payload), encoding="utf-8")
        self._monkeypatch.setenv("FAKE_HLS_OUTPUT", str(output))

    def fail(self, exit_code: int, stderr: str) -> None:
        self._monkeypatch.setenv("FAKE_HLS_EXIT", str(exit_code))
        self._monkeypatch.setenv("FAKE_HLS_STDERR", stderr)

    def slow_first_call(self) -> None:
        self._monkeypatch.setenv("FAKE_HLS_SLOW_ONCE", str(self._directory / "slow-once"))

    def always_slow(self) -> None:
        self._monkeypatch.setenv("FAKE_HLS_SLOW", "1")

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def record(agent: str, session_id: str, **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "protocol": 1,
            "bindingId": f"binding-{session_id}",
            "agent": agent,
            "event": "Stop",
            "phase": "idle",
            "running": True,
            "observedAt": "2026-11-13T09:12:03Z",
            "session": {
                "id": session_id,
                "cwd": f"/work/{session_id}",
                "transcriptPath": f"/transcripts/{session_id}.jsonl",
                "title": f"title-{session_id}",
            },
            "activity": {"subagents": 0, "turns": 1},
            "process": {"pid": 4242, "startedAt": "2026-11-13T09:00:00Z", "host": "test"},
            "terminal": None,
            "tmux": {"pane": "%3", "sessionName": "main", "socket": "/sockets/tmux-501/default"},
            "remoteHost": None,
        }
        record.update(overrides)
        return record


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    monkeypatch.delenv("BELLHOP_HOOKLINESINKER", raising=False)
    runtime_home = Path(tempfile.mkdtemp(prefix="bh-rt-", dir="/tmp"))
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(runtime_home / "runtime"))
    monkeypatch.delenv("BELLHOP_FAKE_ROUTES", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    for name in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CODEX_THREAD_ID",
        "PI_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in [name for name in os.environ if name.startswith("FAKE_HLS_")]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime, "ensure_runtime_dir", refuse_real_runtime_dir)
    yield
    shutil.rmtree(runtime_home, ignore_errors=True)


def real_runtime_dirs() -> set[Path]:
    default = Path(f"/tmp/bellhop-{os.getuid()}")
    return {default, default.resolve()}


def refuse_real_runtime_dir(path: Path, uid: int) -> Path:
    if path in real_runtime_dirs() or path.resolve() in real_runtime_dirs():
        pytest.fail(f"a test reached the real runtime dir {path}")
    return ensure_runtime_dir(path, uid)


@pytest.fixture
def fake_hls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHls:
    directory = tmp_path / "fake-hls"
    directory.mkdir()
    monkeypatch.setenv("BELLHOP_HOOKLINESINKER", str(FAKE_HLS))
    return FakeHls(directory, monkeypatch)


FAKE_TMUX = Path(__file__).resolve().parent / "fakes" / "launch-tmux"
CLAUDE_TRUST = "Do you trust the files in this folder?\n❯ 1. Yes, proceed\n"


@dataclass(frozen=True)
class FakeTmux:
    directory: Path

    def calls(self) -> list[list[str]]:
        log = self.directory / "calls.log"
        if not log.exists():
            return []
        chunks = log.read_text().split("CALL\n")[1:]
        return [chunk.splitlines() for chunk in chunks]

    def commands(self) -> list[str]:
        return [call[2] if call[0] in ("-L", "-S") else call[0] for call in self.calls()]

    def argv(self, name: str) -> list[str]:
        return (self.directory / f"argv-{name}").read_text().split("\0")[:-1]

    def sessions(self) -> list[str]:
        folder = self.directory / "sessions"
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    def show(self, text: str) -> None:
        (self.directory / "pane.txt").write_text(text)

    def close_pane(self) -> None:
        (self.directory / "pane-gone").touch()

    def refuse(self) -> None:
        (self.directory / "new-session-fails").touch()

    @property
    def executable(self) -> str:
        return str(self.directory / "bin" / "tmux")

    def client_env(self) -> list[str]:
        return (self.directory / "client-env").read_text().split()

    def hang(self) -> None:
        (self.directory / "new-session-hangs").touch()

    def hang_before_starting(self) -> None:
        (self.directory / "new-session-hangs-early").touch()

    def occupy(self, name: str) -> None:
        (self.directory / "sessions").mkdir(exist_ok=True)
        (self.directory / "sessions" / name).write_text("%9\n")


@pytest.fixture
def fake_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    directory = tmp_path / "fake-tmux"
    binaries = directory / "bin"
    binaries.mkdir(parents=True)
    (binaries / "tmux").symlink_to(FAKE_TMUX)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    return FakeTmux(directory)


@pytest.fixture(autouse=True)
def drainer_starts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []

    def record(env: Mapping[str, str]) -> int:
        started.append(env.get("XDG_STATE_HOME", ""))
        return 1

    monkeypatch.setattr("bellhop.cli.context.start_drainer", record)
    return started


GOLDEN = Path(__file__).resolve().parent / "golden"
MESSAGE_ID = re.compile(r"bh_[0-9a-f]{12}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def normalized(payload: object) -> str:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    ids: dict[str, str] = {}
    for found in MESSAGE_ID.findall(text):
        ids.setdefault(found, f"bh_{len(ids) + 1:012x}")
    text = MESSAGE_ID.sub(lambda match: ids[match.group(0)], text)
    return TIMESTAMP.sub("2026-11-14T09:00:00Z", text) + "\n"


def check_golden(name: str, payload: object, paths: Mapping[str, str] | None = None) -> None:
    path = GOLDEN / f"{name}.json"
    text = normalized(payload)
    for real, placeholder in (paths or {}).items():
        text = text.replace(real, placeholder)
    if os.environ.get("BELLHOP_UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text


def make_message(number: int, **changes: object) -> Message:
    base = Message(
        seq=0,
        id=f"bh_{number:012x}",
        key=None,
        target_kind="session",
        target="s1",
        harness="claude",
        title="t",
        sender="enki",
        body=f"body {number}",
        urgency="idle",
        degraded_from=None,
        wake=False,
        state="pending",
        route=None,
        detail=None,
        session_id=None,
        transcript_offset=None,
        transcript=None,
        created_at=NOW,
        sent_at=None,
        confirmed_at=None,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


ALPHA = "2e2042a9-937c-4e03-ba25-74b5ba14a58f"
BETA = "2e20ffff-0000-4000-8000-000000000002"
DUP1 = "7f00aa11-0000-4000-8000-000000000003"
DUP2 = "7f00bb22-0000-4000-8000-000000000004"
BELL_PIDS = {ALPHA: 4242, BETA: 4343, DUP1: 4444, DUP2: 4545}


@dataclass
class Bell:
    tmp: Path
    capsys: pytest.CaptureFixture[str]
    monkeypatch: pytest.MonkeyPatch
    hls: FakeHls
    routes: dict[str, object] = field(default_factory=dict)

    def transcript(self, session_id: str) -> Path:
        return self.tmp / "transcripts" / f"{session_id}.txt"

    def record(
        self, harness: str, session_id: str, title: str, **changes: object
    ) -> dict[str, object]:
        path = self.transcript(session_id)
        path.parent.mkdir(exist_ok=True)
        path.touch()
        session = {
            "id": session_id,
            "cwd": f"/work/{title}",
            "transcriptPath": str(path),
            "title": title,
        }
        process = {"pid": BELL_PIDS.get(session_id, 4242), "startedAt": "2026-11-13T09:00:00Z"}
        return FakeHls.record(
            harness, session_id, session=session, **{"process": process, **changes}
        )

    def sessions(self, *records: dict[str, object]) -> None:
        self.hls.respond(list(records))

    def route(self, session_id: str | None = None, **settings: object) -> None:
        sessions = self.routes.setdefault("sessions", {})
        if session_id is None:
            self.routes["default"] = settings
        else:
            assert isinstance(sessions, dict)
            sessions[session_id] = settings
        self.save()

    def save(self) -> None:
        path = self.tmp / "routes.json"
        path.write_text(json.dumps(self.routes))
        self.monkeypatch.setenv("BELLHOP_FAKE_ROUTES", str(path))

    def pushes(self) -> list[dict[str, Any]]:
        log = self.tmp / "pushes.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def run(self, *argv: str, stdin: str = "") -> tuple[int, dict[str, Any]]:
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = main([*argv, "--json"])
        captured = self.capsys.readouterr()
        assert captured.err == ""
        payload: dict[str, Any] = json.loads(captured.out)
        return code, payload

    def stored(self) -> list[dict[str, object]]:
        conn = connect(self.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
        try:
            rows = conn.execute("SELECT id FROM messages ORDER BY seq").fetchall()
            return [m.__dict__ for m in get_messages(conn, [row["id"] for row in rows])]
        finally:
            conn.close()


@pytest.fixture
def bell(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_hls: FakeHls,
) -> Iterator[Bell]:
    table = {pid: Proc(pid, 1, STARTED) for pid in BELL_PIDS.values()}
    monkeypatch.setattr("bellhop.cli.context.process_table", lambda: table)
    world = Bell(tmp_path, capsys, monkeypatch, fake_hls, {"log": str(tmp_path / "pushes.jsonl")})
    world.sessions(
        world.record("claude", ALPHA, "alpha"),
        world.record("codex", BETA, "beta"),
        world.record("pi", DUP1, "dup"),
        world.record("pi", DUP2, "dup"),
    )
    world.route()
    yield world


def payload_message(payload: dict[str, object]) -> dict[str, object]:
    value = payload["message"]
    assert isinstance(value, dict)
    return value


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="bh-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def pong_line(session: str | None, *, wire: int = 2, transcript: str | None = None) -> bytes:
    fields = {"wire": wire, "session": session, "transcript": transcript}
    return f"ok pong {json.dumps(fields)}\n".encode()


@contextmanager
def pong_server(path: Path, reply: bytes) -> Iterator[None]:
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)

    def answer() -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = server.accept()
                with conn, contextlib.suppress(OSError):
                    while conn.recv(4096):
                        pass
                    conn.sendall(reply)

    threading.Thread(target=answer, daemon=True).start()
    try:
        yield
    finally:
        server.close()
