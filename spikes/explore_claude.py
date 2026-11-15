import json
import os
import sys
import time

from spikelib import (
    PYTHON,
    RELAY,
    REPO,
    SPIKES,
    claude_hits,
    claude_transcripts,
    envelope,
    send,
    token,
    wait_socket,
)

from bellhop.testing.run import SmokeRun

CANDIDATES = ("mcp", "hook", "modrelay", "modsubmit")


def extra_for(run: SmokeRun, name: str, candidate: str) -> list[str]:
    root = run.layout.root
    if candidate == "mcp":
        config = root / f"{name}-mcp.json"
        config.write_text(json.dumps({"mcpServers": {"bellhop-relay": {"command": PYTHON, "args": ["-I", str(RELAY), "mcp"]}}}))
        return ["--mcp-config", str(config)]
    if candidate == "hook":
        settings = root / f"{name}-settings.json"
        command = f"{PYTHON} -I {RELAY} detach"
        settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}))
        return ["--settings", str(settings)]
    if candidate == "modrelay":
        return ["--plugin-dir", str(SPIKES / "mod-relay")]
    return ["--plugin-dir", str(SPIKES / "mod-submit")]


def main() -> int:
    candidates = sys.argv[1:] or list(CANDIDATES)
    run = SmokeRun(REPO / ".cache" / "smoke", os.environ)
    print("run", run.run_id, flush=True)
    run.start()
    sockets = {}
    try:
        for candidate in candidates:
            name = f"c-{candidate}"
            sock = run.layout.runtime_dir / f"{name}.sock"
            sockets[candidate] = sock
            env = {
                "BELLHOP_SPIKE_SOCKET": str(sock),
                "BELLHOP_SPIKE_LOG": str(run.layout.logs / f"{name}-relay.log"),
                "BELLHOP_SPIKE_PYTHON": PYTHON,
                "BELLHOP_SPIKE_RELAY": str(RELAY),
            }
            run.launch("claude", name, extra=extra_for(run, name, candidate), env=env)
        for candidate in candidates:
            name = f"c-{candidate}"
            try:
                print(candidate, "ready", run.wait_ready(name), flush=True)
            except Exception as error:
                print(candidate, "NOT READY", error, flush=True)
            print(candidate, "socket", wait_socket(sockets[candidate], 20), flush=True)
        toks = {}
        for candidate in candidates:
            tok = token()
            toks[candidate] = tok
            print(candidate, "send", send(sockets[candidate], "deliver boundary", envelope(tok, "S1")), flush=True)
        time.sleep(25)
        for candidate in candidates:
            name = f"c-{candidate}"
            run.save_pane(name, "after-s1")
            hits = {p.name: claude_hits(p, toks[candidate]) for p in claude_transcripts(run.layout.claude_dir)}
            print(candidate, "hits", {k: v for k, v in hits.items() if v}, flush=True)
            log = run.layout.logs / f"{name}-relay.log"
            print(candidate, "log", log.read_text() if log.exists() else "-", flush=True)
    finally:
        print("leftovers", run.teardown())
    return 0


if __name__ == "__main__":
    sys.exit(main())
