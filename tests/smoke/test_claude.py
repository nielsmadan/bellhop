import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from smoke_helpers import (
    CONFIRM,
    Cell,
    NotBusy,
    Record,
    bellhop,
    harness_rows,
    jsonl_records,
    measured_reps,
    message,
    run_named,
    smoke_run,
    submit_until,
    text_of,
    token,
    wait_for,
)

from bellhop.envelope import BODY_LIMIT
from bellhop.testing.run import SmokeRun, stop_drainer
from bellhop.transcripts.claude import received_texts

pytestmark = pytest.mark.smoke

NAME = "cl-cells"
RELAY = Path(sys.executable).with_name("bellhop")
PERMISSION = re.compile(r"Do you want to (proceed|make this edit|create)", re.IGNORECASE)
QUESTION = re.compile(
    r"^\W*1\.\s+(Red|Blue)\b.*\n(?:.*\n)*?\W*2\.\s+(Red|Blue)\b", re.IGNORECASE | re.MULTILINE
)
WORKING = "esc to interrupt"
GATES = {"idle": "gate-idle.fifo", "now": "gate-now.fifo"}
CHILD_ENV_SCRIPT = "msgvars.sh"
WIDE = "日"
CHILD_ENV_PROBE = """for name in CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN; do
  eval "value=\\${$name-}"
  if [ -n "$value" ]; then echo "$name set"; else echo "$name unset"; fi
done
name=$(basename "${CLAUDE_CODE_MESSAGING_SOCKET:-none}")
number=${name%.sock}
case "$number" in
  ""|*[!0-9]*) echo "CLAUDE_CODE_SOCKET_NAME not-a-pid" ;;
  *)
    p=$$; found=no
    while [ -n "$p" ] && [ "$p" -gt 1 ]; do
      if [ "$p" = "$number" ]; then found=yes; fi
      p=$(ps -o ppid= -p "$p" | tr -d ' ')
    done
    echo "CLAUDE_CODE_SOCKET_NAME pid-of-an-ancestor=$found"
    ;;
esac
"""
ALLOWED = (*(f"cat {gate}" for gate in GATES.values()), f"sh {CHILD_ENV_SCRIPT}")


def ends_turn(record: Mapping[str, object]) -> bool:
    message = record.get("message")
    stop = message.get("stop_reason") if isinstance(message, dict) else None
    return record.get("type") == "assistant" and stop == "end_turn"


