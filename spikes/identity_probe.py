import json
import os
import re
import sys
import time

from spikelib import REPO, PI_EXTENSION

from bellhop.testing.run import SmokeRun

COMMANDS = {
    "claude": 'echo "id=$CLAUDE_CODE_SESSION_ID ppid=$PPID"',
    "codex": 'echo "id=$CODEX_THREAD_ID ppid=$PPID"',
    "pi": 'echo "id=${PI_SESSION_ID:-unset} ppid=$PPID"',
}


def ask(run: SmokeRun, name: str, agent: str) -> None:
    run.tmux.type(name, f"Run this exact shell command and show me its output verbatim: {COMMANDS[agent]}")
    time.sleep(0.5)
    run.tmux.keys(name, "Enter")
    time.sleep(25)


def ids(run: SmokeRun, name: str) -> list[str]:
    return re.findall(r"id=([0-9a-f-]{8,}|unset)", run.tmux.capture(name))


def hls_ids(run: SmokeRun) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for s in json.loads(run.hls_sessions())["sessions"]:
        found.setdefault(s["session"].get("cwd", "").rsplit("/", 1)[-1], []).append(s["session"]["id"])
    return found


def main() -> int:
    run = SmokeRun(REPO / ".cache" / "smoke", os.environ)
    print("run", run.run_id, flush=True)
    run.start()
    try:
        for agent in ("claude", "codex", "pi"):
            extra = ["-e", str(PI_EXTENSION)] if agent == "pi" else (["--no-daemon"] if agent == "codex" else [])
            run.launch(agent, f"id-{agent}", extra=extra, prompt="Reply with the single word OK.")
        for agent in ("claude", "codex", "pi"):
            run.wait_ready(f"id-{agent}")
        time.sleep(10)
        for agent in ("claude", "codex", "pi"):
            ask(run, f"id-{agent}", agent)
        print("before", {a: ids(run, f"id-{a}") for a in ("claude", "codex", "pi")}, hls_ids(run), flush=True)
        for agent in ("claude", "codex", "pi"):
            run.tmux.type(f"id-{agent}", "/clear" if agent == "claude" else "/new")
            time.sleep(0.5)
            run.tmux.keys(f"id-{agent}", "Enter")
        time.sleep(5)
        for agent in ("claude", "codex", "pi"):
            ask(run, f"id-{agent}", agent)
            run.save_pane(f"id-{agent}", "after-new")
        print("after", {a: ids(run, f"id-{a}") for a in ("claude", "codex", "pi")}, hls_ids(run), flush=True)

        solo = hls_ids(run).get("id-codex", [])
        thread = solo[0] if solo else None
        if thread:
            queued = run.codex("queue", "--thread", thread, "--message", "[bellhop queuetest from spike] reply ACK queuetest", check=False)
            print("queue to stand-alone", queued.returncode, (queued.stdout + queued.stderr).strip()[:300], flush=True)
            time.sleep(15)
            run.save_pane("id-codex", "after-queue")
            print("visible in stand-alone TUI", "queuetest" in run.tmux.capture("id-codex"), flush=True)
            run.tmux.kill_session("id-codex")
            time.sleep(3)
            run.launch("codex", "id-codex-resume", extra=["resume", thread, "--no-daemon"])
            run.wait_ready("id-codex-resume")
            time.sleep(15)
            run.save_pane("id-codex-resume", "after-resume")
            print("visible on resume", "queuetest" in run.tmux.capture("id-codex-resume"), flush=True)
    finally:
        print("leftovers", run.teardown(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
