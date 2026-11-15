import argparse
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from bellhop.cli.context import Context
from bellhop.cli.presentation import (
    escape_controls,
    escape_text,
    print_json,
    print_lines,
    print_warnings,
)
from bellhop.codes import INTERNAL_ERROR, INTERRUPTED, NO_ROUTE, PUSH_REJECTED, USAGE
from bellhop.delivery import Channel, confirm_if_seen, new_id, plan_send, push
from bellhop.envelope import BODY_LIMIT, check_body, clean_sender, render
from bellhop.errors import BellhopError
from bellhop.identity import whoami
from bellhop.jsonout import message_object, ok_payload
from bellhop.models import Message, Session, TargetKind, Urgency
from bellhop.routes import Route
from bellhop.store import (
    EXPIRED_DETAIL,
    GONE_DETAIL,
    find_by_key,
    get_message,
    get_messages,
    insert_message,
    older_pending,
    set_degraded,
    take_inbox,
)
from bellhop.targets import Target, agent_of, live_session, resolve

CONFIRM_POLL_SECONDS = 0.2
NEVER_PUSHED = frozenset({EXPIRED_DETAIL, GONE_DETAIL})


@dataclass(frozen=True)
class Delivery:
    session: Session | None
    route: Route | None
    urgency: Urgency
    degraded_from: Urgency | None


def caller(ctx: Context) -> Session | None:
    try:
        return whoami(ctx.env, ctx.sessions.sessions, ctx.table)
    except BellhopError:
        return None


def default_sender(ctx: Context) -> str:
    session = caller(ctx)
    if session is None:
        return "unknown"
    return agent_of(session, ctx.bindings) or session.title or session.session_id


def target_label(target: Target) -> str:
    return target.agent or target.title or target.name


def choose(target: Target, urgency: Urgency, args: argparse.Namespace, ctx: Context) -> Delivery:
    session = target.session
    route = ctx.routes(session) if session is not None else None
    usable = route is not None and session is not None and route.reachable(session)
    if urgency == "hold":
        return Delivery(session, route if usable else None, urgency, None)
    if session is None:
        if args.wake:
            raise BellhopError(
                f"cannot wake '{target_label(target)}': launch is not available yet", code=NO_ROUTE
            )
        return Delivery(None, None, urgency, None)
    if usable:
        return Delivery(session, route, urgency, None)
    if args.or_hold:
        return Delivery(session, None, "hold", urgency)
    raise BellhopError(
        f"no usable route to '{target_label(target)}' ({target.harness or 'unknown harness'})",
        code=NO_ROUTE,
    )


@dataclass(frozen=True)
class Draft:
    body: str
    sender: str
    key: str | None
    wake: bool


def build(
    target: Target, delivery: Delivery, draft: Draft, *, message_id: str, now: datetime
) -> Message:
    kind: TargetKind = target.kind
    return Message(
        seq=0,
        id=message_id,
        key=draft.key,
        target_kind=kind,
        target=target.name,
        harness=target.harness,
        title=target.title,
        sender=draft.sender,
        body=draft.body,
        urgency=delivery.urgency,
        degraded_from=delivery.degraded_from,
        wake=draft.wake,
        state="pending",
        route=None,
        detail=None,
        session_id=delivery.session.session_id if delivery.session else None,
        transcript_offset=None,
        created_at=now,
        sent_at=None,
        confirmed_at=None,
    )


def addresses(target: Target, session: Session) -> list[tuple[TargetKind, str]]:
    agent: list[tuple[TargetKind, str]] = [("agent", target.agent)] if target.agent else []
    return [("session", session.session_id), *agent]


def deliver(stored: Message, target: Target, delivery: Delivery, ctx: Context) -> Message:
    session, route = delivery.session, delivery.route
    if session is None or route is None or stored.urgency == "hold":
        return stored
    older = older_pending(ctx.conn, addresses(target, session), stored.seq)
    plan = plan_send(stored.urgency, route, session, older_pending=older)
    if plan.degraded_from:
        stored = set_degraded(ctx.conn, stored.id, plan.degraded_from)
    if not plan.push:
        return stored
    channel = Channel(route, ctx.readers(session))
    return push(ctx.conn, stored, session, channel, ctx.now(), interrupt=plan.interrupt)


def wait_for_confirmation(
    stored: Message, session: Session, seconds: float, ctx: Context
) -> Message:
    reader = ctx.readers(session)
    if reader is None or stored.transcript_offset is None:
        ctx.warnings.append("cannot confirm: no transcript to read for this session")
        return stored
    deadline = time.monotonic() + seconds
    current = stored
    while current.state == "sent" and time.monotonic() < deadline:
        current = confirm_if_seen(ctx.conn, current, session, reader, ctx.now())
        if current.state == "sent":
            time.sleep(CONFIRM_POLL_SECONDS)
    if current.state == "sent":
        ctx.warnings.append(f"not confirmed within {seconds:g}s")
    return current


