import argparse
from collections.abc import Mapping
from dataclasses import dataclass

from bellhop.cli.context import Context
from bellhop.cli.presentation import (
    escape_controls,
    print_json,
    print_lines,
    print_warnings,
    session_lines,
)
from bellhop.codes import BINDING_CONFLICT, USAGE
from bellhop.config import Config, valid_name
from bellhop.errors import BellhopError
from bellhop.identity import alive
from bellhop.identity import whoami as identify_caller
from bellhop.jsonout import agent_object, ok_payload, session_object, whoami_object
from bellhop.models import Binding, Harness, Session
from bellhop.store import pending_count, save_binding_unless
from bellhop.targets import agent_names, agent_of


def session_row(session: Session, ctx: Context) -> dict[str, object]:
    agent = agent_of(session, ctx.bindings)
    route = ctx.routes(session)
    reachable = route is not None and route.reachable(session)
    pending = pending_count(ctx.conn, "session", session.session_id) + (
        pending_count(ctx.conn, "agent", agent) if agent else 0
    )
    return session_object(
        session,
        agent=agent,
        route=route.name if route is not None and reachable else None,
        reachable=reachable,
        pending=pending,
    )


def sessions(args: argparse.Namespace, ctx: Context) -> int:
    listed = ctx.sessions.sessions
    if args.json:
        rows = [session_row(session, ctx) for session in listed]
        print_json(ok_payload(ctx.warnings, {"sessions": rows}))
        return 0
    print_warnings(ctx.warnings)
    print_lines(session_lines(listed))
    return 0


def whoami(args: argparse.Namespace, ctx: Context) -> int:
    session = identify_caller(ctx.env, ctx.sessions.sessions, ctx.table)
    agent = agent_of(session, ctx.bindings)
    if args.json:
        print_json(ok_payload(ctx.warnings, {"session": whoami_object(session, agent)}))
        return 0
    print_warnings(ctx.warnings)
    title = escape_controls(session.title) if session.title else "-"
    print_lines([f"{session.session_id} {session.harness} {title} {agent or '-'}"])
    return 0


@dataclass(frozen=True)
class AgentRow:
    name: str
    harness: Harness | None
    configured: bool
    binding: Binding | None


def agent_row(name: str, config: Config, current: Mapping[str, Binding]) -> AgentRow:
    configured = config.agents.get(name)
    binding = current.get(name)
    harness = configured.harness if configured else binding.harness if binding else None
    return AgentRow(name, harness, configured is not None, binding)


def agent_rows(ctx: Context) -> list[AgentRow]:
    current = {binding.agent: binding for binding in ctx.bindings}
    return [agent_row(name, ctx.config, current) for name in agent_names(ctx.config, ctx.bindings)]


def agent_line(row: AgentRow) -> str:
    state = row.binding.state if row.binding else "unbound"
    session = row.binding.session_id if row.binding else None
    configured = "configured" if row.configured else "bound only"
    return escape_controls(f"{row.name} {row.harness} {configured} {state} {session or '-'}")


def agents(args: argparse.Namespace, ctx: Context) -> int:
    rows = agent_rows(ctx)
    if args.json:
        objects = [
            agent_object(r.name, r.harness, configured=r.configured, binding=r.binding)
            for r in rows
        ]
        print_json(ok_payload(ctx.warnings, {"agents": objects}))
        return 0
    print_warnings(ctx.warnings)
    print_lines([agent_line(row) for row in rows] or ["no agents configured or bound"])
    return 0


def _holder_alive(binding: Binding, ctx: Context) -> bool:
    if binding.state == "failed":
        return False
    if binding.pid is not None:
        return ctx.table is None or alive(binding.pid, binding.process_started_at, ctx.table)
    return any(s.session_id == binding.session_id for s in ctx.sessions.sessions)


def _bound_elsewhere(binding: Binding | None, session: Session, ctx: Context) -> bool:
    if binding is None or not _holder_alive(binding, ctx):
        return False
    if binding.pid is not None:
        return not (binding.pid == session.pid and session.harness != "codex")
    return binding.session_id != session.session_id


def bind(args: argparse.Namespace, ctx: Context) -> int:
    name = args.agent
    if not valid_name(name):
        raise BellhopError(
            f"agent name '{name}' must use lowercase letters, digits, '_' and '-'", code=USAGE
        )
    session = identify_caller(ctx.env, ctx.sessions.sessions, ctx.table)
    configured = ctx.config.agents.get(name)
    if configured is not None and configured.harness != session.harness:
        raise BellhopError(
            f"'{name}' is configured as a {configured.harness} agent; this session is {session.harness}",
            code=USAGE,
        )
    codex = session.harness == "codex"
    binding = Binding(
        agent=name,
        harness=session.harness,
        state="live",
        session_id=session.session_id,
        pid=None if codex else session.pid,
        process_started_at=None if codex else session.process_started_at,
        app_server_socket=None,
        app_server_pid=session.pid if codex else None,
        tmux_target=session.tmux_pane,
        cause="bind",
        detail=None,
        updated_at=ctx.now(),
    )
    held = save_binding_unless(
        ctx.conn, binding, lambda existing: _bound_elsewhere(existing, session, ctx)
    )
    if held is not None:
        raise BellhopError(
            f"'{name}' is bound to live session {held.session_id}", code=BINDING_CONFLICT
        )
    ctx.forget_bindings()
    row = agent_object(name, session.harness, configured=configured is not None, binding=binding)
    if args.json:
        print_json(ok_payload(ctx.warnings, {"agent": row}))
        return 0
    print_warnings(ctx.warnings)
    print_lines([f"bound {name} to {session.session_id}"])
    return 0
