import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from bellhop.allowlist import LOCALE_PREFIX
from bellhop.codes import LAUNCH_FAILED
from bellhop.errors import BellhopError

TMUX_SECONDS = 5.0
WIDTH, HEIGHT = 200, 50
NAME_TRIES = 9
CLIENT_ENV = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM", "TMUX_TMPDIR"}
)
LOCATION = "#{pane_id} #{socket_path}"


class TmuxTimeout(BellhopError):
    pass


@dataclass(frozen=True)
class Place:
    socket: str
    name: str
    pane: str


def server_args(socket: str) -> list[str]:
    if not socket:
        return []
    return ["-S", socket] if "/" in socket else ["-L", socket]


def tmux_target(place: Place) -> str:
    return shlex.join([*server_args(place.socket), f"{place.name}:{place.pane}"])


def target_place(target: str | None) -> Place | None:
    try:
        parts = shlex.split(target or "")
    except ValueError:
        return None
    if not parts or ":" not in parts[-1] or len(parts) not in (1, 3):
        return None
    if len(parts) == 3 and parts[0] not in ("-L", "-S"):  # noqa: PLR2004
        return None
    name, pane = parts[-1].rsplit(":", 1)
    return Place(parts[1] if len(parts) == 3 else "", name, pane)  # noqa: PLR2004


def escaped(argument: str) -> str:
    return f"{argument[:-1]}\\;" if argument.endswith(";") else argument


def client_env(env: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in env.items()
        if name in CLIENT_ENV or name.startswith(LOCALE_PREFIX)
    }


def place_of(name: str, line: str) -> Place | None:
    pane, _, socket = line.strip().partition(" ")
    return Place(socket, name, pane) if pane and socket.startswith("/") else None


@dataclass(frozen=True)
class Tmux:
    socket: str = ""
    executable: str = "tmux"

    def server(self) -> list[str]:
        return server_args(self.socket)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.executable, *self.server(), *args],
                capture_output=True,
                text=True,
                timeout=TMUX_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
                env=client_env(os.environ),
            )
        except subprocess.TimeoutExpired as error:
            raise TmuxTimeout(
                f"tmux {args[0]} gave no answer within {TMUX_SECONDS:g}s", code=LAUNCH_FAILED
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise BellhopError(f"tmux did not run: {error}", code=LAUNCH_FAILED) from error

    def run(self, *args: str) -> str:
        done = self._run(*args)
        if done.returncode != 0:
            message = done.stderr.strip() or f"exit {done.returncode}"
            raise BellhopError(f"tmux {args[0]} failed: {message}", code=LAUNCH_FAILED)
        return done.stdout

    def session_names(self) -> set[str]:
        done = self._run("list-sessions", "-F", "#{session_name}")
        return set(done.stdout.split()) if done.returncode == 0 else set()

    def free_name(self, base: str) -> str:
        names = [base, *(f"{base}-{n}" for n in range(2, NAME_TRIES + 1))]
        taken = self.session_names()
        free = next((name for name in names if name not in taken), None)
        if free is None:
            raise BellhopError(
                f"tmux sessions {names[0]} to {names[-1]} all exist; remove one to launch "
                f"'{base}' again",
                code=LAUNCH_FAILED,
            )
        return free

    def new_session(self, name: str, cwd: Path, argv: Sequence[str]) -> Place:
        line = self.run(
            "new-session",
            "-d",
            "-P",
            "-F",
            LOCATION,
            "-s",
            name,
            "-x",
            str(WIDTH),
            "-y",
            str(HEIGHT),
            "-c",
            escaped(str(cwd)),
            "--",
            *map(escaped, argv),
        )
        found = place_of(name, line)
        if found is None:
            raise BellhopError(
                f"tmux new-session gave no pane: {line.strip()!r}", code=LAUNCH_FAILED
            )
        return found

    def locate(self, name: str) -> Place | None:
        done = self._run("display-message", "-p", "-t", f"={name}:", LOCATION)
        return place_of(name, done.stdout) if done.returncode == 0 else None

    def capture(self, pane: str) -> str | None:
        done = self._run("capture-pane", "-p", "-J", "-t", pane)
        return done.stdout if done.returncode == 0 else None

    def attach_hint(self, name: str) -> str:
        return shlex.join(["tmux", *self.server(), "attach", "-t", f"={name}"])


def tmux_executable(env: Mapping[str, str]) -> str | None:
    return shutil.which("tmux", path=env.get("PATH", ""))


def tmux_for(env: Mapping[str, str], socket: str) -> Tmux:
    return Tmux(socket, tmux_executable(env) or "tmux")
