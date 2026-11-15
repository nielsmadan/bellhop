import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from smoke_helpers import (
    CONFIRM,
    Cell,
    NotBusy,
    bellhop,
    harness_rows,
    jsonl_records,
    message,
    run_named,
    smoke_run,
    submit_until,
    text_of,
    token,
    wait_for,
)

from bellhop.testing.run import SmokeRun, stop_drainer

pytestmark = pytest.mark.smoke


def records(pi_dir: Path) -> list[tuple[Path, int, dict[str, object]]]:
    return jsonl_records(pi_dir / "sessions", "*.jsonl")


def role(record: Mapping[str, object]) -> str:
    message = record.get("message")
    return str(message.get("role")) if isinstance(message, dict) else ""


def test_bellhop_delivers_into_a_real_pi_and_confirms_from_its_transcript() -> None:
    run = smoke_run()
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
        return harness_rows(self.env, self.run.layout.root, "pi")

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
        prompt = f"Run the shell command `sleep 25` and then reply with exactly DONE {done}."
        if not submit_until(
            lambda: self.type(prompt, enter=True),
            lambda: self.run.tmux.keys("pi-cells", "Enter"),
            self.turn_open,
        ):
            raise NotBusy
        return True

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
        refused = stray_payload.get("error")
        refused_code = refused.get("code") if isinstance(refused, dict) else None
        ok = (
            (code, state) == (0, "confirmed")
            and new_file
            and (stray_code, refused_code) in {(3, "target_not_found"), (4, "no_route")}
        )
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
    results = run_named(named)
    cells.idle()
    return results
