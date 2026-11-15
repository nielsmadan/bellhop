import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bellhop.errors import HlsUnavailable
from bellhop.models import HARNESSES, PHASES, Harness, Session
from bellhop.paths import data_home

PROTOCOL = 1
TIMEOUT_SECONDS = 5.0
ATTEMPTS = 2
OVERRIDE = "BELLHOP_HOOKLINESINKER"
NOT_FOUND = f"hooklinesinker not found; install it or set {OVERRIDE}"

type Runner = Callable[[Sequence[str], Mapping[str, str], float], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class SessionList:
    sessions: tuple[Session, ...]
    warnings: tuple[str, ...]


class _Malformed(ValueError):
    pass


def _run(
    argv: Sequence[str], env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=dict(env),
        stdin=subprocess.DEVNULL,
    )


def find_binary(env: Mapping[str, str]) -> Path:
    override = env.get(OVERRIDE)
    if override:
        return Path(override)
    active = data_home(env) / "hooklinesinker" / "bin" / "hooklinesinker"
    if active.is_file() and os.access(active, os.X_OK):
        return active
    found = shutil.which("hooklinesinker", path=env.get("PATH", os.defpath))
    if found is None:
        raise HlsUnavailable(NOT_FOUND)
    return Path(found)


def _invoke(
    binary: Path, env: Mapping[str, str], timeout: float, run: Runner
) -> subprocess.CompletedProcess[str]:
    for _ in range(ATTEMPTS):
        try:
            return run([str(binary), "sessions", "--json"], env, timeout)
        except subprocess.TimeoutExpired:
            continue
        except OSError as error:
            raise HlsUnavailable(f"cannot run {binary}: {error.strerror or error}") from error
    raise HlsUnavailable(f"hooklinesinker sessions --json timed out twice after {timeout:g}s")


def list_sessions(
    env: Mapping[str, str], *, timeout: float | None = None, run: Runner = _run
) -> SessionList:
    binary = find_binary(env)
    result = _invoke(binary, env, TIMEOUT_SECONDS if timeout is None else timeout, run)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no output"
        raise HlsUnavailable(f"hooklinesinker sessions --json exited {result.returncode}: {detail}")
    return parse_sessions(result.stdout)


def _load(text: str) -> dict[str, object]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise HlsUnavailable(f"hooklinesinker sessions --json is not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise HlsUnavailable("hooklinesinker sessions --json is not a JSON object")
    return payload


def _problem_text(problem: object) -> str:
    if isinstance(problem, dict) and "message" in problem:
        return str(problem["message"])
    return str(problem)


def _speaks_protocol(value: object) -> bool:
    return type(value) is int and value == PROTOCOL


def parse_sessions(text: str) -> SessionList:
    payload = _load(text)
    protocol = payload.get("protocol")
    if not _speaks_protocol(protocol):
        raise HlsUnavailable(
            f"hooklinesinker speaks protocol {protocol}; bellhop speaks protocol {PROTOCOL}"
        )
    records = payload.get("sessions")
    problems = payload.get("problems", [])
    if not isinstance(records, list) or not isinstance(problems, list):
        raise HlsUnavailable("hooklinesinker sessions --json lacks a sessions or problems list")
    outcomes = [_record(raw) for raw in records]
    return SessionList(
        sessions=tuple(outcome for outcome in outcomes if isinstance(outcome, Session)),
        warnings=(
            *(_problem_text(problem) for problem in problems),
            *(outcome for outcome in outcomes if isinstance(outcome, str)),
        ),
    )


def _excerpt(raw: object) -> str:
    return json.dumps(raw, ensure_ascii=False)[:120]


def _record(raw: object) -> Session | str | None:
    if not isinstance(raw, dict) or not _speaks_protocol(raw.get("protocol", PROTOCOL)):
        return f"skipped an unrecognized hooklinesinker record: {_excerpt(raw)}"
    harness = HARNESSES.get(str(raw.get("agent")))
    if harness is None or raw.get("running") is not True:
        return None
    try:
        return _session(raw, harness)
    except _Malformed:
        return f"skipped an unrecognized hooklinesinker record: {_excerpt(raw)}"


def _session(raw: Mapping[str, object], harness: Harness) -> Session:
    identity = _object(raw.get("session"))
    process = _optional_object(raw.get("process"))
    tmux = _optional_object(raw.get("tmux"))
    return Session(
        session_id=_text(identity.get("id")),
        binding_id=_text(raw.get("bindingId")),
        harness=harness,
        title=_optional_text(identity.get("title")),
        cwd=_optional_path(identity.get("cwd")),
        transcript_path=_optional_path(identity.get("transcriptPath")),
        phase=PHASES.get(str(raw.get("phase")), "unknown"),
        observed_at=_timestamp(raw.get("observedAt")),
        pid=_optional_int(process.get("pid")),
        process_started_at=_optional_timestamp(process.get("startedAt")),
        tmux_pane=_optional_text(tmux.get("pane")),
        tmux_socket=_optional_text(tmux.get("socket")),
    )


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _Malformed
    return value


def _optional_object(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _Malformed
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_path(value: object) -> Path | None:
    text = _optional_text(value)
    return Path(text) if text else None


def _optional_int(value: object) -> int | None:
    return value if type(value) is int else None


def _parse_time(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _timestamp(value: object) -> datetime:
    try:
        return _parse_time(_text(value))
    except ValueError as error:
        raise _Malformed from error


def _optional_timestamp(value: object) -> datetime | None:
    try:
        return _timestamp(value)
    except _Malformed:
        return None
