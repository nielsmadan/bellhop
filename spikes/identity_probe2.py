import json
import os
import re
import sys
import time

from spikelib import PYTHON, REPO

from bellhop.testing.run import SmokeRun

HOOK = (
    "import json,os,sys; d=json.load(sys.stdin); "
    "open(os.environ['BELLHOP_SPIKE_LOG'],'a').write("
    "json.dumps({'hook_session': d.get('session_id'), 'env_session': os.environ.get('CLAUDE_CODE_SESSION_ID')})+'\\n')"
)


def prompt(run: SmokeRun, name: str, text: str, wait: float = 20) -> None:
    run.tmux.type(name, text)
    time.sleep(0.5)
    run.tmux.keys(name, "Enter")
    time.sleep(wait)


def main() -> int:
    run = SmokeRun(REPO / ".cache" / "smoke", os.environ)
    print("run", run.run_id, flush=True)
    run.start()
    try:
        log = run.layout.logs / "claude-ids.jsonl"
        settings = run.layout.root / "id-settings.json"
        command = f"{PYTHON} -I -c {json.dumps(HOOK)}"
        settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]}}))
        claude = "codex-only" not in sys.argv
        if claude:
            run.launch("claude", "id2-claude", extra=["--settings", str(settings)], env={"BELLHOP_SPIKE_LOG": str(log)}, prompt="Reply with the single word OK.")
        run.launch("codex", "id2-codex", extra=["--no-daemon"], prompt="Reply with the single word OK.")
        run.wait_ready("id2-codex")
        if claude:
            run.wait_ready("id2-claude")
            time.sleep(10)
            prompt(run, "id2-claude", "Reply with the single word TWO.")
            prompt(run, "id2-claude", "/clear", 5)
            prompt(run, "id2-claude", "Reply with the single word THREE.")
            print("claude ids", log.read_text() if log.exists() else "-", flush=True)
        time.sleep(10)

        ask = 'Run this exact shell command and show its output verbatim: echo "thread=$CODEX_THREAD_ID"'
        prompt(run, "id2-codex", ask, 25)
        run.save_pane("id2-codex", "before-new")
        print("codex thread before /new", re.findall(r"thread=([0-9a-f-]{8,})", run.tmux.capture("id2-codex")), flush=True)
        prompt(run, "id2-codex", "/new", 12)
        prompt(run, "id2-codex", ask, 30)
        run.save_pane("id2-codex", "after-new")
        print("codex threads in pane", re.findall(r"thread=([0-9a-f-]{8,})", run.tmux.capture("id2-codex")), flush=True)
        records = [s for s in json.loads(run.hls_sessions())["sessions"] if s["agent"] == "codex"]
        print("codex hls", [(s["session"]["id"], s["observedAt"]) for s in records], flush=True)

        thread = sorted(records, key=lambda s: s["observedAt"])[-1]["session"]["id"]
        queued = run.codex("queue", "--thread", thread, "--message", "[bellhop queuetest from spike] reply with exactly ACK queuetest", check=False)
        print("queue to stand-alone", queued.returncode, (queued.stdout + queued.stderr).strip()[:200], flush=True)
        time.sleep(15)
        print("visible in stand-alone TUI", "queuetest" in run.tmux.capture("id2-codex"), flush=True)
        run.tmux.kill_session("id2-codex")
        time.sleep(3)
        run.launch("codex", "id2-resume", extra=["resume", thread, "--no-daemon"])
        print("resume dialogs", run.wait_ready("id2-resume"), flush=True)
        time.sleep(25)
        run.save_pane("id2-resume", "after-resume")
        pane = run.tmux.capture("id2-resume")
        print("visible on resume", "queuetest" in pane, "acked", "ACK queuetest" in pane, flush=True)
    finally:
        print("leftovers", run.teardown(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
