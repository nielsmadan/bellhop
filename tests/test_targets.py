from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from bellhop.config import AgentConfig, Config
from bellhop.errors import BellhopError
from bellhop.identity import Proc
from bellhop.models import Binding
from bellhop.targets import Target, agent_of, refresh_bindings, resolve
from conftest import NOW, STARTED, make_session

CONFIG = Config(
    path=Path("/c"),
    agents={
        "enki": AgentConfig(name="enki", harness="claude"),
        "nanna": AgentConfig("nanna", "codex"),
    },
)
SESSIONS = [
    make_session("2e2042a9-937c-4e03", title="callboard"),
    make_session("2e2099ff-0000-1111", title="dup", harness="codex"),
    make_session("7f00aa11-2222-3333", title="dup", harness="pi"),
]


def binding(agent: str, **changes: object) -> Binding:
    base = Binding(
        agent=agent,
        harness="claude",
        state="live",
        session_id="2e2042a9-937c-4e03",
        pid=4242,
        process_started_at=STARTED,
        app_server_socket=None,
        app_server_pid=None,
        tmux_target=None,
        cause="bind",
        detail=None,
        updated_at=NOW,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_a_configured_agent_without_a_binding_has_no_session() -> None:
    assert resolve("nanna", SESSIONS, CONFIG, []) == Target(
        "agent", "nanna", "nanna", None, "codex"
    )


def test_a_bound_agent_resolves_to_its_live_session() -> None:
    target = resolve("enki", SESSIONS, CONFIG, [binding("enki")])

    assert (target.kind, target.agent, target.session) == ("agent", "enki", SESSIONS[0])
    assert target.title == "callboard"


def test_a_bound_but_unconfigured_name_is_an_agent_too() -> None:
    target = resolve("scribe", SESSIONS, CONFIG, [binding("scribe", harness="pi", state="failed")])

    assert (target.kind, target.session, target.harness) == ("agent", None, "pi")


def test_an_exact_session_id_resolves_and_names_its_bound_agent() -> None:
    target = resolve("2e2042a9-937c-4e03", SESSIONS, CONFIG, [binding("enki")])

    assert (target.kind, target.name, target.agent) == ("session", "2e2042a9-937c-4e03", "enki")


def test_a_unique_prefix_of_four_or_more_characters_resolves() -> None:
    assert resolve("7f00", SESSIONS, CONFIG, []).session == SESSIONS[2]
    assert resolve("2e2042", SESSIONS, CONFIG, []).session == SESSIONS[0]


def test_a_three_character_prefix_is_not_a_prefix() -> None:
    with pytest.raises(BellhopError) as caught:
        resolve("7f0", SESSIONS, CONFIG, [])

    assert (caught.value.code, caught.value.exit_code) == ("target_not_found", 3)


def test_an_ambiguous_prefix_lists_the_candidates() -> None:
    with pytest.raises(BellhopError) as caught:
        resolve("2e20", SESSIONS, CONFIG, [])

    assert str(caught.value) == "'2e20' is a prefix of 2 live sessions; use a longer id"
    assert (caught.value.code, caught.value.exit_code) == ("target_ambiguous", 3)
    assert caught.value.candidates == [
        "2e2042a9-937c-4e03 (claude, callboard)",
        "2e2099ff-0000-1111 (codex, dup)",
    ]


def test_a_unique_title_resolves_and_a_shared_title_is_ambiguous() -> None:
    assert resolve("callboard", SESSIONS, CONFIG, []).session == SESSIONS[0]
    with pytest.raises(BellhopError) as caught:
        resolve("dup", SESSIONS, CONFIG, [])

    assert str(caught.value) == "'dup' is the title of 2 live sessions; use a longer id"
    assert caught.value.candidates == [
        "2e2099ff-0000-1111 (codex, dup)",
        "7f00aa11-2222-3333 (pi, dup)",
    ]


def test_no_match_suggests_close_names() -> None:
    with pytest.raises(BellhopError) as caught:
        resolve("enky", SESSIONS, CONFIG, [])

    assert str(caught.value) == "no agent or live session matches 'enky'"
    assert caught.value.candidates == ["enki"]


def test_a_binding_follows_its_process_to_a_new_session_id() -> None:
    cleared = make_session("new-id", observed_at=NOW)
    table = {4242: Proc(4242, 1, STARTED)}
    before = [binding("enki")]

    after = refresh_bindings(before, [cleared], table, NOW)

    assert after[0].session_id == "new-id"


def test_a_binding_whose_process_exited_fails() -> None:
    after = refresh_bindings([binding("enki")], SESSIONS, {}, NOW)

    assert (after[0].state, after[0].detail) == ("failed", "process exited")


def test_a_reused_pid_does_not_keep_a_binding_alive() -> None:
    table = {4242: Proc(4242, 1, STARTED + timedelta(hours=2))}

    assert refresh_bindings([binding("enki")], SESSIONS, table, NOW)[0].state == "failed"


def test_a_codex_binding_is_keyed_by_thread_not_pid() -> None:
    codex = binding("nanna", harness="codex", pid=None, session_id="2e2099ff-0000-1111")

    assert refresh_bindings([codex], SESSIONS, {}, NOW) == [codex]
    assert resolve("nanna", SESSIONS, CONFIG, [codex]).session == SESSIONS[1]


def test_bindings_stay_as_they_are_when_the_process_table_is_unavailable() -> None:
    assert refresh_bindings([binding("enki")], SESSIONS, None, NOW) == [binding("enki")]


def test_agent_of_names_the_live_binding_for_a_session() -> None:
    assert agent_of(SESSIONS[0], [binding("enki")]) == "enki"
    assert agent_of(SESSIONS[0], [binding("enki", state="failed")]) is None
