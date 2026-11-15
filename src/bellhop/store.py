import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bellhop.codes import STORE_ERROR
from bellhop.errors import BellhopError
from bellhop.models import (
    Binding,
    Message,
    MessageState,
    PushOutcome,
    PushResult,
    TargetKind,
    Urgency,
)

SCHEMA_VERSION = 3
BUSY_TIMEOUT_SECONDS = 30.0
WAL_RETRY_SECONDS = 0.05
FILE_NAME = "bellhop.sqlite"
EXPIRED_DETAIL = "expired"
GONE_DETAIL = "session gone"
REPLACED_DETAIL = "session replaced in its process"
STARTED_DETAIL = "started with this prompt"
UNKNOWN_DETAIL = "outcome unknown"
ABANDONED_DETAIL = "start abandoned: the agent's binding changed while it was starting"
OPENED_DETAIL = "prompt passed to tmux"
LAUNCH_ROUTE = "launch"

V1 = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  key TEXT UNIQUE,
  target_kind TEXT NOT NULL CHECK (target_kind IN ('session', 'agent')),
  target TEXT NOT NULL,
  harness TEXT,
  title TEXT,
  sender TEXT NOT NULL,
  body TEXT NOT NULL,
  urgency TEXT NOT NULL CHECK (urgency IN ('now', 'idle', 'hold')),
  degraded_from TEXT CHECK (degraded_from IN ('now', 'idle')),
  wake INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK (state IN ('pending', 'sending', 'sent', 'confirmed', 'read', 'failed')),
  route TEXT,
  detail TEXT,
  session_id TEXT,
  transcript_offset INTEGER,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  confirmed_at TEXT
)""",
    "CREATE INDEX messages_target ON messages (target_kind, target, state)",
    """CREATE TABLE bindings (
  agent TEXT PRIMARY KEY,
  harness TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('starting', 'live', 'failed')),
  session_id TEXT,
  pid INTEGER,
  process_started_at TEXT,
  app_server_socket TEXT,
  app_server_pid INTEGER,
  tmux_target TEXT,
  cause TEXT,
  detail TEXT,
  updated_at TEXT NOT NULL
)""",
)
V2 = (
    "ALTER TABLE messages ADD COLUMN finished_at TEXT",
    """UPDATE messages SET finished_at = coalesce(confirmed_at, sent_at, created_at)
    WHERE state IN ('confirmed', 'read', 'failed')""",
    """CREATE INDEX messages_open ON messages (state, seq)
    WHERE state IN ('pending', 'sending', 'sent')""",
)
V3 = ("ALTER TABLE messages ADD COLUMN transcript TEXT",)
MIGRATIONS: tuple[tuple[str, ...], ...] = (V1, V2, V3)
OPEN = "state IN ('pending', 'sending', 'sent')"
DELIVERABLE = """((state = 'pending' AND urgency != 'hold' AND (target_kind = 'session'
    OR EXISTS (SELECT 1 FROM bindings WHERE bindings.agent = messages.target
    AND bindings.state IN ('live', 'starting'))))
    OR state = 'sending')"""
CONFIRMABLE = """state = 'sent' AND transcript_offset IS NOT NULL AND session_id IS NOT NULL
    AND sent_at >= ?"""
SWEEP_SQL = f"""UPDATE messages SET state = 'sent', detail = '{UNKNOWN_DETAIL}'
    WHERE {OPEN} AND state = 'sending' AND sent_at < ?"""
AWAITING_SQL = f"SELECT * FROM messages WHERE {OPEN} AND {CONFIRMABLE} ORDER BY seq"
LAUNCHED_WHERE = f"""{OPEN} AND state = 'pending' AND target_kind = 'agent' AND target = ?
    AND route = ?"""
ABANDONED_WHERE = f"""{OPEN} AND state = 'pending' AND target_kind = 'agent' AND route = ?
    AND NOT EXISTS (SELECT 1 FROM bindings WHERE bindings.agent = messages.target
    AND bindings.state = 'starting')"""
ABANDON_SETS = """state = CASE WHEN detail IS NULL THEN 'failed' ELSE 'sent' END,
    sent_at = CASE WHEN detail IS NULL THEN sent_at ELSE ? END,
    finished_at = CASE WHEN detail IS NULL THEN ? ELSE finished_at END,
    detail = CASE WHEN detail IS NULL THEN ? ELSE ? END"""
HAS_WORK_SQL = f"""SELECT 1 FROM messages WHERE {OPEN} AND ({DELIVERABLE} OR ({CONFIRMABLE}))
    UNION ALL SELECT 1 FROM bindings WHERE state = 'starting' LIMIT 1"""
OUTCOME_STATES: Mapping[PushOutcome, MessageState] = {
    "accepted": "sent",
    "busy": "pending",
    "rejected": "failed",
    "unknown": "sent",
}

type Clock = Callable[[], float]
type Sleep = Callable[[float], None]


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text).replace(tzinfo=UTC) if text else None


def _store_error(path: Path, error: Exception) -> BellhopError:
    return BellhopError(f"cannot use the store at {path}: {error}", code=STORE_ERROR)


def enable_wal(
    conn: sqlite3.Connection,
    *,
    deadline_seconds: float = BUSY_TIMEOUT_SECONDS,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> None:
    deadline = clock() + deadline_seconds
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            busy = "locked" in str(error) or "busy" in str(error)
            if not busy or clock() >= deadline:
                raise
            sleep(WAL_RETRY_SECONDS)


def connect(path: Path) -> sqlite3.Connection:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600))
        conn = sqlite3.connect(
            path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None, check_same_thread=False
        )
    except (OSError, sqlite3.Error) as error:
        raise _store_error(path, error) from error
    try:
        conn.row_factory = sqlite3.Row
        enable_wal(conn)
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn, path)
    except BaseException as error:
        conn.close()
        if isinstance(error, sqlite3.Error):
            raise _store_error(path, error) from error
        raise
    return conn


def open_store(state_dir: Path) -> sqlite3.Connection:
    return connect(state_dir / FILE_NAME)


@contextmanager
def write(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not has_meta:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row else 0


def migrate(conn: sqlite3.Connection, path: Path) -> None:
    if schema_version(conn) == SCHEMA_VERSION:
        return
    for version, statements in enumerate(MIGRATIONS, start=1):
        with write(conn):
            current = schema_version(conn)
            if current > SCHEMA_VERSION:
                raise BellhopError(
                    f"the store at {path} has schema version {current}; this bellhop reads up to "
                    f"{SCHEMA_VERSION}; upgrade bellhop",
                    code=STORE_ERROR,
                )
            if current >= version:
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(version),),
            )


def _message(row: sqlite3.Row) -> Message:
    return Message(
        seq=row["seq"],
        id=row["id"],
        key=row["key"],
        target_kind=row["target_kind"],
        target=row["target"],
        harness=row["harness"],
        title=row["title"],
        sender=row["sender"],
        body=row["body"],
        urgency=row["urgency"],
        degraded_from=row["degraded_from"],
        wake=bool(row["wake"]),
        state=row["state"],
        route=row["route"],
        detail=row["detail"],
        session_id=row["session_id"],
        transcript_offset=row["transcript_offset"],
        transcript=Path(row["transcript"]) if row["transcript"] else None,
        created_at=parse_iso(row["created_at"]) or datetime.min.replace(tzinfo=UTC),
        sent_at=parse_iso(row["sent_at"]),
        confirmed_at=parse_iso(row["confirmed_at"]),
    )


def insert_message(conn: sqlite3.Connection, message: Message) -> Message:
    with write(conn):
        if message.key is not None:
            existing = find_by_key(conn, message.key)
            if existing is not None:
                return existing
        put_message(conn, message)
    return get_message(conn, message.id)


def put_message(conn: sqlite3.Connection, message: Message) -> None:
    conn.execute(
        """INSERT INTO messages (id, key, target_kind, target, harness, title, sender, body,
            urgency, degraded_from, wake, state, route, detail, session_id, transcript_offset,
            created_at, sent_at, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message.id,
            message.key,
            message.target_kind,
            message.target,
            message.harness,
            message.title,
            message.sender,
            message.body,
            message.urgency,
            message.degraded_from,
            int(message.wake),
            message.state,
            message.route,
            message.detail,
            message.session_id,
            message.transcript_offset,
            iso(message.created_at),
            None,
            None,
        ),
    )


