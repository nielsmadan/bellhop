import json
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from smoke_helpers import (
    CONFIRM,
    Cell,
    bellhop,
    harness_rows,
    jsonl_records,
    message,
    run_named,
    smoke_run,
    stop_drainer,
    token,
    wait_for,
)

from bellhop.testing.run import SmokeRun

pytestmark = pytest.mark.smoke

NAME = "co-cells"
PERMISSION = re.compile(
    r"Would you like to (run|make)|approve|Allow command|Yes, proceed", re.IGNORECASE
)
QUESTION = re.compile(
    r"^\W*1\.\s+(Red|Blue)\b.*\n(?:.*\n)*?\W*2\.\s+(Red|Blue)\b", re.IGNORECASE | re.MULTILINE
)
TASK_EVENTS = {"task_started": True, "task_complete": False, "turn_aborted": False}
QUESTION_FEATURE = "default_mode_request_user_input"
S3B_REPS = 3
AGENT = "cx"


def payload(record: Mapping[str, object]) -> dict[str, object]:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def said(record: Mapping[str, object], role: str) -> str:
    body = payload(record)
    if record.get("type") != "response_item" or body.get("type") != "message":
        return ""
    if body.get("role") != role:
        return ""
    content = body.get("content")
    blocks = content if isinstance(content, list) else []
    return "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict))


