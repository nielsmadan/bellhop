import argparse
import time
from collections.abc import Callable
from difflib import get_close_matches

from bellhop.cli.context import Context
from bellhop.cli.messages import (
    Draft,
    build,
    default_sender,
    latest,
    message_line,
    send_message,
    unrouted,
)
from bellhop.cli.presentation import escape_controls, print_json, print_lines, print_warnings
from bellhop.cli.waking import wake
from bellhop.codes import INVALID_CONFIG, LAUNCH_FAILED, TARGET_NOT_FOUND
from bellhop.delivery import new_id
from bellhop.envelope import check_body, clean_sender
from bellhop.errors import BellhopError
from bellhop.jsonout import agent_object, message_object, ok_payload
from bellhop.launch import STARTUP, failed_start
from bellhop.models import Binding, Harness, Message
from bellhop.store import find_by_key, get_binding
from bellhop.targets import resolve

LAUNCH_WAIT_SECONDS = STARTUP.total_seconds() + 5
LAUNCH_POLL_SECONDS = 0.25


def wait_for_start(
    ctx: Context,
    agent: str,
    *,
    seconds: float = LAUNCH_WAIT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Binding | None:
    deadline = clock() + seconds
    binding = get_binding(ctx.conn, agent)
    while binding is not None and binding.state == "starting" and clock() < deadline:
        sleep(LAUNCH_POLL_SECONDS)
        binding = get_binding(ctx.conn, agent)
    if binding is not None and binding.state == "starting":
        ctx.warnings.append(f"'{agent}' is still starting after {seconds:g}s")
    return binding


def launch_lines(agent: str, binding: Binding | None, stored: Message | None) -> list[str]:
    tail = [message_line(stored)] if stored is not None else []
    if binding is None:
        return [f"{agent} not started", *tail]
    session = f" {binding.session_id}" if binding.session_id else ""
    tmux = f" (tmux {escape_controls(binding.tmux_target)})" if binding.tmux_target else ""
    return [f"{agent} {binding.state}{session}{tmux}", *tail]


def launch(args: argparse.Namespace, ctx: Context) -> int:
    name = args.agent
    agent = ctx.config.agents.get(name)
    if agent is None:
        raise BellhopError(
            f"no agent '{name}' in {ctx.config.path}",
            code=TARGET_NOT_FOUND,
            candidates=get_close_matches(name, sorted(ctx.config.agents), n=5, cutoff=0.6),
        )
    if not agent.command:
        raise BellhopError(
            f"cannot launch '{name}': it has no command to start it with ({ctx.config.path})",
            code=INVALID_CONFIG,
        )
    if args.key is not None:
        existing = find_by_key(ctx.conn, args.key)
        if existing is not None:
            binding = next((b for b in ctx.bindings if b.agent == name), None)
            return emit_launch(name, agent.harness, binding, existing, args, ctx)
    target = resolve(name, ctx.sessions.sessions, ctx.config, ctx.bindings)
    sender = clean_sender(args.sender or default_sender(ctx))
    draft = None
    unsent = None
    if args.prompt is not None:
        draft = Draft(check_body(args.prompt), sender, args.key, True)
        unsent = build(target, unrouted("idle"), draft, new_id(), ctx.now())
    woken = wake(name, unsent, args.cause or "launch", ctx, retry=True)
    stored, binding = woken.message, woken.binding
    if woken.outcome == "send" and draft is not None:
        stored = send_message(target, draft, "idle", ctx, or_hold=False, confirm=None)
    if woken.outcome in ("start", "queue") and not args.no_wait:
        binding = wait_for_start(ctx, name)
        stored = latest(ctx, stored) if stored is not None else None
    if binding is not None and failed_start(binding):
        raise BellhopError(
            f"cannot launch '{name}': {binding.detail}", code=LAUNCH_FAILED, stored=stored
        )
    return emit_launch(name, agent.harness, binding, stored, args, ctx)


def emit_launch(
    name: str,
    harness: Harness,
    binding: Binding | None,
    stored: Message | None,
    args: argparse.Namespace,
    ctx: Context,
) -> int:
    if args.json:
        row = agent_object(name, harness, configured=True, binding=binding)
        message = message_object(stored) if stored is not None else None
        print_json(ok_payload(ctx.warnings, {"agent": row, "message": message}))
        return 0
    print_warnings(ctx.warnings)
    print_lines(launch_lines(name, binding, stored))
    return 0
