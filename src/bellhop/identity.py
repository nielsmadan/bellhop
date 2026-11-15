import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bellhop.codes import CALLER_UNKNOWN
from bellhop.errors import BellhopError
from bellhop.models import Session

SESSION_VARS: Mapping[str, str] = {
    "CLAUDE_CODE_SESSION_ID": "claude",
    "CODEX_THREAD_ID": "codex",
    "PI_SESSION_ID": "pi",
}
START_TOLERANCE = timedelta(seconds=1)
LSTART = "%a %b %d %H:%M:%S %Y"
PS_SECONDS = 1.5


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    started: datetime | None


type ProcessTable = Mapping[int, Proc]


def parse_lstart(text: str) -> datetime | None:
    try:
        return datetime.strptime(" ".join(text.split()), LSTART).replace(tzinfo=UTC)
    except ValueError:
        return None


def _proc(line: str) -> Proc | None:
    pid, _, rest = line.strip().partition(" ")
    ppid, _, started = rest.strip().partition(" ")
    if not (pid.isdigit() and ppid.isdigit()):
        return None
    return Proc(int(pid), int(ppid), parse_lstart(started))


def parse_table(text: str) -> dict[int, Proc]:
    procs = [_proc(line) for line in text.splitlines()]
    return {proc.pid: proc for proc in procs if proc is not None}


def complete(table: ProcessTable, own_pid: int) -> ProcessTable | None:
    return table if own_pid in table else None


def process_table() -> ProcessTable | None:
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,lstart="],
            capture_output=True,
            text=True,
            timeout=PS_SECONDS,
            check=False,
            env={"PATH": "/bin:/usr/bin", "LC_ALL": "C", "TZ": "UTC0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return complete(parse_table(result.stdout), os.getpid())


def ancestors(pid: int, table: ProcessTable) -> list[Proc]:
    chain: list[Proc] = []
    current = table.get(pid)
    while current is not None and current.pid not in {proc.pid for proc in chain}:
        chain.append(current)
        current = table.get(current.ppid) if current.ppid > 0 else None
    return chain


def same_start(first: datetime | None, second: datetime | None) -> bool:
    return first is not None and second is not None and abs(first - second) <= START_TOLERANCE


def alive(pid: int | None, started: datetime | None, table: ProcessTable) -> bool:
    proc = table.get(pid) if pid is not None else None
    return proc is not None and (started is None or same_start(proc.started, started))


def _owner(session: Session, chain: Sequence[Proc]) -> int | None:
    return next(
        (
            depth
            for depth, proc in enumerate(chain)
            if proc.pid == session.pid and same_start(proc.started, session.process_started_at)
        ),
        None,
    )


def _unknown(message: str, candidates: Sequence[Session] = ()) -> BellhopError:
    return BellhopError(
        message, code=CALLER_UNKNOWN, candidates=[describe(session) for session in candidates]
    )


def describe(session: Session) -> str:
    return f"{session.session_id} ({session.harness}, {session.title or '-'})"


def _nearest(sessions: Sequence[Session], chain: Sequence[Proc]) -> list[Session]:
    depths = [(_owner(session, chain), session) for session in sessions]
    found = [(depth, session) for depth, session in depths if depth is not None]
    if not found:
        return []
    best = min(depth for depth, _ in found)
    return [session for depth, session in found if depth == best]


def identify(
    env: Mapping[str, str], sessions: Sequence[Session], table: ProcessTable | None, pid: int
) -> Session:
    chain = ancestors(pid, table or {})
    claimed = [(var, env[var], harness) for var, harness in SESSION_VARS.items() if env.get(var)]
    if claimed:
        found = {
            var: [s for s in sessions if s.session_id == value and s.harness == harness]
            for var, value, harness in claimed
        }
        unlisted = [f"{var}={env[var]}" for var, matches in found.items() if not matches]
        if unlisted:
            named = ", ".join(unlisted)
            raise _unknown(f"{named} is not a live session hooklinesinker lists")
        listed = [session for matches in found.values() for session in matches]
        if len(listed) == 1:
            return listed[0]
        nearest = _nearest(listed, chain)
        if len(nearest) == 1:
            return nearest[0]
        raise _unknown("the caller's environment names more than one live session", listed)
    nearest = _nearest(sessions, chain)
    if len(nearest) == 1:
        return nearest[0]
    if nearest:
        raise _unknown("the caller runs inside more than one live session", nearest)
    raise _unknown("the caller is not inside a session hooklinesinker knows")


def whoami(
    env: Mapping[str, str], sessions: Sequence[Session], table: ProcessTable | None
) -> Session:
    return identify(env, sessions, table, os.getpid())
