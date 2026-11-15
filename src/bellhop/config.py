import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bellhop.codes import INVALID_CONFIG
from bellhop.errors import BellhopError
from bellhop.models import HARNESSES, Harness
from bellhop.paths import home as home_dir
from bellhop.paths import xdg_home

NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
NAME_LIMIT = 64
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TOP_KEYS = frozenset({"runtime_dir", "tmux_socket", "agents"})
AGENT_KEYS = frozenset({"harness", "cwd", "command", "env", "server_command"})


@dataclass(frozen=True)
class AgentConfig:
    name: str
    harness: Harness
    cwd: Path | None = None
    command: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    server_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    path: Path
    runtime_dir: Path | None = None
    tmux_socket: str = ""
    agents: Mapping[str, AgentConfig] = field(default_factory=dict)


def config_path(env: Mapping[str, str]) -> Path:
    return xdg_home(env, "XDG_CONFIG_HOME", ".config") / "bellhop" / "config.toml"


def valid_name(name: str) -> bool:
    return len(name) <= NAME_LIMIT and NAME.fullmatch(name) is not None


def _fail(path: Path, key: str, problem: str) -> BellhopError:
    return BellhopError(f"{path}: {key}: {problem}", code=INVALID_CONFIG)


def _expand(value: str, home: Path) -> Path:
    return home / value[2:] if value == "~" or value.startswith("~/") else Path(value)


def _strings(path: Path, key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(path, key, "must be a list of strings")
    return tuple(value)


def _command(path: Path, key: str, value: object) -> tuple[str, ...]:
    argv = _strings(path, key, value)
    if argv and "=" in argv[0]:
        raise _fail(path, key, "must start with a program, not an assignment")
    return argv


def _text(path: Path, key: str, value: object) -> str:
    if not isinstance(value, str):
        raise _fail(path, key, "must be a string")
    return value


def _unknown(path: Path, prefix: str, keys: object, allowed: frozenset[str]) -> None:
    extra = sorted(set(keys) - allowed) if isinstance(keys, dict) else []
    if extra:
        raise _fail(path, f"{prefix}{extra[0]}", "unknown key")


def _agent(path: Path, name: str, raw: object, home: Path) -> AgentConfig:
    key = f"agents.{name}"
    if not valid_name(name):
        raise _fail(
            path,
            key,
            f"agent names use lowercase letters, digits, '_' and '-', at most {NAME_LIMIT}",
        )
    if not isinstance(raw, dict):
        raise _fail(path, key, "must be a table")
    _unknown(path, f"{key}.", raw, AGENT_KEYS)
    harness = HARNESSES.get(str(raw.get("harness")))
    if harness is None:
        raise _fail(path, f"{key}.harness", f"must be one of {', '.join(HARNESSES)}")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise _fail(path, f"{key}.env", "must be a table of strings")
    named = next((name for name in env if not VARIABLE.fullmatch(name)), None)
    if named is not None:
        raise _fail(path, f"{key}.env", f"'{named}' is not an environment variable name")
    cwd = raw.get("cwd")
    return AgentConfig(
        name=name,
        harness=harness,
        cwd=_expand(_text(path, f"{key}.cwd", cwd), home) if cwd is not None else None,
        command=_command(path, f"{key}.command", raw.get("command", [])),
        env={k: str(_expand(v, home)) if v.startswith("~") else v for k, v in env.items()},
        server_command=_strings(path, f"{key}.server_command", raw.get("server_command", [])),
    )


def parse_config(path: Path, text: str, home: Path) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise _fail(path, "toml", str(error)) from error
    _unknown(path, "", raw, TOP_KEYS)
    runtime = raw.get("runtime_dir")
    if runtime is not None and not Path(_text(path, "runtime_dir", runtime)).is_absolute():
        raise _fail(path, "runtime_dir", f"must be an absolute path; got '{runtime}'")
    socket = _text(path, "tmux_socket", raw.get("tmux_socket", ""))
    if "/" in socket and not Path(socket).is_absolute():
        raise _fail(path, "tmux_socket", f"a socket path must be absolute; got '{socket}'")
    agents = raw.get("agents", {})
    if not isinstance(agents, dict):
        raise _fail(path, "agents", "must be a table")
    return Config(
        path=path,
        runtime_dir=Path(runtime) if runtime is not None else None,
        tmux_socket=socket,
        agents={name: _agent(path, name, value, home) for name, value in agents.items()},
    )


def watched_config(
    env: Mapping[str, str], on_error: Callable[[BellhopError], None] | None = None
) -> Callable[[], Config]:
    path = config_path(env)
    seen: dict[str, tuple[tuple[int, int] | None, Config]] = {}

    def current() -> Config:
        try:
            info = path.stat()
            stamp: tuple[int, int] | None = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        known = seen.get("config")
        if known is None or known[0] != stamp:
            try:
                seen["config"] = (stamp, load_config(env))
            except BellhopError as error:
                if known is None or on_error is None:
                    raise
                on_error(error)
                seen["config"] = (stamp, known[1])
        return seen["config"][1]

    return current


def load_config(env: Mapping[str, str]) -> Config:
    path = config_path(env)
    home = home_dir(env)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config(path=path)
    except OSError as error:
        raise _fail(path, "file", f"cannot read: {error.strerror or error}") from error
    return parse_config(path, text, home)