def find_by_key(conn: sqlite3.Connection, key: str) -> Message | None:
    row = conn.execute("SELECT * FROM messages WHERE key = ?", (key,)).fetchone()
    return _message(row) if row else None


def get_message(conn: sqlite3.Connection, message_id: str) -> Message:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise BellhopError(f"message {message_id} vanished from the store", code=STORE_ERROR)
    return _message(row)


def get_messages(conn: sqlite3.Connection, ids: Sequence[str]) -> list[Message]:
    if not ids:
        return []
    found = {
        row["id"]: _message(row)
        for row in conn.execute(
            f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(ids))})", list(ids)
        )
    }
    return [found[message_id] for message_id in ids if message_id in found]


def older_pending(
    conn: sqlite3.Connection, addresses: Sequence[tuple[TargetKind, str]], seq: int
) -> bool:
    clause = " OR ".join("(target_kind = ? AND target = ?)" for _ in addresses)
    params = [value for address in addresses for value in address]
    row = conn.execute(
        f"""SELECT 1 FROM messages WHERE ({clause}) AND seq < ?
        AND urgency != 'hold' AND state IN ('pending', 'sending') LIMIT 1""",
        [*params, seq],
    ).fetchone()
    return row is not None


def holds_waiting(conn: sqlite3.Connection, kind: TargetKind, target: str) -> int:
    row = conn.execute(
        """SELECT count(*) AS n FROM messages WHERE target_kind = ? AND target = ?
        AND urgency = 'hold' AND state = 'pending'""",
        (kind, target),
    ).fetchone()
    return int(row["n"])


