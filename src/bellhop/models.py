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
