import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from bellhop.drain import lock_path
from bellhop.testing.run import SmokeRun

REAL_ENV = dict(os.environ)
REAL_HOME = Path(REAL_ENV.get("HOME", str(Path.home())))
REAL_HLS = REAL_HOME / ".local/share/hooklinesinker/bin/hooklinesinker"
CACHE = Path(__file__).resolve().parents[2] / ".cache" / "smoke"
CONFIRM = "120"

pytestmark = pytest.mark.smoke

type Cell = Callable[[], dict[str, object]]


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


def pi_rows(env: Mapping[str, str], root: Path) -> list[dict[str, object]]:
    _, payload = bellhop(env, "sessions")
    rows = payload.get("sessions")
    assert isinstance(rows, list)
    return [
        r for r in rows if r.get("harness") == "pi" and str(r.get("cwd", "")).startswith(str(root))
    ]


def wait_for(probe: Callable[[], object], timeout: float, every: float = 2.0) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(every)
    return None


def records(pi_dir: Path) -> list[tuple[Path, int, dict[str, object]]]:
    found = []
    for path in sorted((pi_dir / "sessions").rglob("*.jsonl")):
        for index, line in enumerate(path.read_text(errors="replace").splitlines()):
            try:
                found.append((path, index, json.loads(line)))
            except json.JSONDecodeError:
                continue
    return found


def text_of(record: Mapping[str, object]) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def role(record: Mapping[str, object]) -> str:
    message = record.get("message")
    return str(message.get("role")) if isinstance(message, dict) else ""


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


def test_bellhop_delivers_into_a_real_pi_and_confirms_from_its_transcript() -> None:
    run = SmokeRun(
        CACHE,
        REAL_ENV,
        sops_exec=REAL_HOME / "ac" / "bin" / "sops-exec",
        hls_source=REAL_HLS,
        codex_auth=REAL_HOME / ".codex" / "auth.json",
        user_home=REAL_HOME,
    )
    results: dict[str, dict[str, object]] = {}
    with run:
        env = run.env
        extension_dir = run.layout.root / "extension"
        assert bellhop(env, "integration", "export", "pi", "--to", str(extension_dir))[0] == 0
        run.launch("pi", "pi-cells", extra=["-e", str(extension_dir / "bellhop.ts")])
        run.wait_ready("pi-cells")
        try:
            results = run_cells(run, env)
        finally:
            stop_drainer(env)
            (run.layout.logs / "pi-cells.json").write_text(json.dumps(results, indent=2))
    assert {name: cell.get("ok") for name, cell in results.items()} == dict.fromkeys(
        ("idle-to-idle", "idle-to-working", "now-to-working", "hold-and-inbox", "s7", "s10"), True
    )
    assert run.leftovers == []
    assert not run.layout.runtime_dir.exists()


