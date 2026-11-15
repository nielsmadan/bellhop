from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import get_close_matches

from bellhop.codes import TARGET_AMBIGUOUS, TARGET_NOT_FOUND
from bellhop.config import Config
from bellhop.errors import BellhopError
from bellhop.identity import ProcessTable, alive, describe, same_start
from bellhop.models import Binding, Harness, Session, TargetKind

PREFIX_MINIMUM = 4


@dataclass(frozen=True)
class Target:
    kind: TargetKind
    name: str
    agent: str | None
    session: Session | None
    harness: Harness | None

    @property
    def title(self) -> str | None:
        return self.session.title if self.session else None


def live_session(binding: Binding | None, sessions: Iterable[Session]) -> Session | None:
    if binding is None or binding.state != "live" or binding.session_id is None:
        return None
    return next((s for s in sessions if s.session_id == binding.session_id), None)


def binding_for(session: Session, bindings: Sequence[Binding]) -> Binding | None:
    return next(
        (b for b in bindings if b.state == "live" and b.session_id == session.session_id), None
    )


def agent_names(config: Config, bindings: Sequence[Binding]) -> list[str]:
    return sorted({*config.agents, *(binding.agent for binding in bindings)})


def agent_of(session: Session, bindings: Sequence[Binding]) -> str | None:
    binding = binding_for(session, bindings)
    return binding.agent if binding is not None else None


def _agent_target(
    name: str, config: Config, bindings: Sequence[Binding], sessions: Sequence[Session]
) -> Target:
    binding = next((b for b in bindings if b.agent == name), None)
    session = live_session(binding, sessions)
    configured = config.agents.get(name)
    harness = (
        session.harness
        if session
        else configured.harness
        if configured
        else binding.harness
        if binding
        else None
    )
    return Target("agent", name, name, session, harness)


def _session_target(session: Session, bindings: Sequence[Binding]) -> Target:
    return Target(
        "session", session.session_id, agent_of(session, bindings), session, session.harness
    )


def _ambiguous(text: str, how: str, matches: Sequence[Session]) -> BellhopError:
    return BellhopError(
        f"'{text}' {how} {len(matches)} live sessions; use a longer id",
        code=TARGET_AMBIGUOUS,
        candidates=[describe(session) for session in matches],
    )


def _not_found(text: str, names: Sequence[str], sessions: Sequence[Session]) -> BellhopError:
    known = [*names, *(s.title for s in sessions if s.title), *(s.session_id for s in sessions)]
    return BellhopError(
        f"no agent or live session matches '{text}'",
        code=TARGET_NOT_FOUND,
        candidates=get_close_matches(text, sorted(set(known)), n=5, cutoff=0.6),
    )


def resolve(
    text: str, sessions: Sequence[Session], config: Config, bindings: Sequence[Binding]
) -> Target:
    names = agent_names(config, bindings)
    if text in names:
        return _agent_target(text, config, bindings, sessions)
    exact = [s for s in sessions if s.session_id == text]
    if exact:
        return _session_target(exact[0], bindings)
    if len(text) >= PREFIX_MINIMUM:
        prefixed = [s for s in sessions if s.session_id.startswith(text)]
        if len(prefixed) == 1:
            return _session_target(prefixed[0], bindings)
        if prefixed:
            raise _ambiguous(text, "is a prefix of", prefixed)
    titled = [s for s in sessions if s.title == text]
    if len(titled) == 1:
        return _session_target(titled[0], bindings)
    if titled:
        raise _ambiguous(text, "is the title of", titled)
    raise _not_found(text, names, sessions)


def _follow_app_server(
    binding: Binding, sessions: Sequence[Session], table: ProcessTable, now: datetime
) -> Binding:
    if binding.state == "starting":
        return binding
    listed = next((s for s in sessions if s.session_id == binding.session_id), None)
    if listed is None:
        detail = "thread no longer listed"
    elif listed.pid is not None and not alive(listed.pid, listed.process_started_at, table):
        detail = "app server exited"
    else:
        current = replace(
            binding,
            state="live",
            detail=None,
            app_server_pid=listed.pid,
            process_started_at=listed.process_started_at,
        )
        return binding if current == binding else replace(current, updated_at=now)
    if binding.state == "failed":
        return binding
    return replace(binding, state="failed", detail=detail, updated_at=now)


def _follow(
    binding: Binding, sessions: Sequence[Session], table: ProcessTable, now: datetime
) -> Binding:
    if binding.harness == "codex":
        return _follow_app_server(binding, sessions, table, now)
    if binding.state == "failed":
        return binding
    if binding.pid is None:
        return binding
    if not alive(binding.pid, binding.process_started_at, table):
        return replace(binding, state="failed", detail="process exited", updated_at=now)
    owned = [
        s
        for s in sessions
        if s.pid == binding.pid and same_start(s.process_started_at, binding.process_started_at)
    ]
    current = max(owned, key=lambda s: s.observed_at, default=None)
    if current is None or current.session_id == binding.session_id:
        return binding
    return replace(binding, session_id=current.session_id, updated_at=now)


def refresh_bindings(
    bindings: Sequence[Binding],
    sessions: Sequence[Session],
    table: ProcessTable | None,
    now: datetime,
) -> list[Binding]:
    if table is None:
        return list(bindings)
    return [_follow(binding, sessions, table, now) for binding in bindings]
