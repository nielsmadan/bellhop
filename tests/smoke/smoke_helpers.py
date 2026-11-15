import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from bellhop.testing.run import SmokeRun
from bellhop.transcripts.common import content_text

REAL_ENV = dict(os.environ)
REAL_HOME = Path(REAL_ENV.get("HOME", str(Path.home())))
REAL_HLS = REAL_HOME / ".local/share/hooklinesinker/bin/hooklinesinker"
CACHE = Path(__file__).resolve().parents[2] / ".cache" / "smoke"
CONFIRM = "120"

type Cell = Callable[[], dict[str, object]]


class NotBusy(Exception):
    def __init__(self) -> None:
        super().__init__("the busy turn never opened; nothing was sent")


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


def text_of(record: Mapping[str, object]) -> str:
    message = record.get("message")
    return content_text(message.get("content")) if isinstance(message, dict) else ""


def jsonl_records(root: Path, pattern: str) -> list[Record]:
    found: list[Record] = []
    for path in sorted(root.rglob(pattern)):
        for index, line in enumerate(path.read_text(errors="replace").splitlines()):
            try:
                found.append((path, index, json.loads(line)))
            except json.JSONDecodeError:
                continue
    return found


def measured_reps(rep: Cell, wanted: int = 3, attempts: int = 5) -> dict[str, object]:
    reps: list[dict[str, object]] = []
    while len([r for r in reps if r.get("measured")]) < wanted and len(reps) < attempts:
        reps.append(rep())
    measured = [r for r in reps if r.get("measured")]
    safe = len(measured) == wanted and all(r.get("safe") for r in measured)
    return {"ok": safe, "measured": len(measured), "reps": reps}


def run_named(cells: Mapping[str, Cell]) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for name, cell in cells.items():
        try:
            results[name] = cell()
        except Exception as error:  # noqa: BLE001
            results[name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return results


def submit_until(
    type_prompt: Callable[[], None],
    press_enter: Callable[[], None],
    opened: Callable[[], object],
    tries: int = 6,
) -> bool:
    type_prompt()
    for _ in range(tries):
        if wait_for(opened, 10, 0.5):
            return True
        press_enter()
    return False


def wait_for(probe: Callable[[], object], timeout: float, every: float = 2.0) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(every)
    return None


def smoke_run(
    codex_features: tuple[str, ...] = (), claude_allowed: tuple[str, ...] = ()
) -> SmokeRun:
    return SmokeRun(
        CACHE,
        REAL_ENV,
        sops_exec=REAL_HOME / "ac" / "bin" / "sops-exec",
        hls_source=REAL_HLS,
        codex_auth=REAL_HOME / ".codex" / "auth.json",
        user_home=REAL_HOME,
        codex_features=codex_features,
        claude_allowed=claude_allowed,
    )
