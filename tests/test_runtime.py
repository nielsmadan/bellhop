import os
from pathlib import Path

import pytest

from bellhop.config import Config
from bellhop.errors import BellhopError
from bellhop.runtime import ensure_runtime_dir, resolve_runtime_dir, runtime_dir

CONFIG = Config(path=Path("/c/config.toml"))


def test_the_env_override_wins_then_the_config_then_the_default() -> None:
    configured = Config(path=Path("/c"), runtime_dir=Path("/srv/bh"))

    assert runtime_dir({"BELLHOP_RUNTIME_DIR": "/x/rt"}, configured, 501) == Path("/x/rt")
    assert runtime_dir({}, configured, 501) == Path("/srv/bh")
    assert runtime_dir({}, CONFIG, 501) == Path("/tmp/bellhop-501")


def test_a_relative_override_is_refused() -> None:
    with pytest.raises(
        BellhopError, match="BELLHOP_RUNTIME_DIR must be an absolute path"
    ) as caught:
        runtime_dir({"BELLHOP_RUNTIME_DIR": "rt"}, CONFIG, 501)

    assert caught.value.code == "invalid_environment"


def test_a_missing_directory_is_created_private(tmp_path: Path) -> None:
    path = ensure_runtime_dir(tmp_path / "rt", os.getuid())

    assert path.stat().st_mode & 0o777 == 0o700


def test_mode_0755_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "rt"
    path.mkdir(mode=0o755)
    path.chmod(0o755)

    with pytest.raises(BellhopError) as caught:
        ensure_runtime_dir(path, os.getuid())

    assert str(caught.value) == f"runtime directory {path} has mode 0755; it must be 0700"
    assert (caught.value.code, caught.value.exit_code) == ("runtime_dir_unsafe", 1)


def test_another_owner_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "rt"
    path.mkdir(mode=0o700)

    with pytest.raises(BellhopError, match=f"is owned by uid {os.getuid()}, not 4242"):
        ensure_runtime_dir(path, 4242)


def test_a_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir(mode=0o700)
    link = tmp_path / "rt"
    link.symlink_to(target)

    with pytest.raises(BellhopError, match="is not a directory"):
        ensure_runtime_dir(link, os.getuid())


def test_resolve_uses_the_override(tmp_path: Path) -> None:
    env = {"BELLHOP_RUNTIME_DIR": str(tmp_path / "rt")}

    assert resolve_runtime_dir(env, CONFIG) == tmp_path / "rt"