class ClaudeCells:
    def __init__(self, run: SmokeRun, env: Mapping[str, str]) -> None:
        self.run = run
        self.env = env
        rows = wait_for(lambda: [r for r in self.rows() if r.get("reachable")], 120)
        assert isinstance(rows, list) and len(rows) == 1, f"claude not reachable: {rows}"
        self.session_id = str(rows[0]["session_id"])
        self.first_file: str | None = None

    def rows(self) -> list[dict[str, object]]:
        return harness_rows(self.env, self.run.layout.root, "claude")

    def pane(self) -> str:
        return self.run.tmux.capture(NAME)

    def type(self, text: str, *, enter: bool) -> None:
        self.run.tmux.type(NAME, text)
        if enter:
            time.sleep(0.5)
            self.run.tmux.keys(NAME, "Enter")

    def records(self) -> list[Record]:
        found = jsonl_records(self.run.layout.claude_dir / "projects", "*.jsonl")
        return sorted(found, key=lambda r: (r[0].stat().st_mtime_ns, r[1]))

    def working(self) -> bool:
        return WORKING in self.pane().lower()

    def idle(self) -> bool:
        def settled() -> bool:
            if self.working():
                return False
            time.sleep(2)
            return not self.working()

        return bool(wait_for(settled, 240, 1.0))

    def busy_with(self, done: str, label: str) -> Path:
        gate = self.run.launched[NAME].work / GATES[label]
        gate.unlink(missing_ok=True)
        os.mkfifo(gate)
        prompt = (
            f"Run the shell command `cat {gate.name}` and wait for it to finish; it blocks until "
            f"it is released. Then reply with exactly DONE {done}."
        )
        if not submit_until(
            lambda: self.type(prompt, enter=True),
            lambda: self.run.tmux.keys(NAME, "Enter"),
            self.working,
        ):
            raise NotBusy
        return gate

    def release(self, gate: Path) -> None:
        def write() -> None:
            with gate.open("w") as handle:
                handle.write("go\n")

        threading.Thread(target=write, daemon=True).start()

    def send(self, *args: str) -> tuple[int, dict[str, object]]:
        return bellhop(self.env, "send", self.session_id, *args, "--from", "smoke")

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
        return self.position(lambda r: any(tok in text for text in received_texts(dict(r))))

    def queued(self, tok: str) -> bool:
        return any(
            r.get("type") == "queue-operation" and tok in str(r.get("content", ""))
            for _, _, r in self.records()
        )

    def turn_closed_after(self, start: int) -> int | None:
        return next(
            (i for i, (_, _, r) in enumerate(self.records()) if i > start and ends_turn(r)), None
        )

    def idle_to_idle(self) -> dict[str, object]:
        self.idle()
        tok = token()
        code, state, _ = self.ack(tok)
        landed = self.delivered_at(tok)
        self.first_file = self.records()[landed][0].name if landed is not None else None
        return {"ok": (code, state) == (0, "confirmed"), "state": state}

    def to_working(self, *args: str) -> dict[str, object]:
        self.idle()
        done = token()
        gate = self.busy_with(done, "now" if args else "idle")
        busy = True
        tok = token()
        code, result = self.send(f"Reply with exactly ACK {tok}.", *args)
        sent = message(result)
        state_at_send = sent.get("state")
        wait_for(lambda: self.queued(tok), 15, 0.5)
        self.release(gate)
        state = self.settled(sent)
        self.idle()
        asked = self.position(lambda r: r.get("type") == "user" and f"DONE {done}" in text_of(r))
        landed = self.delivered_at(tok)
        closed = self.turn_closed_after(asked) if asked is not None else None
        order = (
            (asked, landed, closed)
            if asked is not None and landed is not None and closed is not None
            else None
        )
        return {
            "code": code,
            "state_at_send": state_at_send,
            "state": state,
            "degraded_from": sent.get("degraded_from"),
            "detail": sent.get("detail"),
            "busy_at_send": busy,
            "boundary": order is not None and order[1] > order[2],
            "mid_turn": order is not None and order[0] < order[1] < order[2],
        }

    def working_cell(self, priority: str, landing: str, *args: str) -> dict[str, object]:
        found = self.to_working(*args)
        pushed_at_once = (found["state_at_send"], found["detail"]) == ("sent", priority)
        ok = (
            found["code"] == 0
            and found["state"] == "confirmed"
            and found["busy_at_send"]
            and found["degraded_from"] is None
            and pushed_at_once
        )
        return {"ok": ok and found[landing] is True, **found}

    def idle_to_working(self) -> dict[str, object]:
        return self.working_cell("later", "boundary")

    def now_to_working(self) -> dict[str, object]:
        return self.working_cell("next", "mid_turn", "--urgency", "now")

    def hold_and_inbox(self) -> dict[str, object]:
        self.idle()
        ack_tok = token()
        _, held = self.send(f"held note {token()}", "--urgency", "hold")
        code, state, _ = self.ack(ack_tok)
        landed = self.position(
            lambda r: any(
                ack_tok in text and "1 more waiting" in text for text in received_texts(dict(r))
            )
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
        code, state, _ = self.ack(token())
        self.idle()
        kept = draft in self.pane()
        submitted = self.position(
            lambda r: r.get("type") == "user" and draft.split()[1] in text_of(r)
        )
        self.run.tmux.keys(NAME, "C-u")
        ok = (code, state) == (0, "confirmed") and kept and submitted is None
        return {
            "ok": ok,
            "state": state,
            "draft_kept": kept,
            "draft_submitted": submitted is not None,
        }

    def dialog(
        self, prompt: str, shown: re.Pattern[str], label: str, *args: str
    ) -> dict[str, object]:
        self.idle()
        self.type(prompt, enter=True)
        if not wait_for(lambda: shown.search(self.pane()), 90, 1.0):
            self.run.save_pane(NAME, f"{label}-no-dialog")
            return {"measured": False}
        tok = token()
        code, result = self.send(f"Reply with exactly ACK {tok}.", *args)
        time.sleep(15)
        still = bool(shown.search(self.pane()))
        early = self.delivered_at(tok)
        created = (self.run.launched[NAME].work / "perm.txt").exists()
        self.run.save_pane(NAME, f"{label}-open")
        self.run.tmux.keys(NAME, "Escape")
        state = self.settled(message(result))
        self.idle()
        priority = "next" if "now" in args else "later"
        pushed = (message(result).get("state"), message(result).get("detail")) == ("sent", priority)
        return {
            "measured": True,
            "code": code,
            "pushed_while_open": pushed,
            "detail_at_send": message(result).get("detail"),
            "dialog_still_open": still,
            "delivered_while_open": early is not None,
            "file_created": created,
            "state": state,
            "safe": code == 0
            and pushed
            and still
            and early is None
            and not created
            and state == "confirmed",
        }

    def s3(self, *args: str) -> dict[str, object]:
        found = self.dialog(
            "Use the Write tool to create the file perm.txt containing hi. Do nothing else.",
            PERMISSION,
            f"S3{'-now' if args else ''}",
            *args,
        )
        return {"ok": found.get("safe") is True, **found}

    def s3_now(self) -> dict[str, object]:
        return self.s3("--urgency", "now")

    def s3b_rep(self) -> dict[str, object]:
        return self.dialog(
            "Use the AskUserQuestion tool to ask me exactly one question, 'Red or blue?', "
            "with the two options Red and Blue. Do nothing else.",
            QUESTION,
            "S3b",
            "--urgency",
            "now",
        )

    def s3b(self) -> dict[str, object]:
        return measured_reps(self.s3b_rep)

    def child_env(self) -> dict[str, object]:
        self.idle()
        script = self.run.launched[NAME].work / CHILD_ENV_SCRIPT
        script.write_text(CHILD_ENV_PROBE)
        self.type(
            f"Run the shell command `sh {CHILD_ENV_SCRIPT}` and reply with its output only.",
            enter=True,
        )
        found = wait_for(lambda: self.tool_output_with("CLAUDE_CODE_MESSAGING_TOKEN"), 90, 1.0)
        self.idle()
        lines = str(found or "").splitlines()
        seen = dict(line.split(" ", 1) for line in lines if line.startswith("CLAUDE_CODE_"))
        return {
            "ok": {"CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN"} <= set(seen),
            "measurement": "is the messaging channel set in a child of Claude's Bash tool",
            **seen,
        }

    def tool_output_with(self, text: str) -> str | None:
        for _, _, record in self.records():
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                body = block.get("content") if isinstance(block, dict) else None
                if block.get("type") == "tool_result" and text in str(body):
                    return str(body)
        return None

    def max_body(self) -> dict[str, object]:
        self.idle()
        tok = token()
        head = f"Reply with exactly ACK {tok}. "
        filler = WIDE * (BODY_LIMIT - len(head))
        code, result = self.send(head + filler, "--confirm", CONFIRM)
        state = self.settled(message(result))
        landed = next(
            (text for _, _, r in self.records() for text in received_texts(dict(r)) if tok in text),
            "",
        )
        whole = landed.count(WIDE) == len(filler)
        self.idle()
        return {
            "ok": code == 0 and state == "confirmed" and whole,
            "characters": BODY_LIMIT,
            "utf8_bytes": len((head + filler).encode()),
            "state": state,
            "arrived_whole": whole,
        }

    def s10(self) -> dict[str, object]:
        self.idle()
        before = self.session_id
        self.type("/clear", enter=True)
        rows = wait_for(
            lambda: [
                r for r in self.rows() if r.get("session_id") != before and r.get("reachable")
            ],
            60,
        )
        if not isinstance(rows, list) or len(rows) != 1:
            return {"ok": False, "detail": f"no new reachable claude session after /clear: {rows}"}
        self.session_id = str(rows[0]["session_id"])
        stray = token()
        stray_code, stray_payload = bellhop(
            self.env, "send", before, f"Reply with exactly ACK {stray}.", "--from", "smoke"
        )
        tok = token()
        code, state, _ = self.ack(tok)
        landed = self.delivered_at(tok)
        new_file = landed is not None and self.records()[landed][0].name != self.first_file
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
            "old_session_send": {"code": stray_code, "error": refused},
        }


