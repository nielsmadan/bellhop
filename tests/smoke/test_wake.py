import json
import os
import signal
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from smoke_helpers import CONFIRM, bellhop, message, run_named, smoke_run, token, wait_for

from bellhop.testing.run import SmokeRun, ps, stop_drainer

pytestmark = pytest.mark.smoke

SETTLED = 150
IDLE_WAIT = 90
FIRST_PROMPT = {
    "enki": "Reply with just the word {word}; nothing else;",
    "nanna": "Count from 1 to 150, one number per line, then the word {word}; nothing else;",
    "utu": "Reply with just the word {word}; nothing else;",
}
NON_EXEC_SERVER = '"$0" app-server "$@"; status=$?; exit "$status"'


def agent_row(env: Mapping[str, str], name: str) -> dict[str, object]:
    _, payload = bellhop(env, "agents")
    rows = payload.get("agents")
    assert isinstance(rows, list)
    row: dict[str, object] = next((r for r in rows if r.get("agent") == name), {})
    binding = row.get("binding")
    return binding if isinstance(binding, dict) else {}


def settled(env: Mapping[str, str], name: str) -> dict[str, object] | None:
    binding = agent_row(env, name)
    return binding if binding.get("state") in ("live", "failed") else None


def phase(env: Mapping[str, str], session_id: object) -> object:
    _, payload = bellhop(env, "sessions")
    rows = payload.get("sessions")
    rows = rows if isinstance(rows, list) else []
    return next((r.get("phase") for r in rows if r.get("session_id") == session_id), None)


def confirmed(env: Mapping[str, str], message_id: object) -> dict[str, object] | None:
    _, payload = bellhop(env, "status", str(message_id))
    rows = payload.get("messages")
    row = rows[0] if isinstance(rows, list) and rows else {}
    return row if isinstance(row, dict) and row.get("state") == "confirmed" else None


def arrived_whole(root: Path, text: str) -> bool:
    encoded = json.dumps(text)[1:-1]
    return any(encoded + '"' in path.read_text(errors="replace") for path in root.rglob("*.jsonl"))


def turn_done_at(codex_dir: Path, session_id: object) -> str | None:
    for path in (codex_dir / "sessions").rglob(f"rollout-*-{session_id}.jsonl"):
        for line in path.read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "task_complete":
                return str(record.get("timestamp"))
    return None


class LaunchCells:
    def __init__(self, run: SmokeRun, env: Mapping[str, str]) -> None:
        self.run = run
        self.env = env

    def wake(self, name: str) -> dict[str, object]:
        body = FIRST_PROMPT[name].format(word=token())
        began = time.monotonic()
        code, woke = bellhop(self.env, "send", name, body, "--wake", "--from", "smoke")
        took = round(time.monotonic() - began, 2)
        first = message(woke)
        result: dict[str, object] = {"wake_exit": code, "wake_seconds": took, "first": first}
        if code != 0:
            return result | {"ok": False}
        self.run.adopt_session(name)
        binding = wait_for(lambda: settled(self.env, name), SETTLED, 2) or agent_row(self.env, name)
        live_at = datetime.now(UTC).isoformat()
        result |= {"binding": binding, "live_seen_at": live_at}
        if not isinstance(binding, dict) or binding.get("state") != "live":
            self.run.save_pane(name, "not-live")
            return result | {"ok": False}
        session_id = binding.get("session_id")
        whole = arrived_whole(
            self.run.layout.root, f"[bellhop {first.get('id')} from smoke] {body}"
        )
        settled_first = wait_for(lambda: confirmed(self.env, first.get("id")), float(CONFIRM), 2)
        _, status = bellhop(self.env, "status", str(first.get("id")))
        rows = status.get("messages")
        first_after = settled_first or (rows[0] if isinstance(rows, list) and rows else {})
        again_code, again = bellhop(
            self.env,
            "send",
            name,
            f"Reply with just the word {token()}.",
            "--from",
            "smoke",
            "--confirm",
            CONFIRM,
        )
        sent = message(again)
        found = wait_for(lambda: confirmed(self.env, sent.get("id")), float(CONFIRM), 2)
        second = found if isinstance(found, dict) else sent
        target = second.get("target")
        reached = target.get("session_id") if isinstance(target, dict) else None
        idle = wait_for(lambda: phase(self.env, session_id) == "idle", IDLE_WAIT, 2)
        if name == "nanna":
            result["first_turn_done_at"] = turn_done_at(self.run.layout.codex_dir, session_id)
        ok = (
            first.get("state") == "pending"
            and first.get("route") == "launch"
            and took < 2
            and whole
            and isinstance(first_after, dict)
            and first_after.get("state") == "confirmed"
            and first_after.get("route") == "launch"
            and again_code == 0
            and second.get("state") == "confirmed"
            and reached == session_id
        )
        return result | {
            "first_after": first_after,
            "semicolon_whole": whole,
            "second": second,
            "idle_after": bool(idle),
            "ok": ok,
        }

    def codex_server_stops(self, name: str) -> dict[str, object]:
        wrapper = self.run.launched[name].pane_pid
        procs = ps()
        tui = next((p for p in procs if p.ppid == wrapper and "--remote" in p.command), None)
        servers = [p for p in procs if "app-server" in p.command and "--listen" in p.command]
        group = [p for p in servers if p.ppid == wrapper or p.ppid in {s.pid for s in servers}]
        if tui is None or len(group) < 2:
            return {"ok": False, "commands": [p.command for p in servers]}
        os.kill(tui.pid, signal.SIGTERM)
        alive = {p.identity for p in group}
        stopped = wait_for(lambda: alive.isdisjoint(p.identity for p in ps()), 20, 0.5)
        sockets = sorted(str(p) for p in self.run.layout.runtime_dir.glob(f"codex-{name}-*.sock"))
        return {
            "ok": bool(stopped) and not sockets,
            "group": [p.pid for p in group],
            "sockets": sockets,
        }


def prepare_codex_home(run: SmokeRun) -> None:
    run.launch("codex", "codex-prep")
    run.wait_ready("codex-prep")
    run.tmux.kill_session("codex-prep")


def test_a_wake_starts_each_harness_and_the_next_send_reaches_the_same_session() -> None:
    run = smoke_run()
    results: dict[str, dict[str, object]] = {}
    with run:
        env = run.env
        extension = run.layout.root / "extension"
        assert bellhop(env, "integration", "export", "pi", "--to", str(extension))[0] == 0
        codex = str(run.binary("codex").path)
        blocks = [
            run.agent_block("claude", "enki"),
            run.agent_block(
                "codex", "nanna", server_command=["/bin/sh", "-c", NON_EXEC_SERVER, codex]
            ),
            run.agent_block("pi", "utu", extra=["-e", str(extension / "bellhop.ts")]),
        ]
        run.write_bellhop_config(*blocks)
        prepare_codex_home(run)
        cells = LaunchCells(run, env)
        try:
            results = run_named(
                {
                    "claude-wake": lambda: cells.wake("enki"),
                    "codex-wake": lambda: cells.wake("nanna"),
                    "codex-server-stops": lambda: cells.codex_server_stops("nanna"),
                    "pi-wake": lambda: cells.wake("utu"),
                }
            )
        finally:
            stop_drainer(env)
            (run.layout.logs / "launch-cells.json").write_text(json.dumps(results, indent=2))
    assert {name: cell.get("ok") for name, cell in results.items()} == dict.fromkeys(
        ("claude-wake", "codex-wake", "codex-server-stops", "pi-wake"), True
    )
    assert run.leftovers == []
