import sys
import uuid
from dataclasses import dataclass, replace
from typing import Literal

from bellhop.cli.context import Context
from bellhop.codes import INVALID_CONFIG, LAUNCH_FAILED
from bellhop.errors import BellhopError
from bellhop.identity import ProcessTable, alive
from bellhop.launch import StartSetup, agent_lock, begin, cannot_launch, decide
from bellhop.models import Binding, Message
from bellhop.runtime import resolve_runtime_dir
from bellhop.store import insert_message
from bellhop.tmux import tmux_for

type Outcome = Literal["start", "queue", "send", "stored"]


@dataclass(frozen=True)
class Woken:
    outcome: Outcome
    message: Message | None
    binding: Binding | None


def server_alive(binding: Binding | None, table: ProcessTable | None) -> bool:
    if binding is None or binding.app_server_pid is None or table is None:
        return False
    return alive(binding.app_server_pid, binding.process_started_at, table)


def current_binding(ctx: Context, agent: str) -> Binding | None:
    ctx.forget_bindings()
    return next((binding for binding in ctx.bindings if binding.agent == agent), None)


def store(ctx: Context, message: Message | None) -> Message | None:
    return insert_message(ctx.conn, message) if message is not None else None


def setup(ctx: Context) -> StartSetup:
    return StartSetup(
        env=ctx.env,
        tmux=tmux_for(ctx.env, ctx.config.tmux_socket),
        runtime_dir=lambda: resolve_runtime_dir(ctx.env, ctx.config),
        python=sys.executable,
        new_id=lambda: str(uuid.uuid4()),
    )


def refuse(agent: str, binding: Binding, message: Message | None, ctx: Context) -> BellhopError:
    failed = replace(message, state="failed", detail=binding.detail) if message else None
    return BellhopError(
        f"cannot wake '{agent}': {binding.detail}; run `bellhop launch {agent}` to retry",
        code=LAUNCH_FAILED,
        stored=store(ctx, failed),
    )


def wake(agent: str, message: Message | None, cause: str, ctx: Context, *, retry: bool) -> Woken:
    configured = ctx.config.agents.get(agent)
    with agent_lock(ctx.env, agent):
        binding = current_binding(ctx, agent)
        server = server_alive(binding, ctx.table)
        decision = decide(binding, retry=retry, server_alive=server, now=ctx.now())
        if decision == "fail" and binding is not None:
            raise refuse(agent, binding, message, ctx)
        if decision == "send":
            if binding is not None and binding.state == "failed":
                ctx.warnings.append(
                    f"'{agent}' has a running codex app server whose thread hooklinesinker no "
                    "longer lists; messages wait until it is listed again"
                )
            return Woken("send", None, binding)
        if decision == "queue":
            return Woken("queue", store(ctx, message), binding)
        reason = cannot_launch(configured, ctx.env)
        if (reason is not None or configured is None) and retry:
            raise BellhopError(
                f"cannot launch '{agent}': {reason} ({ctx.config.path})", code=INVALID_CONFIG
            )
        if reason is not None or configured is None:
            ctx.warnings.append(f"cannot launch '{agent}': {reason}; the message waits")
            return Woken("stored", store(ctx, message), binding)
        stored, started = begin(
            ctx.conn, configured, message, cause=cause, setup=setup(ctx), now=ctx.now()
        )
    ctx.forget_bindings()
    ctx.kick(again=True)
    return Woken("start" if started is not None else "stored", stored, started or binding)
