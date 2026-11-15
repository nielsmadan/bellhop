import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from codex_rpc import Client, rpc_request, turn_start_request
from spikelib import REPO, codex_hits, codex_rollouts, envelope, token

from bellhop.testing.run import SmokeRun

PROMPT = "Reply with the single word OK."


def daemon_socket(codex_home: Path) -> Path:
    control = codex_home.resolve() / "app-server-control" / "app-server-control.sock"
    return Path(f"/private/tmp/codex-daemon-{os.getuid()}") / hashlib.sha256(str(control).encode()).hexdigest()


def call(client: Client, method: str, params: dict) -> dict:
    return client.call(lambda req_id: rpc_request(req_id, method, params))


def connect(path: Path) -> Client:
    client = Client(str(path))
    client.initialize()
    return client


def loaded(path: Path) -> list[str]:
    client = connect(path)
    try:
        return list(call(client, "thread/loaded/list", {}).get("data") or [])
    finally:
        client.close()


def turn_start(path: Path, thread: str, text: str) -> str:
    client = connect(path)
    try:
        result = client.call(turn_start_request, thread, text, f"spike-{token()}")
        return f"turn/start {result.get('turn', {}).get('id', '')}"
    except Exception as error:  # noqa: BLE001
        return f"error {type(error).__name__}: {error}"
    finally:
        client.close()


def hits(run: SmokeRun, tok: str) -> dict[str, dict[str, int]]:
    found = {p.name: codex_hits(p, tok) for p in codex_rollouts(run.layout.codex_dir)}
    return {k: v for k, v in found.items() if v}


def hls(run: SmokeRun) -> None:
    for s in json.loads(run.hls_sessions())["sessions"]:
        if s["agent"] == "codex":
            print("  hls", s["session"]["id"], s["phase"], s.get("process"), (s.get("tmux") or {}).get("pane"), flush=True)


def main() -> int:
    run = SmokeRun(REPO / ".cache" / "smoke", os.environ)
    print("run", run.run_id, flush=True)
    run.start()
    remote_server: subprocess.Popen[bytes] | None = None
    try:
        codex_home = Path(run.env["CODEX_HOME"])
        assert codex_home.resolve().is_relative_to(run.layout.root.resolve())
        ours = daemon_socket(codex_home)
        users = daemon_socket(Path(os.environ["HOME"]) / ".codex")
        assert ours != users
        run._prepare("codex", run.layout.work)
        started = run.codex("app-server", "daemon", "start", check=False)
        print("daemon start", started.returncode, started.stdout.strip()[:200], started.stderr.strip()[:200], flush=True)
        print("daemon socket", ours, ours.exists(), flush=True)
        run.track()

        run.launch("codex", "x-daemon", prompt=PROMPT)
        run.launch("codex", "x-solo", extra=["--no-daemon"], prompt=PROMPT)
        remote_sock = run.layout.runtime_dir / "codex-remote.sock"
        log = (run.layout.logs / "remote-server.log").open("wb")
        remote_server = subprocess.Popen(
            [str(run.binary("codex").path), "app-server", "--listen", f"unix://{remote_sock}"],
            env=run.env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while not remote_sock.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        run.track()
        run.launch("codex", "x-remote", extra=["--remote", f"unix://{remote_sock}"], prompt=PROMPT)
        for name in ("x-daemon", "x-solo", "x-remote"):
            print(name, "ready", run.wait_ready(name), flush=True)
        time.sleep(15)
        daemon_threads = loaded(ours) if ours.exists() else []
        remote_threads = loaded(remote_sock)
        print("daemon loaded", daemon_threads, flush=True)
        print("remote loaded", remote_threads, flush=True)
        hls(run)
        for rollout in codex_rollouts(codex_home):
            print("  rollout", rollout.name, flush=True)

        if daemon_threads:
            tok = token()
            print("daemon turn/start", turn_start(ours, daemon_threads[0], envelope(tok, "S1")), flush=True)
            time.sleep(20)
            print("  hits", hits(run, tok), flush=True)
            run.save_pane("x-daemon", "after-turnstart")
            tok = token()
            queued = run.codex("queue", "--thread", daemon_threads[0], "--message", envelope(tok, "S1"), check=False)
            print("daemon codex queue", queued.returncode, queued.stdout.strip()[:200], queued.stderr.strip()[:300], flush=True)
            time.sleep(20)
            print("  hits", hits(run, tok), flush=True)
            run.save_pane("x-daemon", "after-queue")

        if remote_threads:
            tok = token()
            print("remote turn/start", turn_start(remote_sock, remote_threads[0], envelope(tok, "S1")), flush=True)
            time.sleep(20)
            print("  hits", hits(run, tok), flush=True)
            run.save_pane("x-remote", "after-turnstart")

        solo_ids = [p.name for p in codex_rollouts(codex_home)]
        print("all rollouts", solo_ids, flush=True)
    finally:
        if remote_server is not None:
            remote_server.terminate()
        print("leftovers", run.teardown())
    return 0


if __name__ == "__main__":
    sys.exit(main())
