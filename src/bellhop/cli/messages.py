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
from bellhop.cli.waking import key_reused, replay_warnings, wake
from bellhop.codes import INTERNAL_ERROR, INTERRUPTED, LAUNCH_FAILED, NO_ROUTE, PUSH_REJECTED, USAGE
from bellhop.delivery import Channel, confirm_if_seen, new_id, plan_send, push
from bellhop.envelope import BODY_LIMIT, check_body, check_controls, clean_sender, render
from bellhop.errors import BellhopError
from bellhop.identity import whoami
from bellhop.jsonout import message_object, ok_payload
from bellhop.launch import START_FAILED
from bellhop.models import Message, Session, TargetKind, Urgency
from bellhop.routes import Route
from bellhop.store import (
    ABANDONED_DETAIL,
    EXPIRED_DETAIL,
    GONE_DETAIL,
    REPLACED_DETAIL,
    find_by_key,
    get_message,
    get_messages,
    insert_message,
    older_pending,
    set_degraded,
    take_inbox,
)
from bellhop.targets import (
    PREFIX_MINIMUM,
    Target,
    agent_of,
    binding_for,
    live_session,
    resolve,
)

CONFIRM_POLL_SECONDS = 0.2
NEVER_PUSHED = frozenset({EXPIRED_DETAIL, GONE_DETAIL, REPLACED_DETAIL})


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


def choose(target: Target, urgency: Urgency, ctx: Context, *, or_hold: bool) -> Delivery:
    session = target.session
    if urgency == "hold" or session is None:
        return Delivery(session, None, urgency, None)
    route = ctx.routes(session, binding_for(session, ctx.bindings))
    if route is not None and ctx.reachable(route, session, strict=not or_hold):
        return Delivery(session, route, urgency, None)
    if or_hold:
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
    target: Target, delivery: Delivery, draft: Draft, message_id: str, now: datetime
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
        transcript=None,
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
    older = older_pending(ctx.conn, addresses(target, session), stored.seq, stored.urgency)
    plan = plan_send(stored.urgency, route, session, older_pending=older)
    if plan.degraded_from:
        stored = set_degraded(ctx.conn, stored.id, plan.degraded_from)
    if not plan.push:
        return stored
    channel = Channel(route, ctx.readers(session, binding_for(session, ctx.bindings)))
    return push(ctx.conn, stored, session, channel, ctx.now(), interrupt=plan.interrupt)


def wait_for_confirmation(
    stored: Message, session: Session, seconds: float, ctx: Context
) -> Message:
    reader = ctx.readers(session, binding_for(session, ctx.bindings))
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
    if stored.state == "failed" and failed_start_detail(stored.detail):
        raise BellhopError(
            f"the start for {stored.id} failed: {stored.detail}", code=LAUNCH_FAILED, stored=stored
        )
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


def failed_start_detail(detail: str | None) -> bool:
    return (detail or "").startswith((START_FAILED, ABANDONED_DETAIL))


def message_line(message: Message) -> str:
    detail = f" ({escape_controls(message.detail)})" if message.detail else ""
    degraded = f" degraded from {message.degraded_from}" if message.degraded_from else ""
    return f"{message.id} {message.state}{degraded} -> {escape_controls(message.target)}{detail}"


def explicit_sender(args: argparse.Namespace) -> str | None:
    return check_controls(args.sender, "--from") if args.sender is not None else None


def normal_body(text: str) -> str:
    return text.replace("\r\n", "\n")


def read_body(args: argparse.Namespace) -> str:
    if args.text is not None:
        return check_body(normal_body(args.text))
    try:
        text = sys.stdin.read(BODY_LIMIT + 1)
    except UnicodeDecodeError as error:
        raise BellhopError("stdin is not valid UTF-8 text", code=USAGE) from error
    if len(text) > BODY_LIMIT:
        raise BellhopError(
            f"message body on stdin is over the limit of {BODY_LIMIT} characters", code=USAGE
        )
    return check_body(normal_body(text))


def latest(ctx: Context, stored: Message) -> Message:
    try:
        return get_message(ctx.conn, stored.id)
    except Exception:  # noqa: BLE001
        return stored


def finish(
    stored: Message, target: Target, delivery: Delivery, confirm: float | None, ctx: Context
) -> Message:
    try:
        stored = deliver(stored, target, delivery, ctx)
        ctx.kick()
        if confirm and stored.state == "sent" and delivery.session is not None:
            stored = wait_for_confirmation(stored, delivery.session, confirm, ctx)
    except BellhopError as error:
        error.stored = error.stored or latest(ctx, stored)
        raise
    except KeyboardInterrupt:
        raise BellhopError("interrupted", code=INTERRUPTED, stored=latest(ctx, stored)) from None
    except Exception as error:
        message = f"internal error: {type(error).__name__}: {error}"
        raise BellhopError(message, code=INTERNAL_ERROR, stored=latest(ctx, stored)) from error
    return stored


