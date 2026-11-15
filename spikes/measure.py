import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from codex_rpc import Client, rpc_request, turn_start_request, turn_steer_request
from spikelib import PI_EXTENSION, PYTHON, RELAY, REPO, SPIKES, envelope, jsonl, send, token, wait_socket

from bellhop.testing.agents import make_work
from bellhop.testing.run import SmokeRun

CLAUDE = ("mcp", "hook", "modrelay", "modsubmit")
CODEX = ("daemon-start", "daemon-queue", "remote-start")
PI = ("ext",)
REPS = 3
RECEIVE_SECONDS = 90.0
IDLE_SECONDS = 150.0
LOCK = threading.Lock()
STARTUP = {"claude": threading.Lock(), "codex": threading.Lock(), "pi": threading.Lock()}

PERMISSION = {
    "claude": re.compile(r"Do you want to (proceed|make this edit|create)|Yes, and don't ask again", re.I),
    "codex": re.compile(r"Would you like to (run|make)|approve|Allow command|Yes, proceed", re.I),
}
QUESTION = {
    "claude": re.compile(r"Red.*\n(.*\n)*.*Blue|Blue.*\n(.*\n)*.*Red", re.I),
    "codex": re.compile(r"Red.*\n(.*\n)*.*Blue|Blue.*\n(.*\n)*.*Red", re.I),
}
WORKTREE = re.compile(r"Where should the new conversation run\?")
BUSY = re.compile(r"esc to interrupt|Working \(|Working\.\.\.|esc interrupt", re.I)


@dataclass
class Session:
    run: SmokeRun
    harness: str
    candidate: str
    name: str
    work: Path = field(default_factory=Path)
    socket: Path | None = None
    codex_socket: Path | None = None
    server: subprocess.Popen[bytes] | None = None

    def pane(self) -> str:
        return self.run.tmux.capture(self.name)

    def type(self, text: str, enter: bool = True) -> None:
        self.run.tmux.type(self.name, text)
        if enter:
            time.sleep(0.5)
            self.run.tmux.keys(self.name, "Enter")

    def transcripts(self) -> list[Path]:
        match self.harness:
            case "claude":
                files = list((self.run.layout.claude_dir / "projects").glob(f"*{self.name}*/*.jsonl"))
            case "codex":
                files = [
                    p for p in (self.run.layout.codex_dir / "sessions").rglob("rollout-*.jsonl")
                    if (jsonl(p)[:1] or [{}])[0].get("payload", {}).get("cwd") == str(self.work)
                ]
            case _:
                files = list((self.run.layout.pi_dir / "sessions").glob(f"*{self.name}*/*.jsonl"))
        return sorted(files, key=lambda p: p.stat().st_mtime)

    def current(self) -> Path | None:
        found = self.transcripts()
        return found[-1] if found else None

    def received(self, row: dict, tok: str) -> bool:
        text = json.dumps(row)
        if tok not in text:
            return False
        match self.harness:
            case "claude":
                if row.get("type") == "attachment":
                    return (row.get("attachment") or {}).get("type") == "queued_command"
                return row.get("type") == "user" and "tool_result" not in text
            case "codex":
                payload = row.get("payload") or {}
                return row.get("type") == "response_item" and payload.get("role") == "user"
            case _:
                return row.get("type") == "message" and (row.get("message") or {}).get("role") in ("user", "custom")

    def assistant(self, row: dict, text: str) -> bool:
        dumped = json.dumps(row)
        if text not in dumped:
            return False
        match self.harness:
            case "claude":
                return row.get("type") == "assistant"
            case "codex":
                return row.get("type") == "response_item" and (row.get("payload") or {}).get("role") == "assistant"
            case _:
                return (row.get("message") or {}).get("role") == "assistant"

    def find(self, tok: str) -> tuple[Path, int] | None:
        for path in reversed(self.transcripts()):
            rows = jsonl(path)
            index = next((i for i, row in enumerate(rows) if self.received(row, tok)), None)
            if index is not None:
                return path, index
        return None

    def wait_received(self, tok: str, timeout: float = RECEIVE_SECONDS) -> tuple[Path, int] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hit = self.find(tok)
            if hit:
                return hit
            time.sleep(1)
        return None

    def wait_idle(self, timeout: float = IDLE_SECONDS) -> bool:
        deadline = time.monotonic() + timeout
        quiet_since = time.monotonic()
        last = None
        while time.monotonic() < deadline:
            path = self.current()
            stamp = (path, path.stat().st_mtime) if path else None
            pane = self.pane()
            busy = bool(BUSY.search(pane))
            if stamp != last or busy:
                last, quiet_since = stamp, time.monotonic()
            elif time.monotonic() - quiet_since >= 4:
                return True
            time.sleep(1)
        return False

    def codex_thread(self) -> str | None:
        records = [
            s for s in json.loads(self.run.hls_sessions())["sessions"]
            if s["agent"] == "codex" and s["session"].get("cwd") == str(self.work)
        ]
        records.sort(key=lambda s: s["observedAt"])
        return records[-1]["session"]["id"] if records else None

    def push(self, how: str, text: str) -> str:
        if self.harness != "codex":
            assert self.socket is not None
            return send(self.socket, f"deliver {how}", text)
        thread = self.codex_thread()
        if thread is None:
            return "error no codex thread in hls for this work dir"
        if self.candidate == "daemon-queue":
            result = self.run.codex("queue", "--thread", thread, "--message", text, check=False)
            return f"queue exit {result.returncode}: {(result.stdout or result.stderr).strip()[:160]}"
        holder: dict[str, str] = {}
        worker = threading.Thread(target=codex_deliver, args=(self.codex_socket, thread, text, how, holder), daemon=True)
        worker.start()
        worker.join(2.0)
        return holder.get("reply", f"pending (spike drainer waiting) thread={thread}")


