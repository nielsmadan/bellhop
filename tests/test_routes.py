import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from bellhop.models import Binding
from bellhop.routes import RouteSetup, route_finder
from bellhop.routes.codex import CodexRoute
from bellhop.routes.endpoint import EndpointRoute
from bellhop.routes.fake import FakeRoute
from bellhop.transcripts import PlainTranscripts, ReaderSetup, reader_finder
from conftest import make_session

BINDING = Binding(
    "nanna", "codex", "live", "x1", None, None, None, 4242, None, "bind", None, datetime.now(UTC)
)


def unused() -> Path:
    raise AssertionError("the runtime directory is not needed here")


def test_the_real_finder_routes_every_harness(tmp_path: Path, short_tmp: Path) -> None:
    find = route_finder(
        RouteSetup(
            runtime_dir=lambda: short_tmp, codex_home=lambda session, binding: tmp_path / ".codex"
        )
    )
    bound = replace(BINDING, app_server_socket="/tmp/per-session.sock")
    pi = find(make_session("p1", harness="pi", pid=11), None)
    claude = find(make_session("c1", harness="claude", pid=22), None)

    assert isinstance(pi, EndpointRoute)
    assert isinstance(claude, EndpointRoute)
    assert (pi.name, pi.socket(make_session("p1", harness="pi", pid=11))) == (
        "pi-extension",
        short_tmp / "pi-11.sock",
    )
    assert (claude.name, claude.socket(make_session("c1", harness="claude", pid=22))) == (
        "claude-relay",
        short_tmp / "claude-22.sock",
    )
    assert find(make_session("x1", harness="codex"), None) == CodexRoute(
        tmp_path / ".codex" / "app-server-control" / "app-server-control.sock"
    )
    assert find(make_session("x1", harness="codex"), bound) == CodexRoute(
        Path("/tmp/per-session.sock")
    )


def test_the_fake_route_takes_outcomes_in_order_and_logs_pushes(tmp_path: Path) -> None:
    config = tmp_path / "routes.json"
    log = tmp_path / "pushes.jsonl"
    config.write_text(json.dumps({"log": str(log), "default": {"outcomes": ["busy", "accepted"]}}))
    find = route_finder(RouteSetup(runtime_dir=unused, fake_routes=config))
    session = make_session("s1")

    first = find(session, None)
    assert first is not None
    assert first.push(session, "one", interrupt=False).outcome == "busy"
    second = find(session, None)
    assert second is not None
    assert second.push(session, "two", interrupt=True).outcome == "accepted"
    assert [json.loads(line)["text"] for line in log.read_text().splitlines()] == ["one", "two"]


def test_the_fake_route_can_be_absent_per_session(tmp_path: Path) -> None:
    config = tmp_path / "routes.json"
    config.write_text(json.dumps({"sessions": {"s1": {"none": True}}}))

    setup = RouteSetup(runtime_dir=unused, fake_routes=config)
    assert route_finder(setup)(make_session("s1"), None) is None


def test_an_echoing_fake_route_writes_the_transcript_the_plain_reader_reads(tmp_path: Path) -> None:
    transcript = tmp_path / "t.txt"
    transcript.write_text("before [bellhop bh_1 from x]\n")
    session = make_session("s1", transcript_path=transcript)
    reader = PlainTranscripts()
    offset = reader.size(session)
    assert offset is not None

    FakeRoute(echo=True).push(session, "[bellhop bh_1 from enki] hi", interrupt=False)

    assert reader.contains(session, "[bellhop bh_1 from", since=offset) is True
    assert reader.contains(session, "[bellhop bh_2 from", since=0) is False
    assert isinstance(
        reader_finder(ReaderSetup(tmp_path, fake=True))(session, None), PlainTranscripts
    )