def asked_for(stored: Message, target_text: str, body: str) -> bool:
    named = target_text in (stored.target, stored.title) or (
        len(target_text) >= PREFIX_MINIMUM and stored.target.startswith(target_text)
    )
    return named and stored.body == body


def unrouted(urgency: Urgency) -> Delivery:
    return Delivery(None, None, urgency, None)


def send(args: argparse.Namespace, ctx: Context) -> int:
    body = read_body(args)
    explicit = explicit_sender(args)
    if args.key is not None:
        existing = find_by_key(ctx.conn, args.key)
        if existing is not None:
            if not asked_for(existing, args.target, body):
                ctx.warnings.append(key_reused(args.key, existing))
            return emit_message(existing, args, ctx)
    target = resolve(args.target, ctx.sessions.sessions, ctx.config, ctx.bindings)
    sender = clean_sender(explicit or default_sender(ctx))
    draft = Draft(body, sender, args.key, bool(args.wake))
    if args.wake and wakeable(target, args.urgency):
        unsent = build(target, unrouted(args.urgency), draft, new_id(), ctx.now())
        woken = wake(str(target.agent), unsent, f"send --wake from {sender}", ctx, retry=False)
        if woken.outcome != "send" and woken.message is not None:
            if args.confirm and woken.outcome in ("start", "queue"):
                ctx.warnings.append("--confirm does not wait for a message that starts its agent")
            ctx.kick()
            return emit_message(woken.message, args, ctx)
    sent = send_message(
        target, draft, args.urgency, ctx, or_hold=args.or_hold, confirm=args.confirm
    )
    if waits_for_agent(target, sent, ctx) and not args.wake:
        ctx.warnings.append(
            f"'{target.agent}' is not running; the message waits (use --wake to start it)"
        )
    elif now_waits(target, sent):
        reason = sent.detail or "behind an older now message to the same session"
        ctx.warnings.append(f"the now message waits: {reason}")
    return emit_message(sent, args, ctx)


def now_waits(target: Target, sent: Message) -> bool:
    return (
        target.session is not None
        and sent.urgency == "now"
        and sent.state == "pending"
        and sent.degraded_from is None
    )


def waits_for_agent(target: Target, sent: Message, ctx: Context) -> bool:
    return (
        target.kind == "agent"
        and target.session is None
        and target.agent in ctx.config.agents
        and not any(b.agent == target.agent and b.state == "starting" for b in ctx.bindings)
        and sent.urgency != "hold"
        and sent.state == "pending"
    )


def wakeable(target: Target, urgency: Urgency) -> bool:
    return target.kind == "agent" and target.session is None and urgency != "hold"


def send_message(
    target: Target,
    draft: Draft,
    urgency: Urgency,
    ctx: Context,
    *,
    or_hold: bool,
    confirm: float | None,
) -> Message:
    delivery = choose(target, urgency, ctx, or_hold=or_hold)
    message = build(target, delivery, draft, new_id(), ctx.now())
    stored = insert_message(ctx.conn, message)
    if stored.id != message.id:
        ctx.warnings += replay_warnings(message, stored)
        return stored
    return finish(stored, target, delivery, confirm, ctx)


def recheck(message: Message, ctx: Context) -> Message:
    if message.state != "sent" or message.transcript_offset is None:
        return message
    session = next((s for s in ctx.sessions.sessions if s.session_id == message.session_id), None)
    if session is None:
        return message
    reader = ctx.readers(session, binding_for(session, ctx.bindings))
    return confirm_if_seen(ctx.conn, message, session, reader, ctx.now())


def rechecked(stored: list[Message], ctx: Context) -> list[Message]:
    try:
        return [recheck(message, ctx) for message in stored]
    except BellhopError as error:
        ctx.warnings.append(f"cannot recheck transcripts: {error}")
        return get_messages(ctx.conn, [m.id for m in stored])


def status(args: argparse.Namespace, ctx: Context) -> int:
    found = rechecked(get_messages(ctx.conn, args.ids), ctx)
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
        rows = [message_object(m) | {"body": m.body} for m in taken]
        print_json(ok_payload(ctx.warnings, {"messages": rows}))
        return 0
    print_warnings(ctx.warnings)
    print_lines(inbox_lines(taken))
    return 0


def inbox_lines(messages: Sequence[Message]) -> list[str]:
    if not messages:
        return ["no messages waiting"]
    return [escape_text(render(m.id, m.sender, m.body, 0)) for m in messages]