def codex_deliver(sock: Path, thread: str, text: str, how: str, holder: dict[str, str]) -> None:
    try:
        client = Client(str(sock))
        client.initialize()
        deadline = time.monotonic() + 180
        while True:
            turn = client.busy_turn(thread)
            if turn is None:
                result = client.call(turn_start_request, thread, text, f"spike-{token()}")
                holder["reply"] = f"ok turn/start {result.get('turn', {}).get('id', '')}"
                return
            if how == "interrupt" and turn != "starting":
                result = client.call(turn_steer_request, thread, turn, text, f"spike-{token()}")
                holder["reply"] = f"ok turn/steer {result.get('turnId', turn)}"
                return
            if time.monotonic() > deadline:
                holder["reply"] = "error still busy after 180s"
                return
            time.sleep(0.5)
    except Exception as error:  # noqa: BLE001
        holder["reply"] = f"error {type(error).__name__}: {error}"


class Measure:
    def __init__(self, run: SmokeRun, results: Path) -> None:
        self.run = run
        self.results = results
        self.daemon_socket: Path | None = None

    def record(self, session: Session, rep: int, scenario: str, passed: bool | None, **detail: object) -> None:
        row = {
            "harness": session.harness,
            "candidate": session.candidate,
            "rep": rep,
            "scenario": scenario,
            "pass": passed,
            "session": session.name,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **detail,
        }
        with LOCK, self.results.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    def launch(self, harness: str, candidate: str, name: str) -> Session:
        run = self.run
        session = Session(run, harness, candidate, name)
        sock = run.layout.runtime_dir / f"{name}.sock"
        env = {
            "BELLHOP_SPIKE_SOCKET": str(sock),
            "BELLHOP_SPIKE_LOG": str(run.layout.logs / f"{name}-relay.log"),
            "BELLHOP_SPIKE_PYTHON": PYTHON,
            "BELLHOP_SPIKE_RELAY": str(RELAY),
        }
        extra: list[str] = []
        if harness == "claude":
            session.socket = sock
            extra = claude_extra(run, name, candidate, sock)
        elif harness == "pi":
            session.socket = sock
            extra = ["-e", str(PI_EXTENSION)]
        elif candidate == "remote-start":
            work = make_work(run.layout, name)
            remote = run.layout.runtime_dir / f"{name}-srv.sock"
            log = (run.layout.logs / f"{name}-server.log").open("wb")
            session.server = subprocess.Popen(
                [str(run.binary("codex").path), "app-server", "--listen", f"unix://{remote}"],
                env=run.env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=work,
            )
            deadline = time.monotonic() + 30
            while not remote.exists() and time.monotonic() < deadline:
                time.sleep(0.5)
            run.adopt(session.server.pid)
            session.codex_socket = remote
            extra = ["--remote", f"unix://{remote}"]
        else:
            session.codex_socket = self.daemon_socket
        with STARTUP[harness]:
            with LOCK:
                launched = run.launch(harness, name, extra=extra, env=env, prompt="Reply with the single word OK.")
            session.work = launched.work
            run.wait_ready(name)
        if session.socket is not None and not wait_socket(session.socket, 30):
            raise RuntimeError(f"{name}: endpoint socket never answered ping")
        session.wait_idle()
        return session

    def close(self, session: Session) -> None:
        self.run.save_pane(session.name, "end")
        self.run.tmux.kill_session(session.name)
        if session.server is not None:
            session.server.terminate()

    def s1(self, s: Session, rep: int) -> None:
        tok = token()
        sent = time.monotonic()
        reply = s.push("boundary", envelope(tok, "S1"))
        hit = s.wait_received(tok)
        self.record(s, rep, "S1", hit is not None, reply=reply, latency_s=round(time.monotonic() - sent, 1) if hit else None)

    def s2(self, s: Session, rep: int, how: str) -> None:
        s.wait_idle()
        done = token()
        s.type(f"Run the shell command `sleep 20` and then reply with exactly DONE {done}.")
        time.sleep(8)
        busy = bool(BUSY.search(s.pane()))
        tok = token()
        reply = s.push(how, envelope(tok, f"S2-{how}"))
        hit = s.wait_received(tok, 150)
        s.wait_idle()
        rows = jsonl(hit[0]) if hit else []
        done_at = next((i for i, row in enumerate(rows) if s.assistant(row, f"DONE {done}")), None)
        timing = None
        if hit:
            timing = "mid-turn" if done_at is None or hit[1] < done_at else "boundary"
        self.record(s, rep, f"S2-{how}", hit is not None, reply=reply, busy_at_push=busy, timing=timing, turn_finished=done_at is not None)

    def s10(self, s: Session, rep: int) -> None:
        s.wait_idle()
        before = s.current()
        hls_before = s.codex_thread() if s.harness == "codex" else None
        s.type("/clear" if s.harness == "claude" else "/new")
        time.sleep(3)
        worktree = bool(WORKTREE.search(s.pane()))
        if worktree:
            self.run.save_pane(s.name, "S10-worktree-dialog")
            s.run.tmux.keys(s.name, "Up")
            time.sleep(0.3)
            s.run.tmux.keys(s.name, "Enter")
        time.sleep(5)
        hls_after = s.codex_thread() if s.harness == "codex" else None
        tok = token()
        reply = s.push("boundary", envelope(tok, "S10"))
        hit = s.wait_received(tok)
        landed = hit[0].name if hit else None
        self.record(
            s, rep, "S10", bool(hit and hit[0] != before), reply=reply, before=before.name if before else None,
            landed=landed, worktree_dialog=worktree, hls_thread_before=hls_before, hls_thread_after=hls_after,
        )

    def s7(self, s: Session, rep: int) -> None:
        s.wait_idle()
        draft = f"draft {token()} keep me"
        s.type(draft, enter=False)
        time.sleep(1.5)
        tok = token()
        reply = s.push("boundary", envelope(tok, "S7"))
        hit = s.wait_received(tok)
        s.wait_idle()
        pane = s.pane()
        sent = s.find(draft.split()[1]) is not None
        kept = draft in pane
        self.record(s, rep, "S7", bool(hit and kept and not sent), reply=reply, delivered=hit is not None, draft_kept=kept, draft_submitted=sent)

    def dialog(self, s: Session, rep: int, scenario: str, prompt: str, pattern: re.Pattern[str] | None) -> None:
        if pattern is None:
            self.record(s, rep, scenario, None, na="the harness has no such dialog")
            return
        s.wait_idle()
        s.type(prompt)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not pattern.search(s.pane()):
            time.sleep(1)
        if not pattern.search(s.pane()):
            self.run.save_pane(s.name, f"{scenario}-no-dialog")
            if s.harness == "codex" and scenario == "S3b":
                self.record(s, rep, scenario, None, not_measured="Codex opened no question dialog in its default mode (request_user_input asked for, nothing shown)")
            else:
                self.record(s, rep, scenario, None, error="dialog never appeared")
            return
        self.run.save_pane(s.name, f"{scenario}-dialog")
        toks = (token(), token())
        replies = [s.push("boundary", envelope(toks[0], scenario))]
        time.sleep(12)
        replies.append(s.push("interrupt", envelope(toks[1], scenario)))
        time.sleep(15)
        pane = s.pane()
        self.run.save_pane(s.name, f"{scenario}-after")
        still = bool(pattern.search(pane))
        created = (s.work / "perm.txt").exists()
        delivered = [s.find(t) is not None for t in toks]
        self.record(s, rep, scenario, still and not created, replies=replies, dialog_still_open=still, file_created=created, delivered_while_open=delivered)

    def session_a(self, harness: str, candidate: str, rep: int) -> None:
        s = self.launch(harness, candidate, f"{harness[:2]}-{candidate}-r{rep}-a")
        try:
            self.s1(s, rep)
            self.s2(s, rep, "boundary")
            if not (harness == "codex" and candidate == "daemon-queue"):
                self.s2(s, rep, "interrupt")
            self.s10(s, rep)
            self.s7(s, rep)
        finally:
            self.close(s)

    def session_b(self, harness: str, candidate: str, rep: int) -> None:
        if harness == "pi":
            s = Session(self.run, harness, candidate, f"pi-{candidate}-r{rep}-b")
            self.record(s, rep, "S3", None, na="Pi has no permission prompts")
            return
        s = self.launch(harness, candidate, f"{harness[:2]}-{candidate}-r{rep}-b")
        try:
            prompt = "Use the shell to run this exact command and nothing else: echo hi > perm.txt"
            if harness == "codex":
                prompt += ". The sandbox is read-only, so request approval to run it outside the sandbox with escalated permissions."
            self.dialog(s, rep, "S3", prompt, PERMISSION[harness])
        finally:
            self.close(s)

    def session_c(self, harness: str, candidate: str, rep: int) -> None:
        if harness == "pi":
            s = Session(self.run, harness, candidate, f"pi-{candidate}-r{rep}-c")
            self.record(s, rep, "S3b", None, na="Pi has no question dialog")
            return
        s = self.launch(harness, candidate, f"{harness[:2]}-{candidate}-r{rep}-c")
        tool = "AskUserQuestion" if harness == "claude" else "request_user_input"
        try:
            self.dialog(
                s, rep, "S3b",
                f"Use the {tool} tool to ask me exactly one question, 'Red or blue?', with the two options Red and Blue. Do nothing else.",
                QUESTION[harness],
            )
        finally:
            self.close(s)


