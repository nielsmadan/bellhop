import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, TracebackType
from typing import IO, Self

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
TEARDOWN_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
BASE = Path("/tmp")
AGENT_ENV = {
    "AGENT_REQUIRE_SECRETS": "1",
    "DISABLE_AUTOUPDATER": "1",
    "PI_SKIP_VERSION_CHECK": "1",
    "PI_TELEMETRY": "0",
}
DRAIN_LOCK = Path("bellhop") / "drain.lock"
DRAIN_COMMAND = "bellhop _drain"
DRAIN_STOP_SECONDS = 10.0
DRAIN_POLL_SECONDS = 0.2


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


@dataclass(frozen=True)
class HostPaths:
    sops_exec: Path
    hls_source: Path
    codex_auth: Path
    user_home: Path


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def host_paths(
    environ: Mapping[str, str],
    sops_exec: Path | None,
    hls_source: Path | None,
    codex_auth: Path | None,
    user_home: Path | None,
) -> HostPaths:
    home = Path(environ.get("HOME") or Path.home())
    return HostPaths(
        sops_exec=sops_exec or home / "ac" / "bin" / "sops-exec",
        hls_source=hls_source
        or home / ".local" / "share" / "hooklinesinker" / "bin" / "hooklinesinker",
        codex_auth=codex_auth or home / ".codex" / "auth.json",
        user_home=user_home or home,
    )


