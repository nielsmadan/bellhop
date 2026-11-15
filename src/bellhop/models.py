from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

type Harness = Literal["claude", "codex", "pi"]
type Phase = Literal["idle", "working", "permission", "compacting", "unknown"]

HARNESSES: Mapping[str, Harness] = {"claude": "claude", "codex": "codex", "pi": "pi"}
PHASES: Mapping[str, Phase] = {
    "idle": "idle",
    "working": "working",
    "permission": "permission",
    "compacting": "compacting",
    "unknown": "unknown",
}


@dataclass(frozen=True)
class Session:
    session_id: str
    binding_id: str
    harness: Harness
    title: str | None
    cwd: Path | None
    transcript_path: Path | None
    phase: Phase
    observed_at: datetime
    pid: int | None
    process_started_at: datetime | None
    tmux_pane: str | None
    tmux_socket: str | None


type Urgency = Literal["now", "idle", "hold"]
type MessageState = Literal["pending", "sending", "sent", "confirmed", "read", "failed"]
type PushOutcome = Literal["accepted", "busy", "rejected", "unknown"]
type TargetKind = Literal["session", "agent"]
type BindingState = Literal["starting", "live", "failed"]

URGENCIES: Mapping[str, Urgency] = {"now": "now", "idle": "idle", "hold": "hold"}
FAKE_ROUTES_ENV = "BELLHOP_FAKE_ROUTES"


@dataclass(frozen=True)
class PushResult:
    outcome: PushOutcome
    detail: str


@dataclass(frozen=True)
class Message:
    seq: int
    id: str
    key: str | None
    target_kind: TargetKind
    target: str
    harness: Harness | None
    title: str | None
    sender: str
    body: str
    urgency: Urgency
    degraded_from: Urgency | None
    wake: bool
    state: MessageState
    route: str | None
    detail: str | None
    session_id: str | None
    transcript_offset: int | None
    transcript: Path | None
    created_at: datetime
    sent_at: datetime | None
    confirmed_at: datetime | None


@dataclass(frozen=True)
class Binding:
    agent: str
    harness: Harness
    state: BindingState
    session_id: str | None
    pid: int | None
    process_started_at: datetime | None
    app_server_socket: str | None
    app_server_pid: int | None
    tmux_target: str | None
    cause: str | None
    detail: str | None
    updated_at: datetime