def claude_extra(run: SmokeRun, name: str, candidate: str, sock: Path) -> list[str]:
    root = run.layout.root
    if candidate == "mcp":
        config = root / f"{name}-mcp.json"
        config.write_text(json.dumps({"mcpServers": {"bellhop-relay": {"command": PYTHON, "args": ["-I", str(RELAY), "mcp", str(sock)]}}}))
        return ["--mcp-config", str(config), "--strict-mcp-config"]
    if candidate == "hook":
        settings = root / f"{name}-settings.json"
        command = f"{PYTHON} -I {RELAY} detach"
        settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}))
        return ["--settings", str(settings)]
    return ["--plugin-dir", str(SPIKES / ("mod-relay" if candidate == "modrelay" else "mod-submit"))]


def expected(harness: str, candidate: str, part: str) -> set[str]:
    if part == "b":
        return {"S3"}
    if part == "c":
        return {"S3b"}
    interrupt = set() if candidate == "daemon-queue" else {"S2-interrupt"}
    return {"S1", "S2-boundary", "S10", "S7"} | interrupt


def done_parts(paths: list[str]) -> set[tuple[str, str, int, str]]:
    rows = [json.loads(line) for path in paths for line in Path(path).read_text().splitlines() if line.strip()]
    seen: dict[tuple[str, str, int, str], set[str]] = {}
    for r in rows:
        if r["pass"] is None and not r.get("na"):
            continue
        key = (r["harness"], r["candidate"], r["rep"], r["session"].rsplit("-", 1)[-1])
        seen.setdefault(key, set()).add(r["scenario"])
    return {key for key, scenarios in seen.items() if expected(key[0], key[1], key[3]) <= scenarios}


