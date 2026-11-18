import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from bellhop.codex_names import named
from bellhop.errors import HlsUnavailable
from bellhop.models import HARNESSES, PHASES, Harness, Session
from bellhop.paths import configured_dir, data_home

PROTOCOL = 1
BUDGET_SECONDS = 5.0
ATTEMPTS = 2
DETAIL_LIMIT = 200
FIRST_ATTEMPT_SHARE = 0.8
OVERRIDE = "BELLHOP_HOOKLINESINKER"
NOT_FOUND = f"hooklinesinker not found; install it or set {OVERRIDE}"

type Runner = Callable[[Sequence[str], Mapping[str, str], float], subprocess.CompletedProcess[str]]
type Clock = Callable[[], float]


@dataclass(frozen=True)
class SessionList:
    sessions: tuple[Session, ...]
    warnings: tuple[str, ...]


class _Malformed(ValueError):
    pass


def first_line(text: str) -> str:
    stripped = text.strip()
    line = stripped.splitlines()[0].strip() if stripped else ""
    return line if len(line) <= DETAIL_LIMIT else f"{line[:DETAIL_LIMIT]}…"


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


def attempt_timeout(attempt: int, budget: float, remaining: float) -> float:
    return remaining if attempt == ATTEMPTS - 1 else min(remaining, budget * FIRST_ATTEMPT_SHARE)


def _invoke(
    binary: Path, env: Mapping[str, str], budget: float, run: Runner, clock: Clock
) -> subprocess.CompletedProcess[str]:
    deadline = clock() + budget
    for attempt in range(ATTEMPTS):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            return run(
                [str(binary), "sessions", "--json"],
                env,
                attempt_timeout(attempt, budget, remaining),
            )
        except subprocess.TimeoutExpired:
            continue
        except OSError as error:
            raise HlsUnavailable(f"cannot run {binary}: {error.strerror or error}") from error
    raise HlsUnavailable(
        f"hooklinesinker sessions --json gave no answer within {budget:g}s ({ATTEMPTS} attempts)"
    )


def list_sessions(
    env: Mapping[str, str],
    *,
    budget: float = BUDGET_SECONDS,
    run: Runner = _run,
    clock: Clock = time.monotonic,
) -> SessionList:
    binary = find_binary(env)
    result = _invoke(binary, env, budget, run, clock)
    if result.returncode != 0:
        detail = first_line(result.stderr or result.stdout) or "no output"
        raise HlsUnavailable(f"hooklinesinker sessions --json exited {result.returncode}: {detail}")
    listed = parse_sessions(result.stdout)
    codex_home = configured_dir(env, "CODEX_HOME", ".codex")
    return replace(listed, sessions=named(listed.sessions, codex_home))


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
    return problem if isinstance(problem, str) else json.dumps(problem, ensure_ascii=False)


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
        sessions=collapse([outcome for outcome in outcomes if isinstance(outcome, Session)]),
        warnings=(
            *(f"hooklinesinker reports: {_problem_text(problem)}" for problem in problems),
            *(outcome for outcome in outcomes if isinstance(outcome, str)),
        ),
    )


def collapse(sessions: Sequence[Session]) -> tuple[Session, ...]:
    latest: dict[str, Session] = {}
    for session in sessions:
        kept = latest.get(session.session_id)
        if kept is None or session.observed_at >= kept.observed_at:
            latest[session.session_id] = session
    return tuple(session for session in sessions if latest[session.session_id] is session)


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
