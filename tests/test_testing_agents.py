import json
import os
from pathlib import Path

import pytest

from bellhop.testing.agents import (
    AgentSetupError,
    Binary,
    Layout,
    isolated_env,
    launch_argv,
    make_work,
    pi_entry,
    pi_launcher,
    resolve_binary,
    write_claude_home,
    write_codex_home,
    write_pi_home,
)


def _layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path / "run", tmp_path / "runtime")


def test_isolated_env_keeps_only_the_allowlist_and_points_everything_into_the_run(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    environ = {
        "PATH": "/usr/bin",
        "SOPS_SECRETS": "/secrets.yaml",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/me.sock",
        "CLAUDE_CODE_MESSAGING_TOKEN": "token",
        "CLAUDE_CODE_SESSION_ID": "mine",
        "TMUX": "/tmp/tmux-501/default,1,0",
        "OPENROUTER_API_KEY": "key",
    }

    env = isolated_env(layout, environ)

    assert env["PATH"] == "/usr/bin"
    assert env["SOPS_SECRETS"] == "/secrets.yaml"
    assert env["HOME"] == str(layout.home)
    assert env["CLAUDE_CONFIG_DIR"] == str(layout.home / ".claude")
    assert env["CODEX_HOME"] == str(layout.home / ".codex")
    assert env["PI_CODING_AGENT_DIR"] == str(layout.home / ".pi" / "agent")
    assert env["XDG_STATE_HOME"] == str(layout.root / "xdg" / "state")
    assert env["BELLHOP_RUNTIME_DIR"] == str(layout.runtime_dir)
    assert sorted(set(environ) & set(env)) == ["PATH", "SOPS_SECRETS"]


def test_claude_home_trusts_the_work_dirs_and_keeps_installed_hooks(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    settings = layout.claude_dir / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"Stop": []}}))
    work = make_work(layout, "w1")

    write_claude_home(layout, [work], "2.1.325", tmp_path / "user")

    config = json.loads((layout.claude_dir / ".claude.json").read_text())
    assert config["projects"][str(work.resolve())]["hasTrustDialogAccepted"] is True
    assert config["lastReleaseNotesSeen"] == "2.1.325"
    written = json.loads(settings.read_text())
    assert written["hooks"] == {"Stop": []}
    assert written["permissions"] == {"allow": ["Bash(sleep:*)"]}
    assert str(tmp_path / "user" / "CLAUDE.md") in written["claudeMdExcludes"]