def run_cells(run: SmokeRun, env: Mapping[str, str]) -> dict[str, dict[str, object]]:
    cells = ClaudeCells(run, env)
    named: dict[str, Cell] = {
        "idle-to-idle": cells.idle_to_idle,
        "idle-to-working": cells.idle_to_working,
        "now-to-working": cells.now_to_working,
        "hold-and-inbox": cells.hold_and_inbox,
        "s7": cells.s7,
        "s3": cells.s3,
        "s3-now": cells.s3_now,
        "s3b": cells.s3b,
        "child-env": cells.child_env,
        "max-body": cells.max_body,
        "s10": cells.s10,
    }
    results = run_named(named)
    cells.idle()
    return results


def test_bellhop_delivers_into_a_real_claude_through_its_relay() -> None:
    run = smoke_run(claude_allowed=ALLOWED)
    results: dict[str, dict[str, object]] = {}
    with run:
        env = run.env
        exported = run.layout.root / "claude-integration"
        assert bellhop(env, "integration", "export", "claude", "--to", str(exported))[0] == 0
        run.launch(
            "claude",
            NAME,
            extra=["--mcp-config", str(exported / "bellhop-mcp.json"), "--strict-mcp-config", "--"],
            prompt="Reply with the single word OK.",
            env={"PATH": f"{RELAY.parent}:{env['PATH']}"},
        )
        run.wait_ready(NAME)
        try:
            results = run_cells(run, env)
        finally:
            stop_drainer(env)
            (run.layout.logs / "claude-cells.json").write_text(json.dumps(results, indent=2))
    assert {name: cell.get("ok") for name, cell in results.items()} == dict.fromkeys(results, True)
    assert set(results) == {
        "idle-to-idle",
        "idle-to-working",
        "now-to-working",
        "hold-and-inbox",
        "s7",
        "s3",
        "s3-now",
        "s3b",
        "child-env",
        "max-body",
        "s10",
    }
    assert run.leftovers == []