def jobs(measure: Measure, harnesses: list[str], reps: range, done: set[tuple[str, str, int, str]]) -> list[Callable[[], None]]:
    plan = {"claude": CLAUDE, "codex": CODEX, "pi": PI}
    parts = {"a": measure.session_a, "b": measure.session_b, "c": measure.session_c}
    out: list[Callable[[], None]] = []
    for rep in reps:
        for harness in harnesses:
            for candidate in plan[harness]:
                for key, part in parts.items():
                    if (harness, candidate, rep, key) not in done:
                        out.append(lambda part=part, harness=harness, candidate=candidate, rep=rep: part(harness, candidate, rep))
    return out


def guarded(job: Callable[[], None]) -> None:
    try:
        job()
    except Exception:  # noqa: BLE001
        print("JOB ERROR", traceback.format_exc(), flush=True)


def stop_daemon_updater(run: SmokeRun) -> None:
    pid_file = run.layout.codex_dir / "app-server-daemon" / "daemon-updater.pid"
    if not pid_file.exists():
        print("no daemon updater pid file", flush=True)
        return
    pid = int(json.loads(pid_file.read_text())["pid"])
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout
    if str(run.layout.root) in command:
        os.kill(pid, 15)
        print("stopped isolated daemon updater", pid, flush=True)
    else:
        print("daemon updater pid does not belong to this run; left alone", pid, flush=True)