@dataclass(frozen=True)
class Push:
    session_id: str
    route: str
    offset: int | None = None
    transcript: Path | None = None


def claim(conn: sqlite3.Connection, message_id: str, push: Push, now: datetime) -> bool:
    transcript = str(push.transcript) if push.transcript else None
    with write(conn):
        cursor = conn.execute(
            """UPDATE messages SET state = 'sending', session_id = ?, route = ?,
            transcript_offset = ?, transcript = ?, sent_at = ? WHERE id = ? AND state = 'pending'""",
            (push.session_id, push.route, push.offset, transcript, iso(now), message_id),
        )
    return cursor.rowcount == 1


def record_outcome(
    conn: sqlite3.Connection, message_id: str, result: PushResult, now: datetime
) -> Message:
    state = OUTCOME_STATES[result.outcome]
    with write(conn):
        if state == "pending":
            conn.execute(
                """UPDATE messages SET state = 'pending', detail = ?, sent_at = NULL,
                transcript_offset = NULL, transcript = NULL WHERE id = ? AND state = 'sending'""",
                (result.detail, message_id),
            )
        else:
            finished = iso(now) if state == "failed" else None
            conn.execute(
                """UPDATE messages SET state = ?, detail = ?, sent_at = ?, finished_at = ?
                WHERE id = ? AND state = 'sending'""",
                (state, result.detail, iso(now), finished, message_id),
            )
    return get_message(conn, message_id)


def sweep_sending(conn: sqlite3.Connection, claimed_before: datetime) -> int:
    with write(conn):
        cursor = conn.execute(SWEEP_SQL, (iso(claimed_before),))
    return cursor.rowcount


def pending_targets(conn: sqlite3.Connection) -> list[tuple[TargetKind, str]]:
    rows = conn.execute(
        """SELECT target_kind, target, min(seq) AS first FROM messages
        WHERE state = 'pending' AND urgency != 'hold' GROUP BY target_kind, target ORDER BY first"""
    )
    return [(row["target_kind"], row["target"]) for row in rows]


def oldest_pending(conn: sqlite3.Connection, kind: TargetKind, target: str) -> Message | None:
    row = conn.execute(
        """SELECT * FROM messages WHERE target_kind = ? AND target = ? AND state = 'pending'
        AND urgency != 'hold' ORDER BY seq LIMIT 1""",
        (kind, target),
    ).fetchone()
    return _message(row) if row else None


def fail_pending(
    conn: sqlite3.Connection, kind: TargetKind, target: str, detail: str, now: datetime
) -> int:
    with write(conn):
        cursor = conn.execute(
            """UPDATE messages SET state = 'failed', detail = ?, finished_at = ?
            WHERE target_kind = ? AND target = ? AND state = 'pending' AND urgency != 'hold'""",
            (detail, iso(now), kind, target),
        )
    return cursor.rowcount