class PiCells:
    def __init__(self, run: SmokeRun, env: Mapping[str, str]) -> None:
        self.run = run
        self.env = env
        rows = wait_for(lambda: [r for r in self.rows() if r.get("reachable")], 120)
        assert isinstance(rows, list) and len(rows) == 1, f"pi not listed and reachable: {rows}"
        self.session_id = str(rows[0]["session_id"])
        self.first_file: Path | None = None

    def rows(self) -> list[dict[str, object]]:
        return pi_rows(self.env, self.run.layout.root)

    def pane(self) -> str:
        return self.run.tmux.capture("pi-cells")

    def type(self, text: str, *, enter: bool) -> None:
        self.run.tmux.type("pi-cells", text)
        if enter:
            time.sleep(0.5)
            self.run.tmux.keys("pi-cells", "Enter")

    def turn_open(self) -> bool:
        messages = [r for _, _, r in records(self.run.layout.pi_dir) if r.get("type") == "message"]
        last = messages[-1] if messages else None
        if last is None or role(last) == "system":
            return False
        message = last.get("message")
        stop = message.get("stopReason") if isinstance(message, dict) else None
        return role(last) != "assistant" or stop in ("toolUse", "pending", "deferred")

    def idle(self) -> bool:
        return bool(wait_for(lambda: not self.turn_open(), 180, 1.0))

    def busy_with(self, done: str) -> bool:
        self.type(
            f"Run the shell command `sleep 25` and then reply with exactly DONE {done}.", enter=True
        )
        time.sleep(8)
        return self.turn_open()

    def send(self, *args: str) -> tuple[int, dict[str, object]]:
        return bellhop(self.env, "send", self.session_id, *args, "--from", "smoke")

    def ack(self, tok: str, *args: str) -> tuple[int, str | None]:
        code, payload = self.send(f"Reply with exactly ACK {tok}.", *args, "--confirm", CONFIRM)
        state = message(payload).get("state")
        return code, str(state) if state is not None else None

    def position(
        self, predicate: Callable[[Mapping[str, object]], bool]
    ) -> tuple[Path, int] | None:
        return next(((p, i) for p, i, r in records(self.run.layout.pi_dir) if predicate(r)), None)

    def delivered_at(self, tok: str) -> tuple[Path, int] | None:
        return self.position(
            lambda r: (
                role(r) == "user"
                and text_of(r).lstrip().startswith("[bellhop")
                and tok in text_of(r)
            )
        )

    def answered_at(self, done: str) -> tuple[Path, int] | None:
        return self.position(lambda r: role(r) == "assistant" and f"DONE {done}" in text_of(r))

    def idle_to_idle(self) -> dict[str, object]:
        self.idle()
        tok = token()
        code, state = self.ack(tok)
        landed = self.delivered_at(tok)
        self.first_file = landed[0] if landed else None
        return {"ok": code == 0 and state == "confirmed", "state": state}

    def to_working(self, *args: str) -> dict[str, object]:
        self.idle()
        done = token()
        busy = self.busy_with(done)
        tok = token()
        code, state = self.ack(tok, *args)
        self.idle()
        landed, answered = self.delivered_at(tok), self.answered_at(done)
        return {
            "code": code,
            "state": state,
            "busy_at_send": busy,
            "boundary": landed is not None and answered is not None and landed[1] > answered[1],
            "mid_turn": landed is not None and (answered is None or landed[1] < answered[1]),
        }

    def idle_to_working(self) -> dict[str, object]:
        found = self.to_working()
        ok = (
            found["code"] == 0
            and found["state"] == "confirmed"
            and found["busy_at_send"] is True
            and found["boundary"] is True
        )
        return {"ok": ok, **found}

    def now_to_working(self) -> dict[str, object]:
        found = self.to_working("--urgency", "now")
        ok = (
            found["code"] == 0
            and found["state"] == "confirmed"
            and found["busy_at_send"] is True
            and found["mid_turn"] is True
        )
        return {"ok": ok, **found}

    def hold_and_inbox(self) -> dict[str, object]:
        self.idle()
        ack_tok = token()
        _, held = self.send(f"held note {token()}", "--urgency", "hold")
        code, state = self.ack(ack_tok)
        landed = self.position(
            lambda r: role(r) == "user" and ack_tok in text_of(r) and "1 more waiting" in text_of(r)
        )
        _, taken = bellhop(self.env, "inbox", "--target", self.session_id)
        items = taken.get("messages")
        ids = [m.get("id") for m in items] if isinstance(items, list) else []
        ok = (
            message(held).get("state") == "pending"
            and (code, state) == (0, "confirmed")
            and landed is not None
            and ids == [message(held).get("id")]
        )
        return {"ok": ok, "state": state, "waiting_line_seen": landed is not None, "inbox": ids}

    def s7(self) -> dict[str, object]:
        self.idle()
        draft = f"draft {token()} keep me"
        self.type(draft, enter=False)
        time.sleep(1.5)
        code, state = self.ack(token())
        self.idle()
        kept = draft in self.pane()
        submitted = self.position(lambda r: role(r) == "user" and draft.split()[1] in text_of(r))
        self.run.tmux.keys("pi-cells", "C-u")
        ok = (code, state) == (0, "confirmed") and kept and submitted is None
        return {
            "ok": ok,
            "state": state,
            "draft_kept": kept,
            "draft_submitted": submitted is not None,
        }

    def s10(self) -> dict[str, object]:
        self.idle()
        before = self.session_id
        self.type("/new", enter=True)
        rows = wait_for(
            lambda: [
                r for r in self.rows() if r.get("session_id") != before and r.get("reachable")
            ],
            60,
        )
        if not isinstance(rows, list) or len(rows) != 1:
            return {"ok": False, "detail": f"no new reachable pi session after /new: {rows}"}
        self.session_id = str(rows[0]["session_id"])
        stray = token()
        stray_code, stray_payload = bellhop(
            self.env, "send", before, f"Reply with exactly ACK {stray}.", "--from", "smoke"
        )
        tok = token()
        code, state = self.ack(tok)
        landed = self.delivered_at(tok)
        new_file = landed is not None and landed[0] != self.first_file
        self.idle()
        stray_landed = self.delivered_at(stray)
        ok = (code, state) == (0, "confirmed") and new_file and stray_landed is None
        return {
            "ok": ok,
            "state": state,
            "before": before,
            "after": self.session_id,
            "old_session_send": {
                "code": stray_code,
                "state": message(stray_payload).get("state"),
                "error": stray_payload.get("error"),
                "delivered": stray_landed is not None,
            },
        }


def run_cells(run: SmokeRun, env: Mapping[str, str]) -> dict[str, dict[str, object]]:
    cells = PiCells(run, env)
    named: dict[str, Cell] = {
        "idle-to-idle": cells.idle_to_idle,
        "idle-to-working": cells.idle_to_working,
        "now-to-working": cells.now_to_working,
        "hold-and-inbox": cells.hold_and_inbox,
        "s7": cells.s7,
        "s10": cells.s10,
    }
    results: dict[str, dict[str, object]] = {}
    for name, cell in named.items():
        try:
            results[name] = cell()
        except Exception as error:  # noqa: BLE001
            results[name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    cells.idle()
    return results
