import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bellhop.models import PushOutcome, PushResult, Session

OUTCOMES: Mapping[str, PushOutcome] = {
    "accepted": "accepted",
    "busy": "busy",
    "rejected": "rejected",
    "unknown": "unknown",
}


@dataclass
class FakeRoute:
    name: str = "fake"
    can_interrupt: bool = True
    queues_until_idle: bool = True
    is_reachable: bool = True
    is_replaced: bool = False
    busy_answer: bool | None = None
    outcomes: list[str] = field(default_factory=list)
    log: Path | None = None
    echo: bool = False
    pushes: list[tuple[str, str, bool]] = field(default_factory=list)

    def reachable(self, session: Session) -> bool:
        return self.is_reachable

    def obstacle(self, session: Session) -> str:
        return "the fake route is set unreachable"

    def replaced(self, session: Session) -> bool:
        return self.is_replaced

    def busy(self, session: Session) -> bool | None:
        return self.busy_answer

    def _done(self, session: Session) -> int:
        if self.log is None or not self.log.exists():
            return len(self.pushes)
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        return sum(1 for row in rows if row["session_id"] == session.session_id)

    def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
        index = self._done(session)
        word = self.outcomes[index] if index < len(self.outcomes) else "accepted"
        self.pushes.append((session.session_id, text, interrupt))
        if self.log is not None:
            with self.log.open("a", encoding="utf-8") as handle:
                row = {"session_id": session.session_id, "text": text, "interrupt": interrupt}
                handle.write(json.dumps(row) + "\n")
        outcome = OUTCOMES[word]
        if outcome == "accepted" and self.echo and session.transcript_path is not None:
            with session.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        return PushResult(outcome, f"fake {word}")


def route_from(settings: Mapping[str, object], log: Path | None) -> FakeRoute | None:
    if settings.get("none"):
        return None
    outcomes = settings.get("outcomes", [])
    busy = settings.get("busy")
    return FakeRoute(
        name=str(settings.get("name", "fake")),
        can_interrupt=bool(settings.get("can_interrupt", True)),
        queues_until_idle=bool(settings.get("queues_until_idle", True)),
        is_reachable=bool(settings.get("reachable", True)),
        is_replaced=bool(settings.get("replaced", False)),
        busy_answer=busy if isinstance(busy, bool) else None,
        outcomes=[str(word) for word in outcomes] if isinstance(outcomes, list) else [],
        log=log,
        echo=bool(settings.get("echo", False)),
    )


def fake_finder(config: Path) -> Callable[[Session], FakeRoute | None]:
    def find(session: Session) -> FakeRoute | None:
        data = json.loads(config.read_text(encoding="utf-8"))
        log = Path(data["log"]) if data.get("log") else None
        sessions = data.get("sessions", {})
        settings = sessions.get(session.session_id, data.get("default", {}))
        return route_from(settings, log)

    return find