def expire_sessions(conn: sqlite3.Connection, created_before: datetime, now: datetime) -> int:
    with write(conn):
        cursor = conn.execute(
            """UPDATE messages SET state = 'failed', detail = ?, finished_at = ?
            WHERE target_kind = 'session' AND state = 'pending' AND urgency != 'hold'
            AND created_at < ?""",
            (EXPIRED_DETAIL, iso(now), iso(created_before)),
        )
    return cursor.rowcount


def awaiting_confirmation(conn: sqlite3.Connection, sent_after: datetime) -> list[Message]:
    rows = conn.execute(AWAITING_SQL, (iso(sent_after),))
    return [_message(row) for row in rows]


def confirm(conn: sqlite3.Connection, message_id: str, now: datetime) -> Message:
    with write(conn):
        conn.execute(
            """UPDATE messages SET state = 'confirmed', confirmed_at = ?, finished_at = ?
            WHERE id = ? AND state = 'sent'""",
            (iso(now), iso(now), message_id),
        )
    return get_message(conn, message_id)


def take_inbox(
    conn: sqlite3.Connection,
    targets: Sequence[tuple[TargetKind, str]],
    *,
    peek: bool,
    now: datetime,
) -> list[Message]:
    if not targets:
        return []
    clause = " OR ".join("(target_kind = ? AND target = ?)" for _ in targets)
    params = [value for target in targets for value in target]
    where = f"urgency = 'hold' AND state = 'pending' AND ({clause})"
    if peek:
        rows = list(conn.execute(f"SELECT * FROM messages WHERE {where} ORDER BY seq", params))
    else:
        with write(conn):
            rows = list(
                conn.execute(
                    f"UPDATE messages SET state = 'read', finished_at = ? WHERE {where} RETURNING *",
                    [iso(now), *params],
                )
            )
    return sorted((_message(row) for row in rows), key=lambda message: message.seq)


def pending_count(conn: sqlite3.Connection, kind: TargetKind, target: str) -> int:
    row = conn.execute(
        """SELECT count(*) AS n FROM messages WHERE target_kind = ? AND target = ?
        AND state = 'pending'""",
        (kind, target),
    ).fetchone()
    return int(row["n"])


def has_work(conn: sqlite3.Connection, confirm_after: datetime) -> bool:
    row = conn.execute(HAS_WORK_SQL, (iso(confirm_after),)).fetchone()
    return row is not None


def orphaned(conn: sqlite3.Connection) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM messages WHERE {DELIVERABLE}").fetchone()
    return int(row["n"])


def prune(conn: sqlite3.Connection, before: datetime) -> int:
    with write(conn):
        cursor = conn.execute(
            """DELETE FROM messages WHERE state IN ('confirmed', 'read', 'failed')
            AND coalesce(finished_at, created_at) < ?""",
            (iso(before),),
        )
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return cursor.rowcount


def _binding(row: sqlite3.Row) -> Binding:
    return Binding(
        agent=row["agent"],
        harness=row["harness"],
        state=row["state"],
        session_id=row["session_id"],
        pid=row["pid"],
        process_started_at=parse_iso(row["process_started_at"]),
        app_server_socket=row["app_server_socket"],
        app_server_pid=row["app_server_pid"],
        tmux_target=row["tmux_target"],
        cause=row["cause"],
        detail=row["detail"],
        updated_at=parse_iso(row["updated_at"]) or datetime.min.replace(tzinfo=UTC),
    )


def bindings(conn: sqlite3.Connection) -> list[Binding]:
    return [_binding(row) for row in conn.execute("SELECT * FROM bindings ORDER BY agent")]


def get_binding(conn: sqlite3.Connection, agent: str) -> Binding | None:
    row = conn.execute("SELECT * FROM bindings WHERE agent = ?", (agent,)).fetchone()
    return _binding(row) if row else None


