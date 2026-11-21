import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bellhop.cli import main
from bellhop.routes.unix import deliver_header, exchange
from conftest import check_golden

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "src" / "bellhop" / "_integrations" / "pi" / "bellhop.ts"
TYPES = ROOT / ".cache" / "typescript" / "node_modules"
TSC = TYPES / "typescript" / "bin" / "tsc"
NODE = shutil.which("node")
UV = shutil.which("uv")
SEND_BUFFER = 2048
UV_CACHE = os.environ.get("UV_CACHE_DIR", str(ROOT / ".cache" / "uv"))

FAKE_PI = """
const mod = await import(process.argv[2]);
const calls = [];
const handlers = {};
const pi = {
  on: (name, fn) => { handlers[name] = fn; },
  sendUserMessage: (text, options) => calls.push({ text, options: options ?? null }),
};
mod.default(pi);
const net = await import("node:net");
const fs = await import("node:fs");
const socket = `${process.env.BELLHOP_RUNTIME_DIR}/pi-${process.pid}.sock`;
const ask = (payload) => new Promise((resolve, reject) => {
  const c = net.connect(socket, () => c.end(payload));
  let buf = "";
  c.on("data", (d) => (buf += d));
  c.on("end", () => resolve(buf));
  c.on("error", reject);
});
let sid = "s1";
const ctx = { sessionManager: { getSessionId: () => sid, getSessionFile: () => "/t/s1.jsonl" } };
await handlers.session_start({}, ctx);
for (let i = 0; i < 50 && !fs.existsSync(socket); i++) await new Promise((r) => setTimeout(r, 20));
const mode = fs.statSync(socket).mode & 0o777;
const replies = [];
const send = (when, id, body, count) =>
  ask(`deliver ${when} ${id} ${count ?? Buffer.byteLength(body)}\\n${body}`);
replies.push(await ask("ping\\n"));
replies.push(await send("boundary", "s1", "hello \\u00e9"));
replies.push(await send("interrupt", "s1", "now"));
replies.push(await send("boundary", "s1", ""));
replies.push(await send("boundary", "s0", "for the old session"));
replies.push(await ask("deliver boundary s1\\nno byte count"));
replies.push(await send("boundary", "s1", "cut short", 4000));
await handlers.session_before_compact({}, ctx);
replies.push(await send("boundary", "s1", "while compacting"));
await handlers.session_compact({}, ctx);
sid = null;
replies.push(await send("boundary", "s1", "to a session that has no id"));
await handlers.session_shutdown({ reason: "quit" }, ctx);
console.log(JSON.stringify({ replies, calls, mode, left: fs.existsSync(socket) }));
process.exit(0);
"""


