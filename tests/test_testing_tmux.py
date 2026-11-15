from pathlib import Path

import pytest

from bellhop.testing.tmux import SmokeTmux, TmuxError


def test_smoke_tmux_refuses_any_other_socket(tmp_path: Path) -> None:
    with pytest.raises(TmuxError, match="refusing tmux socket 'default'"):
        SmokeTmux("default", tmp_path / "tmux.conf", {})


def test_smoke_tmux_pins_its_socket_and_conf(tmp_path: Path) -> None:
    tmux = SmokeTmux("bellhop-smoke", tmp_path / "tmux.conf", {})

    assert tmux.argv("list-sessions") == [
        "tmux",
        "-L",
        "bellhop-smoke",
        "-f",
        str(tmp_path / "tmux.conf"),
        "list-sessions",
    ]
