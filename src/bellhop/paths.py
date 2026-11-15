from collections.abc import Mapping
from pathlib import Path

from bellhop.codes import INVALID_ENVIRONMENT
from bellhop.errors import BellhopError


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME")
    return Path(home) if home else Path.home()


def xdg_home(env: Mapping[str, str], variable: str, default: str) -> Path:
    value = env.get(variable)
    if not value:
        return _home(env) / default
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
