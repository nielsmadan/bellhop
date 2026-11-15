from collections.abc import Callable
from pathlib import Path

import pytest

from bellhop.codes import LAUNCH_FAILED
from bellhop.errors import BellhopError
from bellhop.tmux import Place, Tmux, TmuxTimeout, target_place, tmux_for, tmux_target
from conftest import FakeTmux

type Fake = Callable[..., Tmux]


@pytest.fixture
def fake(fake_tmux: FakeTmux) -> Fake:
    return lambda socket="bellhop-test": Tmux(socket, fake_tmux.executable)


def test_a_new_session_is_detached_named_sized_in_its_cwd_and_runs_the_argv_as_given(
    fake: Fake, fake_tmux: FakeTmux, tmp_path: Path
) -> None:
    pane = fake().new_session("enki", tmp_path, ["/usr/bin/env", "-i", "/bin/a b", "x; y"])

    assert pane == "%0"
    assert fake_tmux.calls()[-1] == [
        "-L",
        "bellhop-test",
        "new-session",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-s",
        "enki",
        "-x",
        "200",
        "-y",
        "50",
        "-c",
        str(tmp_path),
        "--",
        "/usr/bin/env",
        "-i",
        "/bin/a b",
        "x; y",
    ]


def test_arguments_ending_in_a_semicolon_reach_the_program_whole(
    fake: Fake, fake_tmux: FakeTmux, tmp_path: Path
) -> None:
    argv = ["/usr/bin/env", "-i", "A=x;", "/bin/agent", "fix it;", "keep \\;", "last"]

    fake().new_session("enki", tmp_path, argv)

    assert fake_tmux.argv("enki") == argv


def test_the_client_runs_with_the_allowlist_only(
    fake: Fake, fake_tmux: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("CLAUDE_CODE_MESSAGING_TOKEN", "CODEX_THREAD_ID", "GH_TOKEN", "TMUX", "TMUX_PANE"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.setenv("LC_CTYPE", "UTF-8")

    fake().new_session("enki", tmp_path, ["/bin/true", "x"])

    assert {
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CODEX_THREAD_ID",
        "GH_TOKEN",
        "TMUX",
        "TMUX_PANE",
    }.isdisjoint(fake_tmux.client_env())
    assert {"PATH", "HOME", "TMUX_TMPDIR", "LC_CTYPE"} <= set(fake_tmux.client_env())


def test_a_free_name_skips_sessions_that_already_exist(fake: Fake, fake_tmux: FakeTmux) -> None:
    fake_tmux.occupy("enki")
    fake_tmux.occupy("enki-2")

    assert fake().free_name("enki") == "enki-3"


def test_no_free_name_after_nine_tries_is_a_launch_failure(fake: Fake, fake_tmux: FakeTmux) -> None:
    for name in ["enki", *(f"enki-{n}" for n in range(2, 10))]:
        fake_tmux.occupy(name)

    with pytest.raises(BellhopError) as raised:
        fake().free_name("enki")

    assert (raised.value.code, str(raised.value)) == (
        LAUNCH_FAILED,
        "tmux sessions enki to enki-9 all exist; remove one to launch 'enki' again",
    )


def test_a_closed_pane_captures_as_none(fake: Fake, fake_tmux: FakeTmux) -> None:
    fake_tmux.show("hello\n")
    shown = fake().capture("%0")
    fake_tmux.close_pane()

    assert (shown, fake().capture("%0")) == ("hello\n", None)


def test_a_session_s_pane_is_found_by_name(fake: Fake, fake_tmux: FakeTmux) -> None:
    fake_tmux.occupy("enki")

    assert (fake().pane_of("enki"), fake().pane_of("ghost")) == ("%9", None)


def test_a_refused_command_is_a_launch_failure_with_tmux_s_reason(
    fake: Fake, fake_tmux: FakeTmux
) -> None:
    fake_tmux.refuse()

    with pytest.raises(BellhopError) as raised:
        fake().new_session("enki", Path("/"), ["/bin/true", "x"])

    assert str(raised.value) == "tmux new-session failed: fake: server refused"


def test_a_tmux_that_does_not_answer_is_a_timeout(
    fake: Fake, fake_tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bellhop.tmux.TMUX_SECONDS", 0.5)
    fake_tmux.hang()

    with pytest.raises(TmuxTimeout):
        fake().new_session("enki", Path("/"), ["/bin/true", "x"])

    assert fake_tmux.sessions() == ["enki"]


def test_a_missing_tmux_is_a_launch_failure(tmp_path: Path) -> None:
    with pytest.raises(BellhopError) as raised:
        Tmux("", str(tmp_path / "no-tmux")).new_session("enki", tmp_path, ["/bin/true"])

    assert raised.value.code == LAUNCH_FAILED


@pytest.mark.parametrize(
    ("socket", "hint"),
    [
        ("", "tmux attach -t =enki"),
        ("fleet", "tmux -L fleet attach -t =enki"),
        ("/tmp/my tmux", "tmux -S '/tmp/my tmux' attach -t =enki"),
    ],
)
def test_the_attach_hint_names_the_configured_server(socket: str, hint: str) -> None:
    assert Tmux(socket).attach_hint("enki") == hint


@pytest.mark.parametrize("socket", ["", "fleet", "/tmp/my tmux"])
def test_a_start_s_place_round_trips_through_its_tmux_target(socket: str) -> None:
    place = Place(socket, "enki-2", "%7")

    assert target_place(tmux_target(place)) == place


@pytest.mark.parametrize("target", [None, "", "%3", "-X x enki:%0", "a b enki:%0"])
def test_a_tmux_target_that_is_not_a_start_has_no_place(target: str | None) -> None:
    assert target_place(target) is None


def test_tmux_is_found_on_the_callers_path(fake_tmux: FakeTmux) -> None:
    found = tmux_for({"PATH": str(fake_tmux.directory / "bin")}, "fleet")

    assert found == Tmux("fleet", fake_tmux.executable)


def test_a_cwd_ending_in_a_semicolon_is_escaped_for_tmux(
    fake: Fake, fake_tmux: FakeTmux, tmp_path: Path
) -> None:
    fake().new_session("enki", Path("/work/odd;"), ["/bin/true", "x"])

    assert fake_tmux.calls()[-1][fake_tmux.calls()[-1].index("-c") + 1] == "/work/odd\\;"
