import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

FAKE_HLS = Path(__file__).resolve().parent / "fakes" / "hooklinesinker"


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
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    monkeypatch.delenv("BELLHOP_HOOKLINESINKER", raising=False)
    monkeypatch.delenv("BELLHOP_RUNTIME_DIR", raising=False)
    for name in [name for name in os.environ if name.startswith("FAKE_HLS_")]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def fake_hls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHls:
    directory = tmp_path / "fake-hls"
    directory.mkdir()
    monkeypatch.setenv("BELLHOP_HOOKLINESINKER", str(FAKE_HLS))
    return FakeHls(directory, monkeypatch)
