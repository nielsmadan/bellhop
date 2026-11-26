import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bellhop.cli.codex import Remote, for_remote, plan, wrapped
from bellhop.trace import read_events

FAKE_CODEX = """#!{python}
import json, os, socket, sys, time
with open(os.environ["FAKE_CODEX_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["app-server"]:
    if os.environ.get("FAKE_CODEX_SERVER") == "dies":
        sys.exit(3)
    listen = sys.argv[sys.argv.index("--listen") + 1]
    server = socket.socket(socket.AF_UNIX)
    server.bind(listen.removeprefix("unix://"))
    server.listen(1)
    time.sleep(60)
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
"""


@pytest.fixture
def codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    program = tmp_path / "bin" / "codex"
    program.parent.mkdir()
    program.write_text(FAKE_CODEX.format(python=sys.executable))
    program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{program.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
    return tmp_path / "codex.log"


def bellhop_codex(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bellhop", "codex", *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def runtime_dir() -> Path:
    return Path(os.environ["BELLHOP_RUNTIME_DIR"])


@pytest.mark.parametrize(
    ("arguments", "overrides"),
    [
        ([], []),
        (["fix the build"], []),
        (["-c", "a=1", "--model", "m", "--enable", "x", "hello"], ["-c", "a=1", "--enable", "x"]),
        (["--config=a=1", "-cb=2", "resume", "--last"], ["--config=a=1", "-cb=2"]),
        (["-m", "exec", "--", "exec"], []),
        (["resume", "--last", "-c", "a=1"], ["-c", "a=1"]),
        (["fix it", "--enable", "x"], ["--enable", "x"]),
        (["-i", "a.png", "b.png", "-c", "a=1"], ["-c", "a=1"]),
        (["resume", "--profile", "work"], None),
        (["fork", "--oss"], None),
        (["resume", "--help"], None),
        (["hello", "--", "--remote"], []),
        (["exec", "hello"], None),
        (["-c", "a=1", "login"], None),
        (["--remote", "unix:///x"], None),
        (["--no-daemon"], None),
        (["--profile", "work"], None),
        (["--version"], None),
        (["-h"], None),
    ],
)
def test_only_an_interactive_codex_gets_its_own_app_server(
    arguments: list[str], overrides: list[str] | None
) -> None:
    assert wrapped(arguments) == overrides


def test_the_terminal_ui_keeps_every_argument_and_the_server_gets_the_overrides() -> None:
    made = plan(["-c", "a=1", "hello"], ["-c", "a=1"], Path("/rt/codex-7.sock"))

    assert made.server == [
        "codex",
        "app-server",
        "-c",
        "a=1",
        "--listen",
        "unix:///rt/codex-7.sock",
    ]
    assert made.tui == ["codex", "--remote", "unix:///rt/codex-7.sock", "-c", "a=1", "hello"]


def test_a_session_runs_on_its_own_server_and_cleans_up(codex: Path) -> None:
    done = bellhop_codex("-c", "a=1", "hello")

    assert (done.returncode, done.stderr) == (0, "")
    server, tui = calls(codex)
    listen = server[-1]
    assert server == ["app-server", "-c", "a=1", "--listen", listen]
    assert Path(listen.removeprefix("unix://")).parent == runtime_dir()
    assert Path(listen.removeprefix("unix://")).name.startswith("codex-")
    assert tui == ["--remote", listen, "-c", "a=1", "hello"]
    assert list(runtime_dir().iterdir()) == []
    state = Path(os.environ["XDG_STATE_HOME"]) / "bellhop"
    assert [path.name for path in state.iterdir()] == ["trace.jsonl"]
    traced = [event["event"] for event in read_events(os.environ, count=10)]
    assert traced == ["start", "listening", "stop"]


def test_the_terminal_uis_exit_code_is_the_commands(
    codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_EXIT", "7")

    assert bellhop_codex("hello").returncode == 7


def test_a_subcommand_runs_as_plain_codex(codex: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_EXIT", "5")

    done = bellhop_codex("exec", "--json", "hello")

    assert done.returncode == 5
    assert calls(codex) == [["exec", "--json", "hello"]]


def test_a_server_that_does_not_start_falls_back_to_plain_codex(
    codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SERVER", "dies")

    done = bellhop_codex("-c", "a=1", "hello")

    assert done.returncode == 0
    assert [call[0] for call in calls(codex)] == ["app-server", "-c"]
    assert calls(codex)[1] == ["-c", "a=1", "hello"]
    assert "bellhop cannot reach this session" in done.stderr
    assert list(runtime_dir().iterdir()) == []


def test_an_unusable_runtime_dir_falls_back_to_plain_codex(
    codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", "relative/dir")

    done = bellhop_codex("hello")

    assert done.returncode == 0
    assert calls(codex) == [["hello"]]
    assert "BELLHOP_RUNTIME_DIR must be an absolute path" in done.stderr


def test_a_state_dir_that_cannot_hold_the_server_log_falls_back_to_plain_codex(
    codex: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the state home should be")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))

    done = bellhop_codex("hello")

    assert done.returncode == 0
    assert calls(codex) == [["hello"]]
    assert "bellhop cannot reach this session" in done.stderr
    assert list(runtime_dir().iterdir()) == []


def test_a_missing_codex_is_an_error_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")

    wrapped_run, plain_run = bellhop_codex("hello"), bellhop_codex("exec", "hello")

    assert (wrapped_run.returncode, plain_run.returncode) == (1, 1)
    assert "Traceback" not in wrapped_run.stderr + plain_run.stderr
    assert "cannot run codex" in wrapped_run.stderr
    assert "cannot run codex" in plain_run.stderr


SANDBOXED = ["-c", 'sandbox_mode="danger-full-access"', "-c", 'approval_policy="never"']
FULL = 'sandbox_mode="danger-full-access"'
NEVER = 'approval_policy="never"'
AUTO_REVIEWER = ["-c", 'approvals_reviewer="auto_review"']
ON_REQUEST = 'approval_policy="on-request"'
WORKSPACE = 'sandbox_mode="workspace-write"'


@pytest.mark.parametrize(
    ("arguments", "remote"),
    [
        ([*SANDBOXED, "resume", "t1"], Remote(["resume", "t1"], [], [])),
        (["-s", "danger-full-access", "resume", "t1"], Remote(["resume", "t1"], ["-c", FULL], [])),
        (["--sandbox=danger-full-access", "fork", "t1"], Remote(["fork", "t1"], ["-c", FULL], [])),
        (["-a", "never", "fork", "t1"], Remote(["fork", "t1"], ["-c", NEVER], [])),
        (
            ["resume", "--dangerously-bypass-approvals-and-sandbox", "t1"],
            Remote(["resume", "t1"], ["-c", FULL, "-c", NEVER], []),
        ),
        (["--yolo", "fork", "t1"], Remote(["fork", "t1"], ["-c", FULL, "-c", NEVER], [])),
        (
            ["resume", "--approve-for-me", "t1"],
            Remote(["resume", "t1"], [*AUTO_REVIEWER, "-c", ON_REQUEST, "-c", WORKSPACE], []),
        ),
        (
            ["--not-so-yolo", "resume", "t1"],
            Remote(["resume", "t1"], [*AUTO_REVIEWER, "-c", ON_REQUEST, "-c", WORKSPACE], []),
        ),
        (
            ["-c", "sandbox_workspace_write.network_access=true", "--config=network.x=1", "resume"],
            Remote(["resume"], [], []),
        ),
        (
            ["-c", 'model_reasoning_effort="high"', "resume", "t1"],
            Remote(["-c", 'model_reasoning_effort="high"', "resume", "t1"], [], []),
        ),
        (
            ["resume", "--add-dir", "/x", "--add-dir=/y", "t1"],
            Remote(["resume", "t1"], [], ["--add-dir", "/x", "--add-dir=/y"]),
        ),
        (["resume", "t1", "--", "-s"], Remote(["resume", "t1", "--", "-s"], [], [])),
        ([*SANDBOXED, "hello"], Remote([*SANDBOXED, "hello"], [], [])),
        (["-s", "danger-full-access"], Remote(["-s", "danger-full-access"], [], [])),
    ],
)
def test_a_resume_or_fork_leaves_permission_overrides_to_the_server(
    arguments: list[str], remote: Remote
) -> None:
    assert for_remote(arguments) == remote


@pytest.mark.parametrize(
    ("arguments", "server_extra", "tui_rest"),
    [
        ([*SANDBOXED, "resume", "t1"], SANDBOXED, ["resume", "t1"]),
        (["-s", "danger-full-access", "fork", "t1"], ["-c", FULL], ["fork", "t1"]),
    ],
)
def test_a_resumed_session_runs_with_its_permissions_on_the_server_only(
    codex: Path, arguments: list[str], server_extra: list[str], tui_rest: list[str]
) -> None:
    done = bellhop_codex(*arguments)

    server, tui = calls(codex)
    listen = server[-1]
    assert (done.returncode, done.stderr) == (0, "")
    assert server == ["app-server", *server_extra, "--listen", listen]
    assert tui == ["--remote", listen, *tui_rest]


def test_a_resume_names_the_options_it_cannot_pass_on(codex: Path) -> None:
    done = bellhop_codex("resume", "--add-dir", "/x", "t1")

    assert calls(codex)[1][2:] == ["resume", "t1"]
    assert done.stderr == (
        "bellhop: codex cannot take --add-dir /x when resuming or forking through an app server; "
        "left it off\n"
    )
