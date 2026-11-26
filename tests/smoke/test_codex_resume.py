import json
import sys
import time
from collections.abc import Mapping

import pytest
from smoke_helpers import (
    CONFIRM,
    bellhop,
    harness_rows,
    jsonl_records,
    message,
    smoke_run,
    token,
    wait_for,
)

from bellhop.testing.run import SmokeRun, stop_drainer

pytestmark = pytest.mark.smoke

FIRST = "co-first"
WRAPPER = [sys.executable, "-m", "bellhop", "codex"]
STAYS_UP_SECONDS = 15
LISTED_SECONDS = 90
FAILED_RESUME = "Permission overrides are not supported"


def payload(record: Mapping[str, object]) -> dict[str, object]:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


class Resumes:
    def __init__(self, run: SmokeRun, env: Mapping[str, str]) -> None:
        self.run = run
        self.env = env
        self.path = f"{run.binary('codex').path.parent}:{env['PATH']}"

    def rows(self) -> list[dict[str, object]]:
        return harness_rows(self.env, self.run.layout.root, "codex")

    def listed(self, thread: str) -> bool:
        return any(r.get("session_id") == thread and r.get("reachable") for r in self.rows())

    def sandboxes(self, thread: str) -> list[object]:
        sessions = self.run.layout.codex_dir / "sessions"
        return [
            payload(r).get("sandbox_policy")
            for p, _, r in jsonl_records(sessions, "rollout-*.jsonl")
            if thread in p.name and r.get("type") == "turn_context"
        ]

    def first_thread(self) -> str:
        self.run.launch("codex", FIRST, prompt="Reply with the single word OK.")
        self.run.wait_ready(FIRST)
        rows = wait_for(lambda: [r for r in self.rows() if r.get("reachable")], 120)
        assert isinstance(rows, list) and len(rows) == 1, f"codex not listed: {rows}"
        thread = str(rows[0]["session_id"])
        wait_for(lambda: self.sandboxes(thread), 120)
        time.sleep(5)
        self.run.tmux.kill_session(FIRST)
        return thread

    def cell(self, name: str, arguments: list[str], thread: str) -> dict[str, object]:
        work = self.run.launched[FIRST].work
        started = len(self.sandboxes(thread))
        self.run.launch(
            "codex",
            name,
            extra=["-C", str(work), *arguments],
            wrapper=WRAPPER,
            env={"PATH": self.path},
        )
        time.sleep(STAYS_UP_SECONDS)
        pane = self.run.tmux.capture(name) if self.run.tmux.alive(name) else ""
        result: dict[str, object] = {
            "arguments": arguments,
            "up": self.run.tmux.alive(name) and FAILED_RESUME not in pane,
        }
        if not result["up"] or arguments[-2] == "fork":
            self.run.save_pane(name, "after")
            return result | {"ok": bool(result["up"])}
        listed = bool(wait_for(lambda: self.listed(thread), LISTED_SECONDS))
        result["listed"] = listed
        if listed:
            code, sent = bellhop(
                self.env,
                "send",
                thread,
                f"Reply with exactly ACK {token()}.",
                "--from",
                "smoke",
                "--confirm",
                CONFIRM,
            )
            result |= {"send_exit": code, "state": message(sent).get("state")}
        result["sandboxes_after_resume"] = self.sandboxes(thread)[started:]
        self.run.save_pane(name, "after")
        self.run.tmux.kill_session(name)
        return result | {"ok": result.get("state") == "confirmed"}


def test_a_resumed_or_forked_codex_through_bellhop_stays_up_and_reachable() -> None:
    run = smoke_run()
    results: dict[str, object] = {}
    with run:
        env = run.env
        cells = Resumes(run, env)
        try:
            thread = cells.first_thread()
            results["first_sandboxes"] = cells.sandboxes(thread)
            results["resume-s"] = cells.cell(
                "co-resume-s", ["-s", "workspace-write", "resume", thread], thread
            )
            results["resume-c"] = cells.cell(
                "co-resume-c",
                [
                    "-c",
                    'sandbox_mode="workspace-write"',
                    "-c",
                    'approval_policy="on-request"',
                    "resume",
                    thread,
                ],
                thread,
            )
            results["fork-s"] = cells.cell(
                "co-fork-s", ["-s", "workspace-write", "fork", thread], thread
            )
        finally:
            stop_drainer(env)
            (run.layout.logs / "resume-cells.json").write_text(json.dumps(results, indent=2))
    cells_ok = {name: cell.get("ok") for name, cell in results.items() if isinstance(cell, dict)}
    assert cells_ok == {"resume-s": True, "resume-c": True, "fork-s": True}
    assert run.leftovers == []