def test_codex_home_links_auth_and_disables_shell_snapshots(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    work = make_work(layout, "w1")

    write_codex_home(layout, [work], auth, ("default_mode_request_user_input",))

    config = (layout.codex_dir / "config.toml").read_text()
    assert "[features]\nshell_snapshot = false\ndefault_mode_request_user_input = true\n" in config
    assert f'[projects."{work.resolve()}"]\ntrust_level = "trusted"' in config
    assert (layout.codex_dir / "auth.json").readlink() == auth


@pytest.mark.parametrize(
    ("features", "written"),
    [
        (("shell_snapshot",), "[features]\nshell_snapshot = true\n\n"),
        (("x_y", "x_y"), "[features]\nshell_snapshot = false\nx_y = true\n\n"),
    ],
    ids=["overrides a default", "named twice"],
)
def test_codex_features_are_written_once_each(
    tmp_path: Path, features: tuple[str, ...], written: str
) -> None:
    layout = _layout(tmp_path)
    auth = tmp_path / "auth.json"
    auth.write_text("{}")

    write_codex_home(layout, [], auth, features)

    assert written in (layout.codex_dir / "config.toml").read_text()


def test_codex_feature_names_cannot_add_config(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("{}")

    with pytest.raises(AgentSetupError, match="not a codex feature name"):
        write_codex_home(
            _layout(tmp_path), [], auth, ('x = true\n[mcp_servers.x]\ncommand = "sh"',)
        )


def test_codex_home_refuses_without_the_user_auth_file(tmp_path: Path) -> None:
    with pytest.raises(AgentSetupError, match="isolated Codex cannot authenticate"):
        write_codex_home(_layout(tmp_path), [], tmp_path / "missing.json")


def test_pi_home_sets_the_cheap_model_and_silences_startup(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    write_pi_home(layout, "1.5.2")

    settings = json.loads((layout.pi_dir / "settings.json").read_text())
    assert (settings["defaultProvider"], settings["defaultModel"]) == (
        "openrouter",
        "deepseek/deepseek-v4.1-flash",
    )
    assert settings["lastChangelogVersion"] == "1.5.2"
    assert settings["quietStartup"] is True


def test_launch_argv_never_bypasses_permissions(tmp_path: Path) -> None:
    sops = tmp_path / "sops-exec"
    claude = launch_argv(
        Binary("claude", tmp_path / "claude", "2"), sops, session_id="s1", prompt="hi"
    )
    codex = launch_argv(Binary("codex", tmp_path / "codex", "1"), sops, prompt="hi")
    pi = launch_argv(Binary("pi", tmp_path / "pi", "1"), sops, pi_prefix=["/run/bin/pi", "/cli.js"])

    assert claude == [
        str(sops),
        str(tmp_path / "claude"),
        "--session-id",
        "s1",
        "--model",
        "haiku",
        "--permission-mode",
        "default",
        "hi",
    ]
    assert codex == [
        str(tmp_path / "codex"),
        "-m",
        "gpt-6-luna",
        "-a",
        "on-request",
        "-s",
        "read-only",
        "hi",
    ]
    assert pi[:3] == [str(sops), "/run/bin/pi", "/cli.js"]


def test_pi_entry_reads_the_shim_target_and_links_a_pi_named_runtime(tmp_path: Path) -> None:
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "pi"
    shim.write_text("#!/bin/sh\n# aube-bin-shim v2 target=../pkg/dist/cli.js\n")
    node = tmp_path / "node"
    node.write_text("")
    node.chmod(0o755)

    entry = pi_entry(shim, str(tmp_path))
    launcher = pi_launcher(_layout(tmp_path), entry)

    assert entry.script == (tmp_path / "node_modules" / "pkg" / "dist" / "cli.js").resolve()
    assert entry.node_path.startswith(str(tmp_path / "node_modules"))
    assert launcher.name == "pi"
    assert launcher.readlink() == node.resolve()


def test_pi_entry_without_a_shim_target_is_an_error(tmp_path: Path) -> None:
    shim = tmp_path / "pi"
    shim.write_text("#!/bin/sh\n")
    with pytest.raises(AgentSetupError, match="cannot find the node script"):
        pi_entry(shim, str(tmp_path))


def test_resolve_binary_reads_the_version_and_keeps_the_path_on_path(tmp_path: Path) -> None:
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\necho 'codex-cli 0.167.1'\n")
    fake.chmod(0o755)

    binary = resolve_binary("codex", {"PATH": f"{tmp_path}{os.pathsep}{os.defpath}"})

    assert binary == Binary("codex", fake, "0.167.1")


def test_resolve_binary_without_the_agent_is_an_error() -> None:
    with pytest.raises(AgentSetupError, match="claude not found"):
        resolve_binary("claude", {"PATH": os.defpath})


def test_claude_home_keeps_what_earlier_sessions_wrote(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    first = make_work(layout, "w1")
    second = make_work(layout, "w2")
    write_claude_home(layout, [first], "2.1.325", tmp_path / "user")
    config = layout.claude_dir / ".claude.json"
    config.write_text(
        json.dumps(json.loads(config.read_text()) | {"numStartups": 7, "userID": "abc"})
    )

    write_claude_home(layout, [second], "2.1.325", tmp_path / "user")

    written = json.loads(config.read_text())
    assert sorted(written["projects"]) == sorted([str(first.resolve()), str(second.resolve())])
    assert written["userID"] == "abc"
    assert list(layout.claude_dir.glob(".*.tmp")) == []