class CodexCells:
    def __init__(self, run: SmokeRun, env: Mapping[str, str]) -> None:
        self.run = run
        self.env = env
        rows = wait_for(lambda: [r for r in self.rows() if r.get("reachable")], 120)
        assert isinstance(rows, list) and len(rows) == 1, f"codex not listed and reachable: {rows}"
        self.thread = str(rows[0]["session_id"])

    def rows(self) -> list[dict[str, object]]:
        return harness_rows(self.env, self.run.layout.root, "codex")

    def pane(self) -> str:
        return self.run.tmux.capture(NAME)

    def type(self, text: str, *, enter: bool) -> None:
        self.run.tmux.type(NAME, text)
        if enter:
            time.sleep(0.5)
            self.run.tmux.keys(NAME, "Enter")

    def records(self) -> list[tuple[Path, int, dict[str, object]]]:
        sessions = self.run.layout.codex_dir / "sessions"
        return [r for r in jsonl_records(sessions, "rollout-*.jsonl") if self.thread in r[0].name]

    def turn_open(self) -> bool:
        events = [
            TASK_EVENTS[str(payload(r).get("type"))]
            for _, _, r in self.records()
            if r.get("type") == "event_msg" and payload(r).get("type") in TASK_EVENTS
        ]
        return bool(events) and events[-1]

    def turn_closed_after(self, start: int) -> int | None:
        return next(
            (
                index
                for index, (_, _, r) in enumerate(self.records())
                if index > start
                and r.get("type") == "event_msg"
                and TASK_EVENTS.get(str(payload(r).get("type"))) is False
            ),
            None,
        )

    def idle(self) -> bool:
        return bool(wait_for(lambda: not self.turn_open(), 240, 1.0))

    def busy_with(self, done: str) -> bool:
        self.type(
            f"Run the shell command `sleep 25` and then reply with exactly DONE {done}.", enter=True
        )
        time.sleep(10)
        return self.turn_open()

    def send(self, *args: str) -> tuple[int, dict[str, object]]:
        return bellhop(self.env, "send", self.thread, *args, "--from", "smoke")

    def settled(self, sent: Mapping[str, object]) -> str | None:
        state = sent.get("state")
        if state not in {"pending", "sent"}:
            return str(state) if state is not None else None
        ident = str(sent.get("id"))

        def confirmed() -> str | None:
            _, status = bellhop(self.env, "status", ident)
            rows = status.get("messages")
            current = rows[0].get("state") if isinstance(rows, list) and rows else None
            return str(current) if current == "confirmed" else None

        found = wait_for(confirmed, 240, 3.0)
        return str(found) if found else str(state)

    def ack(self, tok: str, *args: str) -> tuple[int, str | None, dict[str, object]]:
        code, result = self.send(f"Reply with exactly ACK {tok}.", *args, "--confirm", CONFIRM)
        sent = message(result)
        return code, self.settled(sent), sent

    def position(self, predicate: Callable[[Mapping[str, object]], bool]) -> int | None:
        return next((i for i, (_, _, r) in enumerate(self.records()) if predicate(r)), None)

    def delivered_at(self, tok: str) -> int | None:
        return self.position(
            lambda r: said(r, "user").lstrip().startswith("[bellhop") and tok in said(r, "user")
        )

    def answered_at(self, done: str) -> int | None:
        return self.position(lambda r: f"DONE {done}" in said(r, "assistant"))

    def idle_to_idle(self) -> dict[str, object]:
        self.idle()
        code, state, _ = self.ack(token())
        return {"ok": (code, state) == (0, "confirmed"), "state": state}

    def to_working(self, *args: str) -> dict[str, object]:
        self.idle()
        done = token()
        busy = self.busy_with(done)
        tok = token()
        code, state, sent = self.ack(tok, *args)
        self.idle()
        asked = self.position(lambda r: f"DONE {done}" in said(r, "user"))
        landed = self.delivered_at(tok)
        closed = self.turn_closed_after(asked) if asked is not None else None
        order = (
            (asked, landed, closed)
            if asked is not None and landed is not None and closed is not None
            else None
        )
        return {
            "code": code,
            "state": state,
            "degraded_from": sent.get("degraded_from"),
            "detail": sent.get("detail"),
            "busy_at_send": busy,
            "answered": self.answered_at(done) is not None,
            "boundary": order is not None and order[1] > order[2],
            "mid_turn": order is not None and order[0] < order[1] < order[2],
        }

    def idle_to_working(self) -> dict[str, object]:
        found = self.to_working()
        ok = found["code"] == 0 and found["state"] == "confirmed"
        return {"ok": ok and found["busy_at_send"] is True and found["boundary"] is True, **found}

    def now_to_working(self) -> dict[str, object]:
        found = self.to_working("--urgency", "now")
        ok = found["code"] == 0 and found["state"] == "confirmed" and found["busy_at_send"]
        steered = str(found["detail"]).startswith("turn/steer")
        undegraded = found["degraded_from"] is None
        return {"ok": ok and steered and undegraded and found["mid_turn"] is True, **found}

    def hold_and_inbox(self) -> dict[str, object]:
        self.idle()
        ack_tok = token()
        _, held = self.send(f"held note {token()}", "--urgency", "hold")
        code, state, _ = self.ack(ack_tok)
        landed = self.position(
            lambda r: ack_tok in said(r, "user") and "1 more waiting" in said(r, "user")
        )
        _, taken = bellhop(self.env, "inbox", "--target", self.thread)
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
        code, state, _ = self.ack(token())
        self.idle()
        kept = draft in self.pane()
        submitted = self.position(lambda r: draft.split()[1] in said(r, "user"))
        self.run.tmux.keys(NAME, "C-u")
        ok = (code, state) == (0, "confirmed") and kept and submitted is None
        return {
            "ok": ok,
            "state": state,
            "draft_kept": kept,
            "draft_submitted": submitted is not None,
        }

    def s3(self, *args: str) -> dict[str, object]:
        self.idle()
        self.type(
            "Use the shell to run this exact command and nothing else: echo hi > perm.txt. The "
            "sandbox is read-only, so request approval to run it outside the sandbox with "
            "escalated permissions.",
            enter=True,
        )
        shown = wait_for(lambda: PERMISSION.search(self.pane()), 90, 1.0)
        if not shown:
            self.run.save_pane(NAME, "S3-no-dialog")
            return {"ok": False, "detail": "the permission prompt never appeared"}
        tok = token()
        code, result = self.send(f"Reply with exactly ACK {tok}.", *args)
        time.sleep(15)
        still = bool(PERMISSION.search(self.pane()))
        created = (self.run.launched[NAME].work / "perm.txt").exists()
        self.run.save_pane(NAME, f"S3{'-now' if args else ''}-after")
        self.run.tmux.keys(NAME, "Escape")
        state = self.settled(message(result))
        waited = message(result).get("state") == "pending"
        return {
            "ok": code == 0 and waited and still and not created and state == "confirmed",
            "code": code,
            "state_at_send": message(result).get("state"),
            "detail_at_send": message(result).get("detail"),
            "prompt_still_open": still,
            "file_created": created,
            "state": state,
        }

    def s3_idle(self) -> dict[str, object]:
        return self.s3()

    def s3_now(self) -> dict[str, object]:
        return self.s3("--urgency", "now")

    def binding_state(self) -> object:
        _, listed = bellhop(self.env, "agents")
        rows = listed.get("agents")
        found = [a for a in rows if a.get("agent") == AGENT] if isinstance(rows, list) else []
        binding = found[0].get("binding") if found else None
        return binding.get("state") if isinstance(binding, dict) else None

    def daemon_restart(self) -> dict[str, object]:
        self.idle()
        bound, _ = bellhop({**self.env, "CODEX_THREAD_ID": self.thread}, "bind", AGENT)
        restarted = self.run.codex("app-server", "daemon", "restart", check=False).returncode
        dropped = wait_for(lambda: self.binding_state() == "failed", 60)
        self.type("Reply with the single word AGAIN.", enter=True)
        revived = wait_for(lambda: self.binding_state() == "live", 120)
        self.idle()
        tok = token()
        code, sent = bellhop(
            self.env,
            "send",
            AGENT,
            f"Reply with exactly ACK {tok}.",
            "--from",
            "smoke",
            "--confirm",
            CONFIRM,
        )
        state = self.settled(message(sent))
        ok = (bound, restarted, code, state) == (0, 0, 0, "confirmed") and bool(revived)
        return {
            "ok": ok,
            "binding_failed_after_restart": bool(dropped),
            "binding_live_after_a_turn": bool(revived),
            "state": state,
        }

    def s10(self) -> dict[str, object]:
        self.idle()
        before = self.thread
        self.type("/new", enter=True)
        time.sleep(8)
        listed = [str(r["session_id"]) for r in self.rows() if r.get("reachable")]
        tok = token()
        _, state, _ = self.ack(tok)
        landed = self.position(lambda r: tok in said(r, "user"))
        return {
            "ok": None,
            "known_limit": "hls keeps the old thread after /new (ADR 0002)",
            "listed_after": listed,
            "new_thread_listed": any(t != before for t in listed),
            "state": state,
            "landed_in_old_thread": landed is not None,
        }

    def s3b_rep(self) -> dict[str, object]:
        self.idle()
        self.type(
            "Use the request_user_input tool to ask me exactly one question, 'Red or blue?', with "
            "the two options Red and Blue. Do nothing else.",
            enter=True,
        )
        shown = wait_for(lambda: QUESTION.search(self.pane()), 90, 1.0)
        if not shown:
            self.run.save_pane(NAME, "S3b-no-dialog")
            return {"measured": False, "detail": "Codex opened no question dialog"}
        self.run.save_pane(NAME, "S3b-dialog")
        code, result = self.send(f"Reply with exactly ACK {token()}.", "--urgency", "now")
        time.sleep(15)
        still = bool(QUESTION.search(self.pane()))
        self.run.save_pane(NAME, "S3b-after")
        answered = self.position(
            lambda r: (
                str(payload(r).get("type", "")).endswith("_output")
                and re.search(r"\b(Red|Blue)\b", str(payload(r).get("output", ""))) is not None
            )
        )
        self.run.tmux.keys(NAME, "Escape")
        state = self.settled(message(result))
        self.idle()
        waited = code == 0 and message(result).get("state") == "pending"
        return {
            "measured": True,
            "state_at_send": message(result).get("state"),
            "detail_at_send": message(result).get("detail"),
            "dialog_still_open": still,
            "dialog_answered": answered is not None,
            "state": state,
            "safe": waited and still and answered is None and state == "confirmed",
        }

    def s3b(self) -> dict[str, object]:
        reps = [self.s3b_rep() for _ in range(S3B_REPS)]
        measured = [rep for rep in reps if rep.get("measured")]
        safe = bool(measured) and all(rep.get("safe") for rep in measured)
        return {"ok": safe, "measured": len(measured), "reps": reps}


