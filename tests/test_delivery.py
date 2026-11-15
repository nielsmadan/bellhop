import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from bellhop.delivery import (
    Channel,
    Plan,
    confirm_if_seen,
    drainer_may_push,
    new_id,
    plan_send,
    push,
)
from bellhop.models import PushResult, Session
from bellhop.routes.fake import FakeRoute
from bellhop.store import connect, get_message, insert_message, sweep_sending
from bellhop.transcripts import PlainTranscripts
from conftest import NOW, make_message, make_session


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "bellhop.sqlite")
    yield connection
    connection.close()


@pytest.mark.parametrize(
    ("urgency", "route", "phase", "older", "plan"),
    [
        ("hold", FakeRoute(), "idle", False, Plan(False, False, None)),
        ("now", FakeRoute(), "working", False, Plan(True, True, None)),
        ("now", FakeRoute(can_interrupt=False), "working", False, Plan(True, False, "now")),
        ("now", FakeRoute(), "idle", True, Plan(False, True, None)),
        ("idle", FakeRoute(queues_until_idle=True), "working", False, Plan(True, False, None)),
        (
            "idle",
            FakeRoute(queues_until_idle=False, busy_answer=False),
            "working",
            False,
            Plan(True, False, None),
        ),
        (
            "idle",
            FakeRoute(queues_until_idle=False, busy_answer=True),
            "idle",
            False,
            Plan(False, False, None),
        ),
        ("idle", FakeRoute(queues_until_idle=False), "idle", False, Plan(True, False, None)),
        ("idle", FakeRoute(queues_until_idle=False), "working", False, Plan(False, False, None)),
        ("idle", FakeRoute(), "idle", True, Plan(False, False, None)),
    ],
)
def test_plan_send_follows_the_delivery_rules(
    urgency: str, route: FakeRoute, phase: str, older: bool, plan: Plan
) -> None:
    session = make_session("s1", phase=phase)

    assert plan_send(urgency, route, session, older_pending=older) == plan  # type: ignore[arg-type]


def test_the_drainer_holds_for_busy_phases_and_lets_a_now_reach_an_interrupting_route() -> None:
    queueing = FakeRoute(queues_until_idle=True)

    busy = FakeRoute(queues_until_idle=False, busy_answer=True)

    assert drainer_may_push(queueing, make_session("s", phase="working"), "idle") is True
    for phase in ("permission", "compacting", "unknown"):
        assert drainer_may_push(queueing, make_session("s", phase=phase), "now") is False
    assert drainer_may_push(busy, make_session("s"), "idle") is False
    assert drainer_may_push(busy, make_session("s"), "now") is True
    assert drainer_may_push(replace(busy, can_interrupt=False), make_session("s"), "now") is False


def test_ids_are_bh_and_twelve_hex() -> None:
    value = new_id()

    assert value.startswith("bh_")
    assert len(value) == 15
    int(value[3:], 16)


@pytest.mark.parametrize(
    ("outcome", "state"),
    [("accepted", "sent"), ("busy", "pending"), ("rejected", "failed"), ("unknown", "sent")],
)
def test_each_push_outcome_maps_to_its_state(
    conn: sqlite3.Connection, outcome: str, state: str
) -> None:
    stored = insert_message(conn, make_message(1))
    route = FakeRoute(outcomes=[outcome])

    result = push(conn, stored, make_session("s1"), Channel(route, None), NOW, interrupt=False)

    assert (result.state, result.detail, result.route) == (state, f"fake {outcome}", "fake")
    assert route.pushes == [("s1", "[bellhop bh_000000000001 from enki] body 1", False)]


def test_the_envelope_counts_holds_waiting_for_the_target(conn: sqlite3.Connection) -> None:
    insert_message(conn, make_message(1, urgency="hold"))
    stored = insert_message(conn, make_message(2))
    route = FakeRoute()

    push(conn, stored, make_session("s1"), Channel(route, None), NOW, interrupt=False)

    assert route.pushes[0][1].endswith("\n(bellhop: 1 more waiting; run `bellhop inbox`)")