@dataclass
class SmokeRun:
    cache: Path
    environ: Mapping[str, str]
    socket: str = SMOKE_SOCKET
    sops_exec: Path | None = None
    hls_source: Path | None = None
    codex_auth: Path | None = None
    user_home: Path | None = None
    codex_features: tuple[str, ...] = ()
    claude_allowed: tuple[str, ...] = ()
    run_id: str = field(default_factory=new_run_id)
    base: Path = BASE
    launched: dict[str, Launched] = field(default_factory=dict)
    binaries: dict[Agent, Binary] = field(default_factory=dict)
    tracked: dict[int, Proc] = field(default_factory=dict)
    panes: dict[str, Proc] = field(default_factory=dict)
    agents: dict[str, tuple[Agent, Path]] = field(default_factory=dict)
    leftovers: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    paths: HostPaths = field(init=False)
    _socket_lock: IO[str] | None = field(default=None, init=False, repr=False)
    _handlers: dict[int, object] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not RUN_ID.fullmatch(self.run_id):
            raise SmokeError(f"run id '{self.run_id}' is not YYYYMMDD-HHMMSS-xxxx")
        self.paths = host_paths(
            self.environ, self.sops_exec, self.hls_source, self.codex_auth, self.user_home
        )

    @property
    def layout(self) -> Layout:
        return Layout(self.cache / self.run_id, self.base / f"bellhop-smoke-{self.run_id}")

    @property
    def env(self) -> dict[str, str]:
        return isolated_env(self.layout, self.environ)

    @property
    def tmux(self) -> SmokeTmux:
        return SmokeTmux(self.socket, self.layout.root / "tmux.conf", self.env)

    @property
    def lock_path(self) -> Path:
        return self.base / f"bellhop-smoke-{os.getuid()}-{self.socket}.lock"

    def __enter__(self) -> Self:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            self._handlers[signum] = signal.signal(signum, _interrupt)
        try:
            self.start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        quiet: dict[int, object] = {
            int(signum): signal.signal(signum, signal.SIG_IGN) for signum in TEARDOWN_SIGNALS
        }
        try:
            self.leftovers = [*self.leftovers, *self.teardown()]
        finally:
            restore: dict[int, object] = {**quiet, **self._handlers}
            for signum, handler in restore.items():
                signal.signal(signum, handler)  # type: ignore[arg-type]
            self._handlers.clear()

    def start(self) -> None:
        self._acquire()
        layout = self.layout
        for directory in (layout.home, layout.tmp, layout.work, layout.logs, layout.xdg):
            directory.mkdir(parents=True, exist_ok=True)
        layout.runtime_dir.mkdir(mode=0o700)
        (layout.root / "tmux.conf").write_text(CONF, encoding="utf-8")
        self.tmux.kill_server()
        self.leftovers = self.sweep_earlier_runs()
        self._hls("install", "--consumer", CONSUMER, binary=self.paths.hls_source)
        for agent in HARNESSES:
            self._hls("hooks", "install", "--agent", agent)

    def _acquire(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise SmokeError(
                f"another smoke run holds {self.lock_path}; tmux -L {self.socket} is in use"
            ) from None
        self._socket_lock = handle

    def _release(self) -> None:
        if self._socket_lock is not None:
            self._socket_lock.close()
            self._socket_lock = None

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
        works = [
            *(launched.work for launched in self.launched.values()),
            *(known for _, known in self.agents.values()),
            work,
        ]
        match agent:
            case "claude":
                write_claude_home(
                    self.layout,
                    works,
                    self.binary("claude").version,
                    self.paths.user_home,
                    self.claude_allowed,
                )
            case "codex":
                write_codex_home(self.layout, works, self.paths.codex_auth, self.codex_features)
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
                self.paths.sops_exec,
                session_id=session_id,
                extra=extra,
                prompt=prompt,
                pi_prefix=pi_prefix,
            )
            pane_env = {"AGENT_REQUIRE_SECRETS": "1", **pi_env, **(env or {})}
            self.tmux.start(name, command, pane_env, work_dir)
            pane_pid, pane_id = self.tmux.pane(name)
            pane = next((proc for proc in ps() if proc.pid == pane_pid), None)
            if pane is not None:
                self.panes[name] = pane
            launched = Launched(name, agent, work_dir, session_id, pane_pid, pane_id)
            self.launched[name] = launched
        self.track()
        return launched

    def agent_block(
        self,
        agent: Agent,
        name: str,
        *,
        extra: Sequence[str] = (),
        server_command: Sequence[str] = (),
    ) -> str:
        with self.lock:
            work = make_work(self.layout, name)
            self._prepare(agent, work)
            self.agents[name] = (agent, work)
        pi_env, pi_prefix = self._pi_prefix() if agent == "pi" else ({}, [])
        binary = self.binary(agent)
        command = launch_argv(binary, self.paths.sops_exec, extra=extra, pi_prefix=pi_prefix)
        homes = {
            "claude": {"CLAUDE_CONFIG_DIR": str(self.layout.claude_dir)},
            "codex": {"CODEX_HOME": str(self.layout.codex_dir)},
            "pi": {"PI_CODING_AGENT_DIR": str(self.layout.pi_dir)},
        }[agent]
        env = {**AGENT_ENV, **pi_env, **homes}
        table = ", ".join(f"{key} = {json.dumps(value)}" for key, value in env.items())
        lines = [
            f"[agents.{name}]",
            f"harness = {json.dumps(agent)}",
            f"cwd = {json.dumps(str(work))}",
            f"command = {json.dumps(command)}",
            f"env = {{ {table} }}",
        ]
        if agent == "codex":
            server = list(server_command) or [str(binary.path), "app-server"]
            lines.append(f"server_command = {json.dumps(server)}")
        return "\n".join(lines) + "\n"

    def write_bellhop_config(self, *blocks: str) -> Path:
        path = Path(self.env["XDG_CONFIG_HOME"]) / "bellhop" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"tmux_socket = {json.dumps(self.socket)}\n\n" + "\n".join(blocks))
        return path

    def adopt_session(self, name: str) -> Launched:
        if name not in self.agents:
            raise SmokeError(f"cannot adopt tmux session {name}: no agent_block named it")
        agent, work = self.agents[name]
        with self.lock:
            pane_pid, pane_id = self.tmux.pane(name)
            procs = ps()
            pane = next((proc for proc in procs if proc.pid == pane_pid), None)
            if pane is None or pane_pid in protected(procs):
                raise SmokeError(f"cannot adopt tmux session {name}: no pane process this run owns")
            self.panes[name] = pane
            launched = Launched(name, agent, work, None, pane_pid, pane_id)
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
        procs = ps()
        proc = next((p for p in procs if p.pid == pid), None)
        if proc is None or pid in protected(procs):
            raise SmokeError(f"cannot adopt pid {pid}: it is not a process this run may own")
        self.tracked[pid] = proc

    def live_panes(self, procs: Sequence[Proc]) -> set[int]:
        # a pane's process execs (sops-exec, Pi retitles itself), so pid and start time identify it
        started = {proc.pid: proc.started for proc in procs}
        return {
            state.pid
            for state in self.tmux.panes()
            if not state.dead
            and state.session in self.panes
            and self.panes[state.session].pid == state.pid
            and started.get(state.pid) == self.panes[state.session].started
        }

    def track(self) -> None:
        procs = ps()
        owned = descendants(procs, self.live_panes(procs)) | under_root(procs, self.layout.root)
        allowed = owned - protected(procs)
        self.tracked |= {proc.pid: proc for proc in procs if proc.pid in allowed}

    def survivors(self) -> list[Proc]:
        procs = ps()
        never = protected(procs)
        return [
            proc
            for proc in procs
            if proc.pid in self.tracked
            and proc.pid not in never
            and self.tracked[proc.pid].identity == proc.identity
        ]

    def teardown(self) -> list[str]:
        if self._socket_lock is None:
            return []
        problems: list[str] = []
        steps: list[tuple[str, Callable[[], object]]] = [
            ("track", self.track if self.launched else lambda: None),
            ("tmux kill-server", self.tmux.kill_server),
            ("codex daemon stop", self._stop_daemon),
            ("track after daemon stop", self.track),
        ]
        for label, step in steps:
            _attempt(label, step, problems)
        leftovers = _attempt_list("stop processes", self._stop_survivors, problems)
        if _attempt_bool("tmux server check", self.tmux.server_alive, problems):
            leftovers.append(f"tmux server -L {self.socket}")
        drainer = _attempt("stop drainer", self.stop_drainer, problems)
        leftovers += [drainer] if isinstance(drainer, str) else []
        _attempt("codex sockets", self._remove_codex_sockets, problems)
        leftovers += _attempt_list("runtime dir", self._remove_runtime_dir, problems)
        self._release()
        return leftovers + problems

    def stop_drainer(self) -> str | None:
        return stop_drainer(self.env)

    def _stop_daemon(self) -> None:
        if (self.layout.codex_dir / "app-server-daemon").exists():
            self.codex("app-server", "daemon", "stop", check=False)

    def _stop_survivors(self) -> list[str]:
        return terminate(self.survivors, EXIT_GRACE_SECONDS)

    def _remove_codex_sockets(self) -> None:
        home, codex = self.layout.home, self.layout.codex_dir
        remove_codex_sockets(self.layout.runtime_dir)
        for directory in (codex / "app-server-daemon", codex / "app-server-control"):
            if not _interior_links(self.layout.root, [home, codex, directory]):
                remove_codex_sockets(directory)

    def _remove_runtime_dir(self) -> list[str]:
        runtime = self.layout.runtime_dir
        if not runtime.exists():
            return []
        found = [f"socket {path}" for path in runtime.iterdir()]
        shutil.rmtree(runtime, ignore_errors=True)
        return found

    def sweep_earlier_runs(self) -> list[str]:
        if not self.cache.is_dir():
            return []
        named = [
            path
            for path in sorted(self.cache.iterdir())
            if path.name != self.run_id and RUN_ID.fullmatch(path.name)
        ]
        refused = {path: _unsafe_entry(path, self.cache) for path in named}
        notes = [f"skipped {path}: {reason}" for path, reason in refused.items() if reason]
        return notes + [note for path in named if not refused[path] for note in self._sweep(path)]

    def _sweep(self, root: Path) -> list[str]:
        layout = Layout(root, self.base / f"bellhop-smoke-{root.name}")
        daemon_dir = layout.codex_dir / "app-server-daemon"
        followed = _interior_links(
            root, (layout.home, layout.codex_dir, daemon_dir, daemon_dir / "daemon.pid")
        )
        notes = [
            f"skipped the codex cleanup of {root.name}: {link} is or lies under a symlink"
            for link in followed
        ]
        if not followed and (daemon_dir / "daemon.pid").exists():
            _attempt(
                f"stop the codex daemon of {root.name}",
                lambda: self.codex(
                    "app-server",
                    "daemon",
                    "stop",
                    check=False,
                    env=isolated_env(layout, self.environ),
                ),
                notes,
            )
        procs = ps()
        mine = under_root(procs, root) - protected(procs)
        stale = {proc.pid: proc for proc in procs if proc.pid in mine}
        notes += [
            f"left pid {proc.pid} ({proc.command[:80]}): it names {root.name} but runs a binary "
            "outside that run's root, so it cannot be proven to be that run's"
            for proc in procs
            if proc.pid not in mine and _names(proc, (root, layout.runtime_dir))
        ]
        notes += terminate(
            lambda: [proc for proc in ps() if stale.get(proc.pid) == proc], EXIT_GRACE_SECONDS
        )
        runtime_problem = _unsafe_entry(layout.runtime_dir, self.base)
        if layout.runtime_dir.exists() or layout.runtime_dir.is_symlink():
            if runtime_problem:
                return [*notes, f"skipped {layout.runtime_dir}: {runtime_problem}"]
            remove_codex_sockets(layout.runtime_dir)
            shutil.rmtree(layout.runtime_dir, ignore_errors=True)
        if not followed:
            remove_codex_sockets(daemon_dir)
        return notes

    def codex(
        self, *args: str, check: bool = True, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [str(self.binary("codex").path), *args],
                capture_output=True,
                text=True,
                timeout=120,
                env=dict(env or self.env),
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SmokeError(f"codex {' '.join(args)} failed: {error}") from error
        if check and result.returncode != 0:
            raise SmokeError(f"codex {' '.join(args)} failed: {result.stderr.strip()[:400]}")
        return result

    def hls_sessions(self) -> str:
        return self._hls("sessions", "--json")


def _unsafe_entry(path: Path, parent: Path) -> str | None:
    if path.is_symlink():
        return "it is a symlink"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if info.st_uid != os.getuid():
        return f"it is owned by uid {info.st_uid}"
    if path.resolve().parent != parent.resolve():
        return f"it resolves outside {parent}"
    return None


def _interior_links(root: Path, paths: Sequence[Path]) -> list[Path]:
    outside = [
        path
        for path in paths
        if path.exists() and not path.resolve().is_relative_to(root.resolve())
    ]
    return sorted({*(path for path in paths if path.is_symlink()), *outside})


def _names(proc: Proc, paths: Sequence[Path]) -> bool:
    return any(str(path) in proc.command for path in paths)


def _interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def _attempt(label: str, step: Callable[[], object], problems: list[str]) -> object:
    try:
        return step()
    except Exception as error:  # noqa: BLE001
        problems.append(f"{label} failed: {type(error).__name__}: {error}")
        return None


def _attempt_list(label: str, step: Callable[[], list[str]], problems: list[str]) -> list[str]:
    result = _attempt(label, step, problems)
    return result if isinstance(result, list) else []


def _attempt_bool(label: str, step: Callable[[], bool], problems: list[str]) -> bool:
    return _attempt(label, step, problems) is True


def terminate(survivors: Callable[[], list[Proc]], grace: float) -> list[str]:
    problems = [note for proc in survivors() for note in _signal(proc, signal.SIGTERM)]
    deadline = time.monotonic() + grace
    while survivors() and time.monotonic() < deadline:
        time.sleep(0.2)
    problems += [note for proc in survivors() for note in _signal(proc, signal.SIGKILL)]
    time.sleep(0.2)
    return problems + [f"process {proc.pid}: {proc.command[:120]}" for proc in survivors()]


def _signal(proc: Proc, signum: int) -> list[str]:
    try:
        os.kill(proc.pid, signum)
    except ProcessLookupError:
        return []
    except PermissionError:
        return [f"process {proc.pid}: not allowed to signal it"]
    return []


def protected(procs: Sequence[Proc]) -> set[int]:
    parents = {proc.pid: proc.ppid for proc in procs}
    chain = {0, 1}
    current = os.getpid()
    while current > 1 and current not in chain:
        chain.add(current)
        current = parents.get(current, 0)
    return chain


def under_root(procs: Sequence[Proc], root: Path) -> set[int]:
    prefixes = (f"{root}/", f"{root.resolve()}/")
    return {proc.pid for proc in procs if proc.command.split(" ", 1)[0].startswith(prefixes)}


def codex_socket_targets(directory: Path, shared: Path | None = None) -> list[Path]:
    home = shared or Path(f"/private/tmp/codex-daemon-{os.getuid()}")
    if not directory.is_dir():
        return []
    links = [link for link in sorted(directory.glob("*.sock")) if link.is_symlink()]
    pairs = [(link, Path(os.readlink(link))) for link in links]
    return [
        target
        for link, target in pairs
        if target.parent == home
        and target.name
        == hashlib.sha256(str(link.parent.resolve() / link.name).encode()).hexdigest()
    ]


def remove_codex_sockets(directory: Path, shared: Path | None = None) -> None:
    for target in codex_socket_targets(directory, shared):
        for path in (target, target.with_name(target.name + ".lock")):
            path.unlink(missing_ok=True)


def parse_ps(text: str) -> list[Proc]:
    rows = [(*line.split(None, 7), "")[:8] for line in text.splitlines() if line.strip()]
    return [Proc(int(row[0]), int(row[1]), row[7], " ".join(row[2:7])) for row in rows]


def lock_held(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


def lock_owner(lock: Path) -> str:
    try:
        return lock.read_text().strip() if not lock.is_symlink() else ""
    except OSError:
        return ""


def owned_drainer(state_home: Path, procs: Sequence[Proc]) -> Proc | None:
    lock = state_home / DRAIN_LOCK
    text = lock_owner(lock)
    proc = next((p for p in procs if text.isdigit() and p.pid == int(text)), None)
    if proc is None or DRAIN_COMMAND not in proc.command or not lock_held(lock):
        return None
    return proc if lock_owner(lock) == text else None


def in_a_run(state_home: Path) -> bool:
    return any(RUN_ID.fullmatch(parent.name) for parent in state_home.resolve().parents)


def stop_drainer(env: Mapping[str, str]) -> str | None:
    state_home = env.get("XDG_STATE_HOME")
    if not state_home or not in_a_run(Path(state_home)):
        return f"skipped the drainer stop: {state_home} is not a smoke run's state dir"
    found = owned_drainer(Path(state_home), ps())
    if found is None:
        return None
    with contextlib.suppress(ProcessLookupError):
        os.kill(found.pid, signal.SIGTERM)
    deadline = time.monotonic() + DRAIN_STOP_SECONDS
    while time.monotonic() < deadline:
        if all(proc.identity != found.identity for proc in ps()):
            return None
        time.sleep(DRAIN_POLL_SECONDS)
    return f"drainer {found.pid}"


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
