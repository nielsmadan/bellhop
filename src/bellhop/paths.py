import os
import pwd
from collections.abc import Mapping
from pathlib import Path

from bellhop.codes import INVALID_ENVIRONMENT
from bellhop.errors import BellhopError


def home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME")
    return Path(home) if home else Path.home()


def xdg_home(env: Mapping[str, str], variable: str, default: str) -> Path:
    value = env.get(variable)
    if not value:
        return home(env) / default
    path = Path(value)
    if not path.is_absolute():
        raise BellhopError(
            f"{variable} must be an absolute path; got '{value}'", code=INVALID_ENVIRONMENT
        )
    return path


def data_home(env: Mapping[str, str]) -> Path:
    return xdg_home(env, "XDG_DATA_HOME", ".local/share")


def state_dir(env: Mapping[str, str]) -> Path:
    return xdg_home(env, "XDG_STATE_HOME", ".local/state") / "bellhop"


def account_home() -> list[Path]:
    try:
        return [Path(pwd.getpwuid(os.getuid()).pw_dir)]
    except KeyError:
        return []


def default_state(env: Mapping[str, str]) -> bool:
    homes = {home(env), *account_home()}
    defaults = {(h / ".local" / "state" / "bellhop").resolve() for h in homes}
    return state_dir(env).resolve() in defaults


def configured_dir(env: Mapping[str, str], variable: str, default: str) -> Path | None:
    value = env.get(variable)
    if not value:
        return home(env) / default
    if value == "~" or value.startswith("~/"):
        return home(env) / value[2:]
    path = Path(value)
    return path if path.is_absolute() else None
