from pathlib import Path

import pytest

from bellhop.errors import BellhopError
from bellhop.paths import data_home, state_dir


def test_state_dir_lives_under_xdg_state_home(tmp_path: Path) -> None:
    assert state_dir({"XDG_STATE_HOME": str(tmp_path / "state")}) == tmp_path / "state" / "bellhop"


def test_state_dir_defaults_under_home(tmp_path: Path) -> None:
    assert state_dir({"HOME": str(tmp_path)}) == tmp_path / ".local" / "state" / "bellhop"


def test_data_home_defaults_under_home(tmp_path: Path) -> None:
    assert data_home({"HOME": str(tmp_path), "XDG_DATA_HOME": ""}) == tmp_path / ".local" / "share"


def test_relative_xdg_state_home_is_refused() -> None:
    with pytest.raises(BellhopError) as caught:
        state_dir({"XDG_STATE_HOME": "relative/state"})

    assert str(caught.value) == "XDG_STATE_HOME must be an absolute path; got 'relative/state'"
    assert caught.value.code == "invalid_environment"
    assert caught.value.exit_code == 1


def test_relative_xdg_data_home_is_refused() -> None:
    with pytest.raises(BellhopError, match="XDG_DATA_HOME must be an absolute path"):
        data_home({"XDG_DATA_HOME": "data"})