def put_binding(conn: sqlite3.Connection, binding: Binding) -> None:
    started = iso(binding.process_started_at) if binding.process_started_at else None
    conn.execute(
        """INSERT OR REPLACE INTO bindings (agent, harness, state, session_id, pid,
        process_started_at, app_server_socket, app_server_pid, tmux_target, cause, detail, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            binding.agent,
            binding.harness,
            binding.state,
            binding.session_id,
            binding.pid,
            started,
            binding.app_server_socket,
            binding.app_server_pid,
            binding.tmux_target,
            binding.cause,
            binding.detail,
            iso(binding.updated_at),
        ),
    )


def save_binding(conn: sqlite3.Connection, binding: Binding) -> None:
    with write(conn):
        put_binding(conn, binding)


def save_binding_unless(
    conn: sqlite3.Connection, binding: Binding, refused: Callable[[Binding | None], bool]
) -> Binding | None:
    with write(conn):
        existing = get_binding(conn, binding.agent)
        if refused(existing):
            return existing
        put_binding(conn, binding)
    return None


def update_bindings(
    conn: sqlite3.Connection, refresh: Callable[[list[Binding]], list[Binding]]
) -> list[Binding]:
    with write(conn):
        before = bindings(conn)
        after = refresh(before)
        previous = {binding.agent: binding for binding in before}
        for binding in after:
            if previous.get(binding.agent) != binding:
                put_binding(conn, binding)
    return after


def begin_start(
    conn: sqlite3.Connection, binding: Binding, message: Message | None, now: datetime
) -> tuple[Message | None, Binding | None]:
    with write(conn):
        existing = find_by_key(conn, message.key) if message and message.key else None
        if existing is not None:
            return existing, None
        conn.execute(
            f"UPDATE messages SET {ABANDON_SETS} WHERE {LAUNCHED_WHERE}",
            [*abandon_values(now), binding.agent, LAUNCH_ROUTE],
        )
        put_binding(conn, binding)
        if message is not None:
            put_message(conn, message)
        stored = get_binding(conn, binding.agent)
    return (get_message(conn, message.id) if message is not None else None), stored


def launch_message(conn: sqlite3.Connection, agent: str) -> Message | None:
    row = conn.execute(
        f"SELECT * FROM messages WHERE {LAUNCHED_WHERE} ORDER BY seq LIMIT 1",
        (agent, LAUNCH_ROUTE),
    ).fetchone()
    return _message(row) if row else None


def _settle_start(
    conn: sqlite3.Connection, before: Binding, after: Binding, sets: str, values: list[object]
) -> bool:
    with write(conn):
        if get_binding(conn, before.agent) != before:
            return False
        put_binding(conn, after)
        conn.execute(
            f"UPDATE messages SET {sets} WHERE {LAUNCHED_WHERE}",
            [*values, before.agent, LAUNCH_ROUTE],
        )
    return True


def open_start(conn: sqlite3.Connection, before: Binding, after: Binding) -> bool:
    return _settle_start(
        conn, before, after, "detail = ?", [f"{OPENED_DETAIL} {after.tmux_target}"]
    )


def finish_start(conn: sqlite3.Connection, before: Binding, after: Binding, now: datetime) -> bool:
    return _settle_start(
        conn,
        before,
        after,
        "state = 'sent', session_id = ?, sent_at = ?, detail = ?",
        [after.session_id, iso(now), STARTED_DETAIL],
    )


def fail_start(
    conn: sqlite3.Connection, before: Binding, after: Binding, now: datetime, *, prompted: bool
) -> bool:
    if prompted:
        return _settle_start(
            conn,
            before,
            after,
            "state = 'sent', session_id = ?, sent_at = ?, detail = ?",
            [after.session_id, iso(now), f"{UNKNOWN_DETAIL}; {after.detail}"],
        )
    return _settle_start(
        conn,
        before,
        after,
        "state = 'failed', detail = ?, finished_at = ?",
        [after.detail, iso(now)],
    )


def abandon_starts(conn: sqlite3.Connection, now: datetime) -> int:
    found = conn.execute(
        f"SELECT 1 FROM messages WHERE {ABANDONED_WHERE} LIMIT 1", (LAUNCH_ROUTE,)
    ).fetchone()
    if found is None:
        return 0
    with write(conn):
        cursor = conn.execute(
            f"UPDATE messages SET {ABANDON_SETS} WHERE {ABANDONED_WHERE}",
            [*abandon_values(now), LAUNCH_ROUTE],
        )
    return cursor.rowcount


def abandon_values(now: datetime) -> list[object]:
    return [iso(now), iso(now), ABANDONED_DETAIL, f"{UNKNOWN_DETAIL}; {ABANDONED_DETAIL}"]


def set_degraded(conn: sqlite3.Connection, message_id: str, degraded_from: Urgency) -> Message:
    with write(conn):
        conn.execute(
            "UPDATE messages SET degraded_from = ? WHERE id = ? AND degraded_from IS NULL",
            (degraded_from, message_id),
        )
    return get_message(conn, message_id)
