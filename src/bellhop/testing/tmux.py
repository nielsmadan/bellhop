import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SMOKE_SOCKETS = frozenset({"bellhop-smoke", "callboard-smoke"})
TIMEOUT_SECONDS = 30.0
WIDTH = 200
HEIGHT = 50

CONF = """set -g default-terminal "tmux-256color"
set -g history-limit 100000
set -s escape-time 0
set -g status off
set -g remain-on-exit on
set -g default-size 200x50
set -g window-size manual
set -g allow-rename off
set -g automatic-rename off
set -g mouse off
set -s exit-empty on
set -g extended-keys on
"""


class TmuxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SmokeTmux:
    socket: str
    conf: Path
    env: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.socket not in SMOKE_SOCKETS:
            raise TmuxError(
                f"refusing tmux socket '{self.socket}'; smoke runs use {sorted(SMOKE_SOCKETS)}"
            )

    def argv(self, *args: str) -> list[str]:
        return ["tmux", "-L", self.socket, "-f", str(self.conf), *args]

    def run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            self.argv(*args),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            env=dict(self.env),
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if check and result.returncode != 0:
            raise TmuxError(f"tmux {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def start(self, name: str, argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> None:
        extra = [arg for key, value in sorted(env.items()) for arg in ("-e", f"{key}={value}")]
        self.run(
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            str(WIDTH),
            "-y",
            str(HEIGHT),
            "-c",
            str(cwd),
            *extra,
            "--",
            *argv,
        )

    def target(self, name: str) -> str:
        return f"={name}:"

    def alive(self, name: str) -> bool:
        result = subprocess.run(
            self.argv("display-message", "-p", "-t", self.target(name), "#{pane_dead}"),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            env=dict(self.env),
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "0"

    def pane_pids(self) -> set[int]:
        listed = self.run("list-panes", "-a", "-F", "#{pane_pid}", check=False).split()
        return {int(pid) for pid in listed if pid.isdigit()}

    def capture(self, name: str) -> str:
        return self.run("capture-pane", "-p", "-J", "-t", self.target(name))

    def pane(self, name: str) -> tuple[int, str]:
        line = self.run("display-message", "-p", "-t", self.target(name), "#{pane_pid} #{pane_id}")
        pid, pane_id = line.split()
        return int(pid), pane_id

    def keys(self, name: str, *keys: str) -> None:
        self.run("send-keys", "-t", self.target(name), *keys)

    def type(self, name: str, text: str) -> None:
        self.run("send-keys", "-t", self.target(name), "-l", "--", text)

    def kill_session(self, name: str) -> None:
        self.run("kill-session", "-t", f"={name}", check=False)

    def kill_server(self) -> None:
        self.run("kill-server", check=False)

    def sessions(self) -> list[str]:
        return self.run("list-sessions", "-F", "#{session_name}", check=False).split()
