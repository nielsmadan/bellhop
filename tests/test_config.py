from pathlib import Path

import pytest

from bellhop.config import AgentConfig, Config, load_config, parse_config, watched_config
from bellhop.errors import BellhopError

FULL = """
runtime_dir = "/tmp/bellhop-501"
tmux_socket = "fleet"

[agents.enki]
harness = "claude"
cwd = "~/fleet/work/slimemold"
command = ["/opt/ac/bin/fleet-claude"]
env = { CLAUDE_CONFIG_DIR = "~/fleet/harness/claude", PLAIN = "x" }

[agents.nanna]
harness = "codex"
server_command = ["/opt/ac/bin/fleet-codex-server"]
"""


def test_a_missing_file_is_an_empty_config(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path), "HOME": str(tmp_path)}

    assert load_config(env) == Config(path=tmp_path / "bellhop" / "config.toml")


def test_every_key_is_read_and_home_is_expanded(tmp_path: Path) -> None:
    path = tmp_path / "bellhop" / "config.toml"
    path.parent.mkdir()
    path.write_text(FULL)

    config = load_config({"XDG_CONFIG_HOME": str(tmp_path), "HOME": "/home/me"})

    assert config.runtime_dir == Path("/tmp/bellhop-501")
    assert config.tmux_socket == "fleet"
    assert config.agents["enki"] == AgentConfig(
        name="enki",
        harness="claude",
        cwd=Path("/home/me/fleet/work/slimemold"),
        command=("/opt/ac/bin/fleet-claude",),
        env={"CLAUDE_CONFIG_DIR": "/home/me/fleet/harness/claude", "PLAIN": "x"},
    )
    assert config.agents["nanna"].server_command == ("/opt/ac/bin/fleet-codex-server",)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("runtime_dir = 'relative'", "runtime_dir: must be an absolute path; got 'relative'"),
        ("colour = 1", "colour: unknown key"),
        ("[agents.Enki]\nharness = 'claude'", "agents.Enki: agent names use lowercase"),
        (
            "[agents.enki]\nharness = 'droid'",
            "agents.enki.harness: must be one of claude, codex, pi",
        ),
        (
            "[agents.enki]\nharness = 'pi'\ncommand = 'pi'",
            "agents.enki.command: must be a list of strings",
        ),
        ("[agents.enki]\nharness = 'pi'\nmodel = 'x'", "agents.enki.model: unknown key"),
        (
            "[agents.enki]\nharness = 'pi'\nenv = { A = 1 }",
            "agents.enki.env: must be a table of strings",
        ),
        (
            "[agents.enki]\nharness = 'pi'\nenv = { 'A-B' = '1' }",
            "agents.enki.env: 'A-B' is not an environment variable name",
        ),
        (
            "[agents.enki]\nharness = 'pi'\ncommand = ['X=1', 'pi']",
            "agents.enki.command: must start with a program, not an assignment",
        ),
        (
            "tmux_socket = 'run/tmux.sock'",
            "tmux_socket: a socket path must be absolute; got 'run/tmux.sock'",
        ),
        ("agents = 3", "agents: must be a table"),
        ("tmux_socket = 3", "tmux_socket: must be a string"),
        ("not toml =", "toml:"),
    ],
)
def test_invalid_config_names_the_file_and_key(text: str, message: str) -> None:
    with pytest.raises(BellhopError) as caught:
        parse_config(Path("/c/config.toml"), text, Path("/home/me"))

    assert str(caught.value).startswith(f"/c/config.toml: {message}")
    assert caught.value.code == "invalid_config"
    assert caught.value.exit_code == 2


def test_a_watched_config_follows_edits_to_the_file(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    path = tmp_path / "config" / "bellhop" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('tmux_socket = "one"\n')
    current = watched_config(env)
    first = current().tmux_socket
    path.write_text('tmux_socket = "two-two"\n[agents.enki]\nharness = "pi"\n')

    assert (first, current().tmux_socket, sorted(current().agents)) == ("one", "two-two", ["enki"])


def test_a_bad_edit_keeps_the_last_good_config_and_is_reported_once(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    path = tmp_path / "config" / "bellhop" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('tmux_socket = "good"\n')
    errors: list[str] = []
    current = watched_config(env, on_error=lambda error: errors.append(str(error)))
    current()
    path.write_text("[agents.half\n")

    sockets = [current().tmux_socket, current().tmux_socket]

    assert (sockets, len(errors)) == (["good", "good"], 1)
