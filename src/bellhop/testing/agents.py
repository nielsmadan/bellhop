import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from bellhop.testing.screens import Agent

MODELS: dict[Agent, str] = {
    "claude": "haiku",
    "codex": "gpt-6-luna",
    "pi": "openrouter/deepseek/deepseek-v4.1-flash",
}
CODEX_EFFORT = "low"
CODEX_FEATURE = re.compile(r"[a-z][a-z0-9_]*")
PINNED_FEATURES = {"shell_snapshot": False}
PI_THINKING = "off"
PASSTHROUGH = (
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SOPS_SECRETS",
    "SOPS_AGE_KEY_FILE",
)
CLAUDE_MD_NAMES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", ".claude/rules/**")


class AgentSetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Binary:
    agent: Agent
    path: Path
    version: str


@dataclass(frozen=True)
class PiEntry:
    runtime: Path
    script: Path
    node_path: str


@dataclass(frozen=True)
class Layout:
    root: Path
    runtime_dir: Path

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def claude_dir(self) -> Path:
        return self.home / ".claude"

    @property
    def codex_dir(self) -> Path:
        return self.home / ".codex"

    @property
    def pi_dir(self) -> Path:
        return self.home / ".pi" / "agent"

    @property
    def xdg(self) -> Path:
        return self.root / "xdg"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def hls(self) -> Path:
        return self.xdg / "data" / "hooklinesinker" / "bin" / "hooklinesinker"


def _version(argv: Sequence[str], env: Mapping[str, str]) -> str:
    result = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=60, env=dict(env), check=False
    )
    found = re.search(r"(\d+\.\d+\.\d+\S*)", result.stdout + result.stderr)
    if found is None:
        raise AgentSetupError(f"cannot read a version from {argv[0]}")
    return found.group(1)


def resolve_binary(agent: Agent, env: Mapping[str, str]) -> Binary:
    found = shutil.which(agent, path=env.get("PATH", os.defpath))
    if found is None:
        raise AgentSetupError(f"{agent} not found on PATH")
    path = Path(found)
    return Binary(agent, path, _version([str(path), "--version"], env))


def pi_entry(shim: Path, search_path: str) -> PiEntry:
    found = re.search(r"target=(\S+)", shim.read_text(encoding="utf-8", errors="replace"))
    node = shutil.which("node", path=search_path)
    if found is None or node is None:
        raise AgentSetupError(f"cannot find the node script behind {shim}")
    modules = shim.parent.parent
    return PiEntry(
        Path(node).resolve(),
        (shim.parent / found.group(1)).resolve(),
        f"{modules}:{modules / '.mise' / 'node_modules'}",
    )


def pi_launcher(layout: Layout, entry: PiEntry) -> Path:
    launcher = layout.root / "bin" / "pi"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if not launcher.is_symlink():
        launcher.symlink_to(entry.runtime)
    return launcher


def isolated_env(layout: Layout, environ: Mapping[str, str]) -> dict[str, str]:
    kept = {key: environ[key] for key in PASSTHROUGH if key in environ}
    return kept | {
        "HOME": str(layout.home),
        "CLAUDE_CONFIG_DIR": str(layout.claude_dir),
        "CODEX_HOME": str(layout.codex_dir),
        "PI_CODING_AGENT_DIR": str(layout.pi_dir),
        "XDG_DATA_HOME": str(layout.xdg / "data"),
        "XDG_STATE_HOME": str(layout.xdg / "state"),
        "XDG_CONFIG_HOME": str(layout.xdg / "config"),
        "XDG_CACHE_HOME": str(layout.xdg / "cache"),
        "BELLHOP_RUNTIME_DIR": str(layout.runtime_dir),
        "TMPDIR": str(layout.tmp),
        "TERM": "tmux-256color",
        "DISABLE_AUTOUPDATER": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
    }


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, payload: object) -> None:
    _write_atomic(path, json.dumps(payload, indent=2) + "\n")


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def claude_md_excludes(work: Path, user_home: Path) -> list[str]:
    dirs = [*work.parents, user_home]
    return list(dict.fromkeys(str(d / name) for d in dirs for name in CLAUDE_MD_NAMES))


def write_claude_home(
    layout: Layout,
    works: Sequence[Path],
    version: str,
    user_home: Path,
    allowed: Sequence[str] = (),
) -> None:
    config_path = layout.claude_dir / ".claude.json"
    existing = _read_json(config_path)
    known = existing.get("projects")
    projects = (known if isinstance(known, dict) else {}) | {
        str(work.resolve()): {
            "hasTrustDialogAccepted": True,
            "hasCompletedProjectOnboarding": True,
            "projectOnboardingSeenCount": 1,
        }
        for work in works
    }
    _write_json(
        config_path,
        existing
        | {
            "hasCompletedOnboarding": True,
            "theme": "dark",
            "numStartups": 1,
            "autoUpdates": False,
            "lastReleaseNotesSeen": version,
            "officialMarketplaceAutoInstallAttempted": True,
            "projects": projects,
        },
    )
    settings_path = layout.claude_dir / "settings.json"
    settings = _read_json(settings_path)
    excludes = claude_md_excludes(layout.work.resolve(), user_home)
    _write_json(
        settings_path,
        settings
        | {
            "autoUpdates": False,
            "claudeMdExcludes": excludes,
            "permissions": {"allow": [f"Bash({command})" for command in allowed]},
        },
    )


def write_codex_home(
    layout: Layout, works: Sequence[Path], user_auth: Path, features: Sequence[str] = ()
) -> None:
    if not user_auth.is_file():
        raise AgentSetupError(f"{user_auth} missing; isolated Codex cannot authenticate")
    odd = [f for f in features if not CODEX_FEATURE.fullmatch(f)]
    if odd:
        raise AgentSetupError(f"not a codex feature name: {', '.join(map(repr, odd))}")
    pinned = [f for f in features if f in PINNED_FEATURES]
    if pinned:
        raise AgentSetupError(f"pinned off for the smoke run: {', '.join(pinned)}")
    switches = PINNED_FEATURES | dict.fromkeys(features, True)
    lines = [
        f"model = {json.dumps(MODELS['codex'])}",
        f"model_reasoning_effort = {json.dumps(CODEX_EFFORT)}",
        "check_for_update_on_startup = false",
        "",
        "[analytics]",
        "enabled = false",
        "",
        "[feedback]",
        "enabled = false",
        "",
        "[features]",
        *(f"{name} = {json.dumps(on)}" for name, on in switches.items()),
        "",
        "[notice]",
        "hide_full_access_warning = true",
        "hide_rate_limit_model_nudge = true",
        "",
    ]
    for work in works:
        lines += [f"[projects.{json.dumps(str(work.resolve()))}]", 'trust_level = "trusted"', ""]
    layout.codex_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(layout.codex_dir / "config.toml", "\n".join(lines))
    link = layout.codex_dir / "auth.json"
    if not link.is_symlink():
        link.symlink_to(user_auth)


def write_pi_home(layout: Layout, version: str) -> None:
    provider, model = MODELS["pi"].split("/", 1)
    settings_path = layout.pi_dir / "settings.json"
    _write_json(
        settings_path,
        {
            "defaultProvider": provider,
            "defaultModel": model,
            "defaultThinkingLevel": PI_THINKING,
            "defaultProjectTrust": "always",
            "packages": [],
            "enableInstallTelemetry": False,
            "lastChangelogVersion": version,
            "quietStartup": True,
        },
    )


def make_work(layout: Layout, name: str) -> Path:
    work = layout.work / name
    work.mkdir(parents=True, exist_ok=True)
    if not (work / ".git").exists():
        subprocess.run(
            ["git", "init", "-q", str(work)],
            capture_output=True,
            timeout=30,
            check=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(layout.home)},
        )
    return work


def launch_argv(
    binary: Binary,
    sops_exec: Path,
    *,
    session_id: str | None = None,
    extra: Sequence[str] = (),
    prompt: str | None = None,
    pi_prefix: Sequence[str] = (),
) -> list[str]:
    tail = [prompt] if prompt is not None else []
    match binary.agent:
        case "claude":
            sid = ["--session-id", session_id] if session_id else []
            return [
                str(sops_exec),
                str(binary.path),
                *sid,
                "--model",
                MODELS["claude"],
                "--permission-mode",
                "default",
                *extra,
                *tail,
            ]
        case "codex":
            policy = ["-a", "on-request", "-s", "read-only"]
            return [str(binary.path), "-m", MODELS["codex"], *policy, *extra, *tail]
        case "pi":
            sid = ["--session-id", session_id] if session_id else []
            return [
                str(sops_exec),
                *pi_prefix,
                "--model",
                MODELS["pi"],
                "--thinking",
                PI_THINKING,
                *sid,
                *extra,
                *tail,
            ]