def run_cells(run: SmokeRun, env: Mapping[str, str]) -> dict[str, dict[str, object]]:
    cells = CodexCells(run, env)
    named: dict[str, Cell] = {
        "idle-to-idle": cells.idle_to_idle,
        "idle-to-working": cells.idle_to_working,
        "now-to-working": cells.now_to_working,
        "hold-and-inbox": cells.hold_and_inbox,
        "s7": cells.s7,
        "s3": cells.s3_idle,
        "s3-now": cells.s3_now,
        "s3b": cells.s3b,
        "daemon-restart": cells.daemon_restart,
        "s10": cells.s10,
    }
    results = run_named(named)
    cells.idle()
    return results


def test_bellhop_delivers_into_a_real_codex_on_its_daemon() -> None:
    run = smoke_run(codex_features=(QUESTION_FEATURE,))
    results: dict[str, dict[str, object]] = {}
    with run:
        env = run.env
        run.launch("codex", NAME, prompt="Reply with the single word OK.")
        run.wait_ready(NAME)
        try:
            results = run_cells(run, env)
        finally:
            stop_drainer(env)
            (run.layout.logs / "codex-cells.json").write_text(json.dumps(results, indent=2))
    asserted = (
        "idle-to-idle",
        "idle-to-working",
        "now-to-working",
        "hold-and-inbox",
        "s7",
        "s3",
        "s3-now",
        "s3b",
        "daemon-restart",
    )
    assert {name: results[name].get("ok") for name in asserted} == dict.fromkeys(asserted, True)
    assert run.leftovers == []
    assert not run.layout.runtime_dir.exists()
