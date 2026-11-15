import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bellhop.testing.agents import (
    Binary,
    Layout,
    isolated_env,
    launch_argv,
    make_work,
    pi_entry,
    pi_launcher,
    resolve_binary,
    write_claude_home,
    write_codex_home,
    write_pi_home,
)
from bellhop.testing.screens import Agent, classify
from bellhop.testing.tmux import CONF, SmokeTmux

SMOKE_SOCKET = "bellhop-smoke"
CONSUMER = "bellhop-smoke"
READY_TIMEOUT_SECONDS = 120.0
AUTH_HOLD_SECONDS = 5.0
EXIT_GRACE_SECONDS = 15.0
DIALOG_REPEATS = 3
HARNESSES: tuple[Agent, ...] = ("claude", "codex", "pi")
UNSET = Path()


class SmokeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    command: str
    started: str = ""

    @property
    def identity(self) -> tuple[int, str, str]:
        return (self.pid, self.started, self.command)


@dataclass(frozen=True)
class Launched:
    name: str
    agent: Agent
    work: Path
    session_id: str | None
    pane_pid: int
    pane_id: str


@dataclass
class SmokeRun:
    cache: Path
    environ: Mapping[str, str]
    socket: str = SMOKE_SOCKET
    sops_exec: Path = UNSET
    hls_source: Path = UNSET
    codex_auth: Path = UNSET
    user_home: Path = UNSET
    run_id: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    )
    launched: dict[str, Launched] = field(default_factory=dict)
    binaries: dict[Agent, Binary] = field(default_factory=dict)
    tracked: dict[int, Proc] = field(default_factory=dict)
    adopted: set[int] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        home = Path(self.environ.get("HOME") or Path.home())
        if self.user_home == UNSET:
            self.user_home = home
        if self.sops_exec == UNSET:
            self.sops_exec = home / "ac" / "bin" / "sops-exec"
        if self.hls_source == UNSET:
            self.hls_source = (
                home / ".local" / "share" / "hooklinesinker" / "bin" / "hooklinesinker"
            )
        if self.codex_auth == UNSET:
            self.codex_auth = home / ".codex" / "auth.json"

    @property
    def layout(self) -> Layout:
        return Layout(self.cache / self.run_id, Path(f"/tmp/bellhop-smoke-{self.run_id}"))

    @property
    def env(self) -> dict[str, str]:
        return isolated_env(self.layout, self.environ)

    @property
    def tmux(self) -> SmokeTmux:
        return SmokeTmux(self.socket, self.layout.root / "tmux.conf", self.env)

    def start(self) -> None:
        layout = self.layout
        for directory in (layout.home, layout.tmp, layout.work, layout.logs, layout.xdg):
            directory.mkdir(parents=True, exist_ok=True)
        layout.runtime_dir.mkdir(mode=0o700)
        (layout.root / "tmux.conf").write_text(CONF, encoding="utf-8")
        self.tmux.kill_server()
        self._hls("install", "--consumer", CONSUMER, binary=self.hls_source)
        for agent in HARNESSES:
            self._hls("hooks", "install", "--agent", agent)

    def _hls(self, *args: str, binary: Path | None = None) -> str:
        executable = binary or self.layout.hls
        try:
            result = subprocess.run(
                [str(executable), *args],
                capture_output=True,
                text=True,
                timeout=60,
                env=self.env,
                check=False,
            )
        except OSError as error:
            raise SmokeError(f"cannot run {executable}: {error.strerror or error}") from error
        if result.returncode != 0:
            raise SmokeError(f"hooklinesinker {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def binary(self, agent: Agent) -> Binary:
        if agent not in self.binaries:
            self.binaries[agent] = resolve_binary(agent, self.env)
        return self.binaries[agent]

    def _prepare(self, agent: Agent, work: Path) -> None:
        works = [launched.work for launched in self.launched.values()] + [work]
        match agent:
            case "claude":
                write_claude_home(self.layout, works, self.binary("claude").version, self.user_home)
            case "codex":
                write_codex_home(self.layout, works, self.codex_auth)
            case "pi":
                write_pi_home(self.layout, self.binary("pi").version)

    def launch(
        self,
        agent: Agent,
        name: str,
        *,
        extra: Sequence[str] = (),
        prompt: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Launched:
        with self.lock:
            if name in self.launched:
                raise SmokeError(f"session name '{name}' already used in this run")
            work_dir = make_work(self.layout, name)
            self._prepare(agent, work_dir)
            session_id = str(uuid.uuid4()) if agent in ("claude", "pi") else None
            pi_env, pi_prefix = self._pi_prefix() if agent == "pi" else ({}, [])
            command = launch_argv(
                self.binary(agent),
                self.sops_exec,
                session_id=session_id,
                extra=extra,
                prompt=prompt,
                pi_prefix=pi_prefix,
            )
            pane_env = {"AGENT_REQUIRE_SECRETS": "1", **pi_env, **(env or {})}
            self.tmux.start(name, command, pane_env, work_dir)
            pane_pid, pane_id = self.tmux.pane(name)
            launched = Launched(name, agent, work_dir, session_id, pane_pid, pane_id)
            self.launched[name] = launched
        self.track()
        return launched

    def _pi_prefix(self) -> tuple[dict[str, str], list[str]]:
        entry = pi_entry(self.binary("pi").path, self.env["PATH"])
        return {"NODE_PATH": entry.node_path}, [
            str(pi_launcher(self.layout, entry)),
            str(entry.script),
        ]

    def wait_ready(self, name: str, timeout: float = READY_TIMEOUT_SECONDS) -> list[str]:
        launched = self.launched[name]
        deadline = time.monotonic() + timeout
        answered: list[str] = []
        auth_since: float | None = None
        while time.monotonic() < deadline:
            if not self.tmux.alive(name):
                saved = self.save_pane(name, "exited")
                raise SmokeError(f"{name} exited before it was ready; pane saved to {saved}")
            screen = classify(launched.agent, self.tmux.capture(name))
            if screen.state == "ready":
                self.track()
                return answered
            if screen.state == "auth":
                auth_since = auth_since or time.monotonic()
                if time.monotonic() - auth_since >= AUTH_HOLD_SECONDS:
                    raise SmokeError(f"{name} shows a login screen: {screen.detail}")
            else:
                auth_since = None
            if screen.state == "dialog":
                if answered[-DIALOG_REPEATS:].count(screen.detail) >= DIALOG_REPEATS:
                    saved = self.save_pane(name, "stuck-dialog")
                    raise SmokeError(
                        f"{name} keeps showing the {screen.detail} dialog; pane saved to {saved}"
                    )
                answered.append(screen.detail)
                for key in screen.keys:
                    self.tmux.keys(name, key)
                    time.sleep(0.2)
                time.sleep(1.0)
                continue
            time.sleep(0.5)
        self.save_pane(name, "not-ready")
        raise SmokeError(f"{name} was not ready within {timeout:g}s")

    def save_pane(self, name: str, label: str) -> Path:
        path = self.layout.logs / f"{name}-{label}.txt"
        path.write_text(self.tmux.capture(name), encoding="utf-8")
        return path

    def adopt(self, pid: int) -> None:
        self.adopted.add(pid)
        self.track()

    def pid_files(self) -> set[int]:
        if not self.layout.runtime_dir.is_dir():
            return set()
        texts = [path.read_text().strip() for path in self.layout.runtime_dir.glob("*.pid")]
        return {int(text) for text in texts if text.isdigit()}

    def owned(self, procs: Sequence[Proc]) -> set[int]:
        roots = self.tmux.pane_pids() | self.adopted | self.pid_files()
        root = f"{self.layout.root}/"
        from_root = {proc.pid for proc in procs if proc.command.split(" ", 1)[0].startswith(root)}
        return descendants(procs, roots | from_root)

    def track(self) -> None:
        procs = ps()
        owned = self.owned(procs)
        self.tracked |= {proc.pid: proc for proc in procs if proc.pid in owned}

    def survivors(self) -> list[Proc]:
        return [
            proc
            for proc in ps()
            if proc.pid in self.tracked and self.tracked[proc.pid].identity == proc.identity
        ]

    def teardown(self) -> list[str]:
        if self.launched:
            self.track()
        self.tmux.kill_server()
        if (self.layout.codex_dir / "app-server-daemon").exists():
            self.codex("app-server", "daemon", "stop", check=False)
            self.track()
        deadline = time.monotonic() + EXIT_GRACE_SECONDS
        while self.survivors() and time.monotonic() < deadline:
            time.sleep(0.5)
        for proc in self.survivors():
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        time.sleep(0.5)
        leftovers = [f"process {proc.pid}: {proc.command[:120]}" for proc in self.survivors()]
        if self.socket in self.tmux_server_sockets():
            leftovers.append(f"tmux server -L {self.socket}")
        remove_codex_sockets(self.layout.codex_dir)
        if self.layout.runtime_dir.exists():
            leftovers += [f"socket {path}" for path in self.layout.runtime_dir.iterdir()]
            shutil.rmtree(self.layout.runtime_dir, ignore_errors=True)
        return leftovers

    def tmux_server_sockets(self) -> list[str]:
        directory = Path(f"/private/tmp/tmux-{os.getuid()}")
        alive = (
            subprocess.run(
                self.tmux.argv("list-sessions"),
                capture_output=True,
                timeout=30,
                env=self.env,
                check=False,
            ).returncode
            == 0
        )
        return [self.socket] if alive and (directory / self.socket).exists() else []

    def codex(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.binary("codex").path), *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=self.env,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if check and result.returncode != 0:
            raise SmokeError(f"codex {' '.join(args)} failed: {result.stderr.strip()[:400]}")
        return result

    def hls_sessions(self) -> str:
        return self._hls("sessions", "--json")


def codex_socket_links(codex_dir: Path, shared: Path | None = None) -> list[Path]:
    shared = shared or Path(f"/private/tmp/codex-daemon-{os.getuid()}")
    links = [
        link
        for folder in ("app-server-daemon", "app-server-control")
        for link in sorted((codex_dir / folder).glob("*.sock"))
        if link.is_symlink()
    ]
    targets = [Path(os.readlink(link)) for link in links]
    return [target for target in targets if target.parent == shared]


def remove_codex_sockets(codex_dir: Path, shared: Path | None = None) -> None:
    for target in codex_socket_links(codex_dir, shared):
        for path in (target, target.with_name(target.name + ".lock")):
            path.unlink(missing_ok=True)


def parse_ps(text: str) -> list[Proc]:
    rows = [(*line.split(None, 7), "")[:8] for line in text.splitlines() if line.strip()]
    return [Proc(int(row[0]), int(row[1]), row[7], " ".join(row[2:7])) for row in rows]


def ps() -> list[Proc]:
    result = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,lstart=,command="],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"},
    )
    return parse_ps(result.stdout)


def descendants(procs: Sequence[Proc], roots: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for proc in procs:
        children.setdefault(proc.ppid, []).append(proc.pid)
    found: set[int] = set()
    frontier = list(roots)
    while frontier:
        pid = frontier.pop()
        if pid in found:
            continue
        found.add(pid)
        frontier.extend(children.get(pid, []))
    return found