def emit_message(stored: Message, args: argparse.Namespace, ctx: Context) -> int:
    if stored.state == "failed" and stored.detail not in NEVER_PUSHED:
        raise BellhopError(
            f"the receiver rejected {stored.id}: {stored.detail or 'no detail'}",
            code=PUSH_REJECTED,
            stored=stored,
        )
    if args.json:
        print_json(ok_payload(ctx.warnings, {"message": message_object(stored)}))
        return 0
    print_warnings(ctx.warnings)
    print_lines([message_line(stored)])
    return 0


def message_line(message: Message) -> str:
    detail = f" ({escape_controls(message.detail)})" if message.detail else ""
    degraded = f" degraded from {message.degraded_from}" if message.degraded_from else ""
    return f"{message.id} {message.state}{degraded} -> {escape_controls(message.target)}{detail}"


def read_body(args: argparse.Namespace) -> str:
    if args.text is not None:
        return check_body(args.text)
    text = sys.stdin.read(BODY_LIMIT + 1)
    if len(text) > BODY_LIMIT:
        raise BellhopError(
            f"message body on stdin is over the limit of {BODY_LIMIT} characters", code=USAGE
        )
    return check_body(text)


def latest(ctx: Context, stored: Message) -> Message:
    try:
        return get_message(ctx.conn, stored.id)
    except Exception:  # noqa: BLE001
        return stored


def finish(
    stored: Message, target: Target, delivery: Delivery, args: argparse.Namespace, ctx: Context
) -> Message:
    try:
        stored = deliver(stored, target, delivery, ctx)
        ctx.kick()
        if args.confirm and stored.state == "sent" and delivery.session is not None:
            stored = wait_for_confirmation(stored, delivery.session, args.confirm, ctx)
    except BellhopError as error:
        error.stored = error.stored or latest(ctx, stored)
        raise
    except KeyboardInterrupt:
        raise BellhopError("interrupted", code=INTERRUPTED, stored=latest(ctx, stored)) from None
    except Exception as error:
        message = f"internal error: {type(error).__name__}: {error}"
        raise BellhopError(message, code=INTERNAL_ERROR, stored=latest(ctx, stored)) from error
    return stored


def send(args: argparse.Namespace, ctx: Context) -> int:
    body = read_body(args)
    if args.key is not None:
        existing = find_by_key(ctx.conn, args.key)
        if existing is not None:
            return emit_message(existing, args, ctx)
    target = resolve(args.target, ctx.sessions.sessions, ctx.config, ctx.bindings)
    delivery = choose(target, args.urgency, args, ctx)
    sender = clean_sender(args.sender or default_sender(ctx))
    draft = Draft(body, sender, args.key, bool(args.wake))
    message = build(target, delivery, draft, message_id=new_id(), now=ctx.now())
    stored = insert_message(ctx.conn, message)
    if stored.id != message.id:
        return emit_message(stored, args, ctx)
    return emit_message(finish(stored, target, delivery, args, ctx), args, ctx)


def status(args: argparse.Namespace, ctx: Context) -> int:
    found = get_messages(ctx.conn, args.ids)
    known = {message.id for message in found}
    ctx.warnings += [f"unknown message id {i}" for i in args.ids if i not in known]
    if args.json:
        print_json(ok_payload(ctx.warnings, {"messages": [message_object(m) for m in found]}))
        return 0
    print_warnings(ctx.warnings)
    print_lines([message_line(message) for message in found] or ["no such messages"])
    return 0


def inbox_targets(ctx: Context, target_text: str | None) -> list[tuple[TargetKind, str]]:
    if target_text is not None:
        target = resolve(target_text, ctx.sessions.sessions, ctx.config, ctx.bindings)
        session_key: list[tuple[TargetKind, str]] = (
            [("session", target.session.session_id)] if target.session else []
        )
        agent_key: list[tuple[TargetKind, str]] = [("agent", target.agent)] if target.agent else []
        return [*session_key, *agent_key]
    session = whoami(ctx.env, ctx.sessions.sessions, ctx.table)
    agents = [b.agent for b in ctx.bindings if live_session(b, [session]) is not None]
    return [("session", session.session_id), *(("agent", name) for name in agents)]


def inbox(args: argparse.Namespace, ctx: Context) -> int:
    taken = take_inbox(ctx.conn, inbox_targets(ctx, args.target), peek=args.peek, now=ctx.now())
    if args.json:
        print_json(ok_payload(ctx.warnings, {"messages": [message_object(m) for m in taken]}))
        return 0
    print_warnings(ctx.warnings)
    print_lines(inbox_lines(taken))
    return 0


def inbox_lines(messages: Sequence[Message]) -> list[str]:
    if not messages:
        return ["no messages waiting"]
    return [escape_text(render(m.id, m.sender, m.body, 0)) for m in messages]
