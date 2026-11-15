import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from bellhop.drain import lock_path
from bellhop.testing.run import SmokeRun

REAL_ENV = dict(os.environ)
REAL_HOME = Path(REAL_ENV.get("HOME", str(Path.home())))
REAL_HLS = REAL_HOME / ".local/share/hooklinesinker/bin/hooklinesinker"
CACHE = Path(__file__).resolve().parents[2] / ".cache" / "smoke"
CONFIRM = "120"

type Cell = Callable[[], dict[str, object]]
type Record = tuple[Path, int, dict[str, object]]


def token() -> str:
    return secrets.token_hex(4)


def bellhop(env: Mapping[str, str], *args: str) -> tuple[int, dict[str, object]]:
    result = subprocess.run(
        [sys.executable, "-m", "bellhop", *args, "--json"],
        capture_output=True,
        text=True,
        timeout=200,
        env=dict(env),
        check=False,
    )
    payload: dict[str, object] = json.loads(result.stdout)
    return result.returncode, payload


def message(payload: Mapping[str, object]) -> dict[str, object]:
    value = payload.get("message")
    return value if isinstance(value, dict) else {}


def harness_rows(env: Mapping[str, str], root: Path, harness: str) -> list[dict[str, object]]:
    _, payload = bellhop(env, "sessions")
    rows = payload.get("sessions")
    assert isinstance(rows, list)
    return [
        r
        for r in rows
        if r.get("harness") == harness and str(r.get("cwd", "")).startswith(str(root))
    ]


def jsonl_records(root: Path, pattern: str) -> list[Record]:
    found: list[Record] = []
    for path in sorted(root.rglob(pattern)):
        for index, line in enumerate(path.read_text(errors="replace").splitlines()):
            try:
                found.append((path, index, json.loads(line)))
            except json.JSONDecodeError:
                continue
    return found


def run_named(cells: Mapping[str, Cell]) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for name, cell in cells.items():
        try:
            results[name] = cell()
        except Exception as error:  # noqa: BLE001
            results[name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return results


def wait_for(probe: Callable[[], object], timeout: float, every: float = 2.0) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(every)
    return None


def stop_drainer(env: Mapping[str, str]) -> None:
    lock = lock_path(env)
    text = lock.read_text().strip() if lock.exists() else ""
    if not text.isdigit():
        return
    pid = int(text)
    command = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    ).stdout
    if "bellhop _drain" not in command:
        return
    os.kill(pid, signal.SIGTERM)
    gone = wait_for(
        lambda: subprocess.run(["kill", "-0", str(pid)], check=False).returncode != 0, 10, 0.2
    )
    assert gone, f"the drainer {pid} outlived SIGTERM"


def smoke_run(codex_features: tuple[str, ...] = ()) -> SmokeRun:
    return SmokeRun(
        CACHE,
        REAL_ENV,
        sops_exec=REAL_HOME / "ac" / "bin" / "sops-exec",
        hls_source=REAL_HLS,
        codex_auth=REAL_HOME / ".codex" / "auth.json",
        user_home=REAL_HOME,
        codex_features=codex_features,
    )