def test_a_push_that_raises_is_an_unknown_outcome(conn: sqlite3.Connection) -> None:
    class Exploding(FakeRoute):
        def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
            raise ConnectionResetError("gone")

    stored = insert_message(conn, make_message(1))

    result = push(
        conn, stored, make_session("s1"), Channel(Exploding(), None), NOW, interrupt=False
    )

    assert (result.state, result.detail) == ("sent", "push raised ConnectionResetError: gone")


def test_a_crash_between_claim_and_push_is_never_pushed_again(conn: sqlite3.Connection) -> None:
    stored = insert_message(conn, make_message(1))

    class Crashing(FakeRoute):
        def push(self, session: Session, text: str, *, interrupt: bool) -> PushResult:
            raise SystemExit("killed mid-push")

    with pytest.raises(SystemExit):
        push(conn, stored, make_session("s1"), Channel(Crashing(), None), NOW, interrupt=False)
    assert get_message(conn, stored.id).state == "sending"
    sweep_sending(conn, NOW + timedelta(seconds=31))
    route = FakeRoute()

    again = push(
        conn, get_message(conn, stored.id), make_session("s1"), Channel(route), NOW, interrupt=False
    )

    assert (again.state, again.detail) == ("sent", "outcome unknown")
    assert route.pushes == []


def test_confirmation_needs_the_prefix_after_the_offset(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    transcript = tmp_path / "t.txt"
    transcript.write_text("[bellhop bh_000000000001 from enki] an old copy\n")
    session = make_session("s1", transcript_path=transcript)
    reader = PlainTranscripts()
    stored = insert_message(conn, make_message(1))

    sent = push(conn, stored, session, Channel(FakeRoute(), reader), NOW, interrupt=False)
    assert sent.transcript_offset == transcript.stat().st_size
    assert confirm_if_seen(conn, sent, session, reader, NOW).state == "sent"

    echoed = insert_message(conn, make_message(2))
    delivered = push(
        conn, echoed, session, Channel(FakeRoute(echo=True), reader), NOW, interrupt=False
    )
    confirmed = confirm_if_seen(conn, delivered, session, reader, NOW)

    assert (confirmed.state, confirmed.confirmed_at) == ("confirmed", NOW)
    assert confirm_if_seen(conn, get_message(conn, sent.id), session, None, NOW).state == "sent"


def test_confirmation_reads_the_file_recorded_at_push_time(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    recorded = tmp_path / "recorded.txt"
    recorded.write_text("")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("")
    session = make_session("s1", transcript_path=recorded)
    stored = insert_message(conn, make_message(1))

    sent = push(
        conn,
        stored,
        session,
        Channel(FakeRoute(echo=True), PlainTranscripts()),
        NOW,
        interrupt=False,
    )
    moved = make_session("s1", transcript_path=elsewhere)

    assert sent.transcript == recorded
    assert confirm_if_seen(conn, sent, moved, PlainTranscripts(), NOW).state == "confirmed"


class RecordingReader(PlainTranscripts):
    def __init__(self) -> None:
        self.prompted: list[bool] = []

    def contains(
        self,
        session: Session,
        prefix: str,
        *,
        since: int,
        path: Path | None = None,
        prompted: bool = False,
    ) -> bool:
        self.prompted.append(prompted)
        return False


@pytest.mark.parametrize(("route", "prompted"), [("launch", True), ("fake", False)])
def test_only_a_start_prompt_is_looked_for_as_a_prompt(
    conn: sqlite3.Connection, route: str, prompted: bool
) -> None:
    message = make_message(1, state="sent", route=route, transcript_offset=0)
    reader = RecordingReader()

    confirm_if_seen(conn, message, make_session("s1"), reader, NOW)

    assert reader.prompted == [prompted]
