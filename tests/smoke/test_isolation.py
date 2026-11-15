import json
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import pytest
from smoke_helpers import REAL_ENV, REAL_HLS, smoke_run

from bellhop.testing.run import SMOKE_SOCKET

PROMPT = "Reply with the single word OK."
LISTED_WITHIN_SECONDS = 90.0

pytestmark = pytest.mark.smoke


def _real_hls(*args: str) -> dict[str, object]:
    result = subprocess.run(
        [str(REAL_HLS), *args, "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        env=REAL_ENV,
        check=True,
    )
    payload: dict[str, object] = json.loads(result.stdout)
    return payload


def _real_consumers() -> object:
    return _real_hls("consumers").get("consumers")


def _real_sessions_under(root: Path) -> list[object]:
    sessions = _real_hls("sessions").get("sessions")
    assert isinstance(sessions, list)
    return [session for session in sessions if str(root) in json.dumps(session)]


def _bellhop_sessions(env: Mapping[str, str]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "bellhop", "sessions", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(env),
        check=True,
    )
    payload: dict[str, object] = json.loads(result.stdout)
    return payload


def _listed(env: Mapping[str, str], root: Path) -> list[dict[str, object]]:
    sessions = _bellhop_sessions(env)["sessions"]
    assert isinstance(sessions, list)
    return [s for s in sessions if str(s.get("cwd", "")).startswith(str(root))]


def test_isolated_claude_codex_and_pi_start_appear_and_tear_down_cleanly() -> None:
    consumers_before = _real_consumers()
    run = smoke_run()
    assert run.socket == SMOKE_SOCKET == "bellhop-smoke"
    with run:
        for agent in ("claude", "codex", "pi"):
            run.launch(agent, f"iso-{agent}", prompt=PROMPT)
        for agent in ("claude", "codex", "pi"):
            run.wait_ready(f"iso-{agent}")
        deadline = time.monotonic() + LISTED_WITHIN_SECONDS
        listed = _listed(run.env, run.layout.root)
        while len(listed) < 3 and time.monotonic() < deadline:
            time.sleep(2)
            listed = _listed(run.env, run.layout.root)
        assert sorted(str(s["harness"]) for s in listed) == ["claude", "codex", "pi"]
        assert _real_sessions_under(run.layout.root) == []
    assert run.leftovers == []
    assert run.tmux.server_alive() is False
    assert not run.layout.runtime_dir.exists()
    assert _real_sessions_under(run.layout.root) == []
    assert _real_consumers() == consumers_before
