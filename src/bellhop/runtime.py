import os
import stat
from collections.abc import Mapping
from pathlib import Path

from bellhop.codes import INVALID_ENVIRONMENT, RUNTIME_DIR_UNSAFE
from bellhop.config import Config
from bellhop.errors import BellhopError

ENV = "BELLHOP_RUNTIME_DIR"
PRIVATE_MODE = 0o700


def runtime_dir(env: Mapping[str, str], config: Config, uid: int) -> Path:
    override = env.get(ENV)
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise BellhopError(
                f"{ENV} must be an absolute path; got '{override}'", code=INVALID_ENVIRONMENT
            )
        return path
    return config.runtime_dir or Path(f"/tmp/bellhop-{uid}")


def _unsafe(path: Path, problem: str) -> BellhopError:
    return BellhopError(f"runtime directory {path} {problem}", code=RUNTIME_DIR_UNSAFE)


def ensure_runtime_dir(path: Path, uid: int) -> Path:
    try:
        path.mkdir(mode=PRIVATE_MODE)
    except FileExistsError:
        pass
    except OSError as error:
        raise _unsafe(path, f"cannot be created: {error.strerror or error}") from error
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe(path, "is not a directory")
    if info.st_uid != uid:
        raise _unsafe(path, f"is owned by uid {info.st_uid}, not {uid}")
    mode = stat.S_IMODE(info.st_mode)
    if mode != PRIVATE_MODE:
        raise _unsafe(path, f"has mode {mode:04o}; it must be 0700")
    return path


def resolve_runtime_dir(env: Mapping[str, str], config: Config) -> Path:
    owner = os.getuid()
    return ensure_runtime_dir(runtime_dir(env, config, owner), owner)
