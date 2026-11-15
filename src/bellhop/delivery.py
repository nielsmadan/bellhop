import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from bellhop.envelope import prefix, render
from bellhop.models import Message, PushResult, Session, Urgency
from bellhop.routes import Route
from bellhop.store import (
    Push,
    claim,
    confirm,
    get_message,
    holds_waiting,
    record_outcome,
    set_degraded,
)
from bellhop.transcripts import TranscriptReader

BUSY_PHASES = frozenset({"permission", "compacting", "unknown"})


@dataclass(frozen=True)
class Channel:
    route: Route
    reader: TranscriptReader | None = None


@dataclass(frozen=True)
class Mode:
    interrupt: bool
    degraded_from: Urgency | None


@dataclass(frozen=True)
class Plan:
    push: bool
    interrupt: bool
    degraded_from: Urgency | None


def new_id() -> str:
    return f"bh_{secrets.token_hex(6)}"


def ready_for_idle(route: Route, session: Session) -> bool:
    if route.queues_until_idle:
        return True
    busy = route.busy(session)
    return busy is False or (busy is None and session.phase == "idle")


def push_mode(urgency: Urgency, route: Route) -> Mode:
    if urgency != "now":
        return Mode(interrupt=False, degraded_from=None)
    if route.can_interrupt:
        return Mode(interrupt=True, degraded_from=None)
    return Mode(interrupt=False, degraded_from="now")


def plan_send(urgency: Urgency, route: Route, session: Session, *, older_pending: bool) -> Plan:
    if urgency == "hold":
        return Plan(push=False, interrupt=False, degraded_from=None)
    mode = push_mode(urgency, route)
    if older_pending:
        return Plan(push=False, interrupt=mode.interrupt, degraded_from=mode.degraded_from)
    ready = mode.interrupt or ready_for_idle(route, session)
    return Plan(push=ready, interrupt=mode.interrupt, degraded_from=mode.degraded_from)


def drainer_may_push(route: Route, session: Session, urgency: Urgency) -> bool:
    if session.phase in BUSY_PHASES:
        return False
    return push_mode(urgency, route).interrupt or ready_for_idle(route, session)


def push(
    conn: sqlite3.Connection,
    message: Message,
    session: Session,
    channel: Channel,
    now: datetime,
    *,
    interrupt: bool,
) -> Message:
    route, reader = channel.route, channel.reader
    offset = reader.size(session) if reader is not None else None
    transcript = reader.path(session) if reader is not None and offset is not None else None
    if not claim(conn, message.id, Push(session.session_id, route.name, offset, transcript), now):
        return get_message(conn, message.id)
    waiting = holds_waiting(conn, message.target_kind, message.target)
    text = render(message.id, message.sender, message.body, waiting)
    try:
        result = route.push(session, text, interrupt=interrupt)
    except Exception as error:  # noqa: BLE001
        result = PushResult("unknown", f"push raised {type(error).__name__}: {error}")
    return record_outcome(conn, message.id, result, now)


def push_waiting(
    conn: sqlite3.Connection, message: Message, session: Session, channel: Channel, now: datetime
) -> Message:
    mode = push_mode(message.urgency, channel.route)
    if mode.degraded_from is not None:
        message = set_degraded(conn, message.id, mode.degraded_from)
    return push(conn, message, session, channel, now, interrupt=mode.interrupt)


def seen(message: Message, session: Session, reader: TranscriptReader | None) -> bool:
    if reader is None or message.transcript_offset is None:
        return False
    return reader.contains(
        session, prefix(message.id), since=message.transcript_offset, path=message.transcript
    )


def confirm_if_seen(
    conn: sqlite3.Connection,
    message: Message,
    session: Session,
    reader: TranscriptReader | None,
    now: datetime,
) -> Message:
    if message.state == "sent" and seen(message, session, reader):
        return confirm(conn, message.id, now)
    return message