def interrupted(signum: int, frame: object) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def main() -> int:
    import signal

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    harnesses = [h for h in sys.argv[1:] if h in ("claude", "codex", "pi")] or ["claude", "codex", "pi"]
    reps = range(int(os.environ.get("SPIKE_REP_FROM", "1")), int(os.environ.get("SPIKE_REPS", REPS)) + 1)
    workers = int(os.environ.get("SPIKE_WORKERS", "6"))
    run = SmokeRun(REPO / ".cache" / "smoke", os.environ)
    results = REPO / ".cache" / f"spike-results-{run.run_id}.jsonl"
    print("run", run.run_id, "results", results, flush=True)
    run.start()
    measure = Measure(run, results)
    try:
        versions = {h: run.binary(h).version for h in harnesses}
        print("versions", versions, flush=True)
        if "codex" in harnesses:
            run._prepare("codex", run.layout.work)
            started = run.codex("app-server", "daemon", "start", check=False)
            print("daemon", started.returncode, started.stdout.strip()[:120], flush=True)
            from explore_codex import daemon_socket

            stop_daemon_updater(run)
            print("daemon version", run.codex("app-server", "daemon", "version", check=False).stdout.strip()[:300], flush=True)
            measure.daemon_socket = daemon_socket(Path(run.env["CODEX_HOME"]))
            assert measure.daemon_socket != daemon_socket(Path(os.environ["HOME"]) / ".codex")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = done_parts([p for p in os.environ.get("SPIKE_DONE", "").split(":") if p])
            list(pool.map(guarded, jobs(measure, harnesses, reps, done)))
    finally:
        if "codex" in harnesses:
            print("daemon version at end", run.codex("app-server", "daemon", "version", check=False).stdout.strip()[:300], flush=True)
        print("leftovers", run.teardown(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