def test_show_prints_the_packaged_file_and_where_pi_loads_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/opt/pi-agent")

    assert main(["integration", "show", "pi", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    fields = payload["integration"]
    assert Path(fields["file"]).read_text() == EXTENSION.read_text()
    assert fields["install_dir"] == "/opt/pi-agent/extensions"
    assert "install: bellhop integration export pi --to /opt/pi-agent/extensions" in fields["lines"]
    check_golden("integration-show", payload, {str(Path(fields["file"])): "<packaged>"})


def test_show_claude_prints_the_mcp_server_line_and_names_no_install_dir(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["integration", "show", "claude", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    fields = payload["integration"]
    assert fields["install_dir"] is None
    assert (
        "install for every session: claude mcp add --scope user bellhop -- bellhop _relay"
        in fields["lines"]
    )
    check_golden("integration-show-claude", payload, {str(Path(fields["file"])): "<packaged>"})


def test_export_claude_writes_an_mcp_config_that_starts_the_relay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "claude"

    assert main(["integration", "export", "claude", "--to", str(target)]) == 0

    config = json.loads((target / "bellhop-mcp.json").read_text())
    assert config == {"mcpServers": {"bellhop": {"command": "bellhop", "args": ["_relay"]}}}
    assert capsys.readouterr().out.startswith("exported the claude integration to ")


def test_export_copies_the_extension_and_writes_nothing_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "exts"

    assert main(["integration", "export", "pi", "--to", str(target), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    exported = target.resolve() / "bellhop.ts"
    assert [path.name for path in target.iterdir()] == ["bellhop.ts"]
    assert exported.read_text() == EXTENSION.read_text()
    assert payload["integration"] == {"harness": "pi", "file": str(exported)}
    check_golden("integration-export", payload, {str(exported): "<exported>"})


def test_export_replaces_a_symlink_instead_of_writing_through_it(tmp_path: Path) -> None:
    target = tmp_path / "exts"
    target.mkdir(mode=0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (target / "bellhop.ts").symlink_to(victim)

    assert main(["integration", "export", "pi", "--to", str(target)]) == 0

    assert victim.read_text() == "keep"
    assert (target / "bellhop.ts").is_symlink() is False


def test_export_refuses_a_directory_others_can_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)

    assert main(["integration", "export", "pi", "--to", str(shared)]) == 2

    assert capsys.readouterr().err == (
        f"bellhop: {shared} must be owned by you and writable only by you\n"
    )


def test_export_into_an_unwritable_place_is_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")

    assert main(["integration", "export", "pi", "--to", str(blocker / "sub")]) == 1

    assert capsys.readouterr().err.startswith(f"bellhop: cannot export to {blocker / 'sub'}: ")


def test_integration_needs_an_action(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["integration"]) == 2

    assert "ACTION" in capsys.readouterr().err


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_extension_answers_the_wire_and_cleans_up_its_socket(
    tmp_path: Path, short_tmp: Path
) -> None:
    runtime = short_tmp / "rt"
    script = tmp_path / "fake-pi.mjs"
    script.write_text(FAKE_PI)
    try:
        result = subprocess.run(
            [str(NODE), str(script), str(EXTENSION)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "BELLHOP_RUNTIME_DIR": str(runtime)},
        )
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        assert outcome["replies"] == [
            'ok pong {"wire":2,"session":"s1","transcript":"/t/s1.jsonl"}\n',
            "ok followUp\n",
            "ok steer\n",
            "error empty message\n",
            "error session_mismatch s1\n",
            "error wire_version\n",
            "error truncated\n",
            "busy compacting\n",
            "error session_mismatch unknown\n",
        ]
        assert outcome["calls"] == [
            {"text": "hello é", "options": {"deliverAs": "followUp"}},
            {"text": "now", "options": {"deliverAs": "steer"}},
        ]
        assert (outcome["mode"], outcome["left"]) == (0o600, False)
        assert runtime.stat().st_mode & 0o777 == 0o700
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_extension_will_not_listen_in_a_directory_others_can_read(
    tmp_path: Path, short_tmp: Path
) -> None:
    runtime = short_tmp / "rt"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)
    script = tmp_path / "start.mjs"
    script.write_text(
        "const mod = await import(process.argv[2]); const h = {};"
        "mod.default({ on: (n, f) => { h[n] = f; }, sendUserMessage: () => {} });"
        "await h.session_start({}, {}); await new Promise((r) => setTimeout(r, 300));"
        "process.exit(0);"
    )

    subprocess.run(
        [str(NODE), str(script), str(EXTENSION)],
        timeout=30,
        check=True,
        env={**os.environ, "BELLHOP_RUNTIME_DIR": str(runtime)},
    )

    assert stat.S_IMODE(runtime.stat().st_mode) == 0o755
    assert list(runtime.iterdir()) == []


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_extension_type_checks_against_pis_types(tmp_path: Path) -> None:
    package = TYPES / "@earendil-works" / "pi-coding-agent"
    assert TSC.exists() and (package / "dist" / "index.d.ts").exists(), "run `just typescript`"
    config = {
        "compilerOptions": {
            "strict": True,
            "noEmit": True,
            "target": "es2022",
            "module": "esnext",
            "moduleResolution": "bundler",
            "skipLibCheck": True,
            "types": ["node"],
            "typeRoots": [str(TYPES / "@types")],
            "paths": {"@earendil-works/pi-coding-agent": [str(package / "dist" / "index.d.ts")]},
        },
        "files": [str(EXTENSION)],
    }
    (tmp_path / "tsconfig.json").write_text(json.dumps(config))

    result = subprocess.run(
        [str(NODE), str(TSC), "-p", str(tmp_path / "tsconfig.json")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.packaging
@pytest.mark.skipif(UV is None, reason="uv not installed")
def test_the_wheel_carries_the_skill_and_the_integrations(tmp_path: Path) -> None:
    assert UV is not None
    result = subprocess.run(
        [UV, "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={
            **os.environ,
            "UV_CACHE_DIR": UV_CACHE,
        },
    )
    assert result.returncode == 0, result.stderr
    wheel = next(tmp_path.glob("bellhop-*.whl"))

    names = zipfile.ZipFile(wheel).namelist()
    assert "bellhop/_integrations/pi/bellhop.ts" in names
    assert "bellhop/_integrations/claude/bellhop-mcp.json" in names
    assert "bellhop/_skills/bellhop/SKILL.md" in names


PARITY = """
const mod = await import(process.argv[2]);
console.log(JSON.stringify(mod.runtimeDir(process.env, process.getuid()) ?? null));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("config", "env", "expected"),
    [
        ('runtime_dir = "/tmp/bh-parity"\n', {}, "/tmp/bh-parity"),
        ("runtime_dir = '/tmp/bh-parity'  # here\n", {}, "/tmp/bh-parity"),
        ('runtime_dir = "rel/dir"\n', {}, None),
        (
            'note = """\nruntime_dir = "/tmp/evil"\n"""\nruntime_dir = "/tmp/good"\n',
            {},
            "/tmp/good",
        ),
        ('runtime_dir = "/tmp/a\\u0062"\n', {}, None),
        ("", {"BELLHOP_RUNTIME_DIR": "rt"}, None),
        ("", {}, "default"),
    ],
)
def test_the_extension_picks_the_runtime_dir_bellhop_does(
    tmp_path: Path, config: str, env: dict[str, str], expected: str | None
) -> None:
    config_home = tmp_path / "config"
    (config_home / "bellhop").mkdir(parents=True)
    (config_home / "bellhop" / "config.toml").write_text(config)
    script = tmp_path / "parity.mjs"
    script.write_text(PARITY)
    base = {k: v for k, v in os.environ.items() if k != "BELLHOP_RUNTIME_DIR"}

    result = subprocess.run(
        [str(NODE), str(script), str(EXTENSION)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={**base, "XDG_CONFIG_HOME": str(config_home), **env},
    )

    chosen = json.loads(result.stdout)
    assert chosen == (f"/tmp/bellhop-{os.getuid()}" if expected == "default" else expected)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_runtime_dir_entry_in_the_way_does_not_crash_pi(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="bh-way-", dir="/tmp"))
    script = tmp_path / "way.mjs"
    script.write_text(
        "const fs = await import('node:fs');"
        "fs.mkdirSync(`${process.env.BELLHOP_RUNTIME_DIR}/pi-${process.pid}.sock/x`, { recursive: true });"
        "const mod = await import(process.argv[2]); const h = {};"
        "mod.default({ on: (n, f) => { h[n] = f; }, sendUserMessage: () => {} });"
        "await h.session_start({}, {}); await new Promise((r) => setTimeout(r, 300));"
        "console.log(JSON.stringify(fs.readdirSync(process.env.BELLHOP_RUNTIME_DIR).sort()));"
    )
    try:
        runtime.chmod(0o700)
        result = subprocess.run(
            [str(NODE), str(script), str(EXTENSION)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "BELLHOP_RUNTIME_DIR": str(runtime)},
        )
    finally:
        shutil.rmtree(runtime, ignore_errors=True)

    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 1


STALL = """
const mod = await import(process.argv[2]);
const calls = [];
const h = {};
mod.default({ on: (n, f) => { h[n] = f; }, sendUserMessage: (text) => calls.push(text.length) });
const ctx = { sessionManager: { getSessionId: () => "s1", getSessionFile: () => "/t/s1.jsonl" } };
await h.session_start({}, ctx);
const fs = await import("node:fs");
const socket = `${process.env.BELLHOP_RUNTIME_DIR}/pi-${process.pid}.sock`;
for (let i = 0; i < 50 && !fs.existsSync(socket); i++) await new Promise((r) => setTimeout(r, 20));
console.log(socket);
const until = Date.now() + 3500;
while (Date.now() < until) {}
await new Promise((r) => setTimeout(r, 1500));
console.log(JSON.stringify(calls));
process.exit(0);
"""


class SmallSendBuffer(socket.socket):
    def __init__(self, family: int = socket.AF_UNIX) -> None:
        super().__init__(family)
        self.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_body_cut_short_by_a_stalled_pi_is_never_delivered(
    tmp_path: Path, short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = SimpleNamespace(socket=SmallSendBuffer, AF_UNIX=socket.AF_UNIX, SHUT_WR=socket.SHUT_WR)
    monkeypatch.setattr("bellhop.routes.unix.socket", small)
    script = tmp_path / "stall.mjs"
    script.write_text(STALL)
    body = "[bellhop bh_1 from x] " + "日" * 7900
    with subprocess.Popen(
        [str(NODE), str(script), str(EXTENSION)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "BELLHOP_RUNTIME_DIR": str(short_tmp)},
    ) as proc:
        assert proc.stdout is not None
        socket_path = Path(proc.stdout.readline().strip())
        result = exchange(socket_path, deliver_header("boundary", "s1", body), body)
        delivered = json.loads(proc.stdout.readline())

    assert result.outcome == "busy"
    assert result.detail.startswith("the write did not finish")
    assert delivered == []


def test_the_skill_exports_into_a_bellhop_folder_the_same_way_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skills = tmp_path / "skills"

    first = main(["integration", "export", "skill", "--to", str(skills)])
    second = main(["integration", "export", "skill", "--to", str(skills)])

    assert (first, second) == (0, 0)
    assert [p.relative_to(skills) for p in sorted(skills.rglob("*"))] == [
        Path("bellhop"),
        Path("bellhop/SKILL.md"),
    ]
    assert (skills / "bellhop" / "SKILL.md").read_text().startswith("---\nname: bellhop\n")
    capsys.readouterr()
    assert main(["integration", "show", "skill"]) == 0
    assert "export skill --to" in capsys.readouterr().out


def test_show_codex_says_there_is_nothing_to_install(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["integration", "show", "codex", "--json"]) == 0

    integration = json.loads(capsys.readouterr().out)["integration"]
    assert (integration["file"], integration["install_dir"]) == (None, None)
    assert integration["lines"][0].startswith("Codex needs nothing installed")


def test_export_lists_what_it_accepts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["integration", "export", "codex", "--to", str(tmp_path)]) == 2

    assert "(choose from claude, pi, skill)" in capsys.readouterr().err.replace("'", "")
