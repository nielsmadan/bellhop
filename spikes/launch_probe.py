import json
import os
import sys
from pathlib import Path

from bellhop.testing.run import SmokeRun

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    prompt = os.environ.get("PROBE_PROMPT")
    agents = sys.argv[1:] or ["claude", "codex", "pi"]
    run = SmokeRun(REPO / ".cache" / "smoke", os.environ)
    print("run", run.run_id, flush=True)
    run.start()
    try:
        for agent in agents:
            run.launch(agent, f"probe-{agent}", prompt=prompt)
        for agent in agents:
            try:
                answered = run.wait_ready(f"probe-{agent}")
                print(agent, "ready", answered, flush=True)
            except Exception as error:
                print(agent, "NOT READY", error, flush=True)
            run.save_pane(f"probe-{agent}", "ready")
        if prompt:
            import time

            time.sleep(25)
            for agent in agents:
                run.save_pane(f"probe-{agent}", "after-prompt")
        payload = json.loads(run.hls_sessions())
        for s in payload["sessions"]:
            print(
                "hls",
                s["agent"],
                s["session"]["id"],
                s["phase"],
                (s.get("process") or {}).get("pid"),
                (s.get("tmux") or {}).get("pane"),
                s["session"].get("cwd"),
            )
        print("problems", payload.get("problems"))
    finally:
        print("leftovers", run.teardown())
    return 0


if __name__ == "__main__":
    sys.exit(main())
