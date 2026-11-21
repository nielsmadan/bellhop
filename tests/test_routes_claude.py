import json
import socket
import time
from pathlib import Path

import pytest

from bellhop.codes import RUNTIME_DIR_UNSAFE
from bellhop.errors import BellhopError
from bellhop.models import Session
from bellhop.routes import Route, RouteSetup, route_finder
from bellhop.routes.claude import ClaudeSocketRoute, own_socket, registered
from conftest import (
    CLAUDE_PID,
    CLAUDE_SESSION,
    accepting_claude,
    claude_peer,
    make_session,
    peer_frames,
    pong_line,
    pong_server,
)

PID = CLAUDE_PID
SESSION = CLAUDE_SESSION
OTHER = "c2a0de00-0000-4000-8000-00000000c2de"
TOKEN = "peer-token-0001"  # noqa: S105
configure = accepting_claude


def route(home: Path | None) -> ClaudeSocketRoute:
    return ClaudeSocketRoute(lambda session: home)


def claude(session_id: str = SESSION, **changes: object) -> Session:
    return make_session(session_id, **{"harness": "claude", "pid": PID, **changes})


def quiet(received: list[bytes], seconds: float = 0.1) -> bool:
    time.sleep(seconds)
    return received == []


def closed_socket(path: Path) -> None:
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))


def test_a_push_writes_the_sessions_own_peer_token_and_one_user_frame(short_tmp: Path) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)
    (home / "sessions" / "1111.abc.key").write_text(json.dumps({"peerToken": "another-pids"}))
    (home / "sessions" / f"{PID}.abc.key").write_text(json.dumps({"peerToken": TOKEN}))

    with claude_peer(sock) as received:
        result = route(home).push(claude(), "hello", interrupt=False)
        auth, user = peer_frames(received)

    assert (result.outcome, result.detail) == ("accepted", "later")
    assert auth == {"type": "auth", "token": TOKEN}
    assert (user["type"], user["priority"], user["message"], user["msgV"]) == (
        "user",
        "later",
        {"role": "user", "content": "hello"},
        1,
    )
    assert isinstance(user["msg_id"], str) and user["msg_id"]


@pytest.mark.parametrize(
    "key",
    [None, "not json", json.dumps({"peerToken": ""}), json.dumps({"peerToken": 5})],
    ids=["none", "garbled", "empty", "number"],
)
def test_an_interrupting_push_without_a_usable_peer_key_sends_only_the_user_frame(
    short_tmp: Path, key: str | None
) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)
    if key is not None:
        (home / "sessions" / f"{PID}.abc.key").write_text(key)

    with claude_peer(sock) as received:
        result = route(home).push(claude(), "hello", interrupt=True)
        (user,) = peer_frames(received)

    assert (result.outcome, result.detail) == ("accepted", "next")
    assert (user["type"], user["priority"]) == ("user", "next")


@pytest.mark.parametrize("inbound", ["hold", "refuse", "Accept", True, None])
def test_a_claude_that_does_not_accept_peer_messages_is_never_written_to(
    short_tmp: Path, inbound: object
) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock, inbound=inbound)
    reason = f"{home}/settings.json does not accept peer messages"

    with claude_peer(sock) as received:
        found = route(home)
        result = found.push(claude(), "hello", interrupt=False)

        assert (found.reachable(claude()), found.obstacle(claude())) == (False, reason)
        assert (result.outcome, result.detail) == ("busy", reason)
        assert quiet(received)


def test_an_accepting_claude_with_a_listening_socket_is_reachable(short_tmp: Path) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)

    with claude_peer(sock):
        found = route(home)

        assert (found.reachable(claude()), found.replaced(claude()), found.busy(claude())) == (
            True,
            False,
            None,
        )
        assert found.obstacle(claude()) == f"claude listens at {sock.resolve()}"


def test_a_pid_that_moved_to_another_session_is_replaced_and_not_pushed_to(
    short_tmp: Path,
) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock, session=OTHER)

    with claude_peer(sock) as received:
        found = route(home)
        result = found.push(claude(), "hello", interrupt=False)

        assert (found.reachable(claude()), found.replaced(claude())) == (False, True)
        assert (result.outcome, result.detail) == (
            "busy",
            f"claude pid {PID} now runs session {OTHER}",
        )
        assert quiet(received)


@pytest.mark.parametrize("session", [None, 7, "../../elsewhere"])
def test_a_registry_entry_without_a_session_id_is_neither_reachable_nor_replaced(
    short_tmp: Path, session: object
) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock, session=session)

    with claude_peer(sock):
        found = route(home)

        assert (found.reachable(claude()), found.replaced(claude())) == (False, False)
        assert found.obstacle(claude()) == f"claude pid {PID} lists no session id"


def test_a_registry_file_that_names_another_pid_is_not_this_sessions(short_tmp: Path) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock, pid=9999)
    reason = f"claude lists no session for pid {PID} in {home}/sessions"

    with claude_peer(sock) as received:
        result = route(home).push(claude(), "hello", interrupt=False)

        assert route(home).obstacle(claude()) == reason
        assert (result.outcome, result.detail) == ("busy", reason)
        assert quiet(received)


@pytest.mark.parametrize("name", ["9999.sock", "peer.sock"])
def test_a_socket_that_is_not_the_pids_own_is_never_written_to(short_tmp: Path, name: str) -> None:
    home, sock = short_tmp / "claude", short_tmp / name
    configure(home, sock)

    with claude_peer(sock) as received:
        result = route(home).push(claude(), "hello", interrupt=False)

        assert (result.outcome, result.detail) == (
            "busy",
            f"claude pid {PID} lists no messaging socket",
        )
        assert quiet(received)


def test_a_missing_socket_is_unreachable_and_says_it_is_gone(short_tmp: Path) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)
    reason = f"claude's messaging socket {sock} is gone"
    result = route(home).push(claude(), "hello", interrupt=False)

    assert (route(home).reachable(claude()), route(home).obstacle(claude())) == (False, reason)
    assert (result.outcome, result.detail) == ("busy", reason)


def test_a_socket_nothing_listens_on_is_reachable_and_a_push_to_it_is_busy(
    short_tmp: Path,
) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)
    closed_socket(sock)
    result = route(home).push(claude(), "hello", interrupt=False)

    assert route(home).reachable(claude()) is True
    assert (result.outcome, result.detail) == (
        "busy",
        "claude's messaging socket: Connection refused",
    )


def test_a_file_that_is_not_a_socket_is_not_connected_to(short_tmp: Path) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)
    sock.write_text("")

    assert route(home).reachable(claude()) is False
    assert route(home).obstacle(claude()) == (
        f"claude's messaging socket cannot be used: "
        f"{sock.resolve()} is not a socket owned by uid {sock.stat().st_uid}"
    )


def test_a_socket_in_a_directory_others_can_write_is_not_connected_to(short_tmp: Path) -> None:
    home, shared = short_tmp / "claude", short_tmp / "shared"
    shared.mkdir()
    sock = shared / f"{PID}.sock"
    configure(home, sock)

    with claude_peer(sock) as received:
        shared.chmod(0o777)
        owner = f"{shared.resolve()} is not a private directory of uid {shared.stat().st_uid}"
        result = route(home).push(claude(), "hello", interrupt=False)

        assert (result.outcome, result.detail) == (
            "busy",
            f"claude's messaging socket cannot be used: {owner}",
        )
        assert quiet(received)


@pytest.mark.parametrize("folder", ["", "sessions"], ids=["config", "sessions"])
def test_a_config_directory_others_can_write_is_not_trusted(short_tmp: Path, folder: str) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)
    (home / folder).chmod(0o777)

    with claude_peer(sock):
        found = route(home)

        assert (found.reachable(claude()), found.replaced(claude())) == (False, False)
        assert found.obstacle(claude()) == (
            f"{home / folder} is not a private directory of uid {home.stat().st_uid}"
        )


def test_a_session_without_a_config_directory_or_pid_has_no_socket_route(
    short_tmp: Path,
) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)
    reason = "no claude config directory or pid is known for this session"

    assert (route(None).reachable(claude()), route(None).obstacle(claude())) == (False, reason)
    assert route(home).obstacle(claude(pid=None)) == reason
    assert route(None).accepting(claude()) is False


@pytest.mark.parametrize("settings", [None, "not json", "[]"], ids=["absent", "garbled", "list"])
def test_settings_that_cannot_be_read_do_not_accept(short_tmp: Path, settings: str | None) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)
    (home / "settings.json").unlink()
    if settings is not None:
        (home / "settings.json").write_text(settings)

    with claude_peer(sock):
        assert (route(home).accepting(claude()), route(home).reachable(claude())) == (False, False)


def test_an_accepting_claude_that_lists_no_registry_entry_is_unreachable(short_tmp: Path) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    configure(home, sock)
    (home / "sessions" / f"{PID}.json").unlink()

    assert route(home).obstacle(claude()) == (
        f"claude lists no session for pid {PID} in {home}/sessions"
    )
    assert route(home).replaced(claude()) is False


def find(runtime: Path, home: Path, session: Session) -> Route:
    setup = RouteSetup(runtime_dir=lambda: runtime, claude_dir=lambda session, binding: home)
    found = route_finder(setup)(session, None)
    assert found is not None
    return found


def test_the_relay_is_used_when_it_answers_and_the_socket_only_without_it(
    short_tmp: Path,
) -> None:
    home, sock, runtime = short_tmp / "claude", short_tmp / f"{PID}.sock", short_tmp / "rt"
    runtime.mkdir(mode=0o700)
    configure(home, sock)

    with claude_peer(sock):
        without_relay = find(runtime, home, claude()).name
        with pong_server(runtime / f"claude-{PID}.sock", pong_line(SESSION)):
            with_relay = find(runtime, home, claude()).name
        configure(home, sock, inbound=None)
        neither = find(runtime, home, claude()).name

    assert (without_relay, with_relay, neither) == (
        "claude-socket",
        "claude-relay",
        "claude-relay",
    )


def test_a_push_through_the_finder_goes_over_the_socket_when_no_relay_answers(
    short_tmp: Path,
) -> None:
    home, sock, runtime = short_tmp / "claude", short_tmp / f"{PID}.sock", short_tmp / "rt"
    runtime.mkdir(mode=0o700)
    configure(home, sock)

    with claude_peer(sock) as received:
        found = find(runtime, home, claude())
        reachable = found.reachable(claude())
        result = found.push(claude(), "hello", interrupt=False)
        (user,) = peer_frames(received)

    assert (reachable, result.outcome, result.detail) == (True, "accepted", "later")
    assert user["message"] == {"role": "user", "content": "hello"}


def test_the_finders_route_reports_a_replaced_socket_only_session(short_tmp: Path) -> None:
    home, sock, runtime = short_tmp / "claude", short_tmp / f"{PID}.sock", short_tmp / "rt"
    runtime.mkdir(mode=0o700)
    configure(home, sock, session=OTHER)

    with claude_peer(sock):
        found = find(runtime, home, claude())

        assert (found.name, found.reachable(claude()), found.replaced(claude())) == (
            "claude-socket",
            False,
            True,
        )
        assert found.obstacle(claude()) == f"claude pid {PID} now runs session {OTHER}"


def test_the_finders_route_gives_the_sockets_reason_only_to_an_accepting_claude(
    short_tmp: Path,
) -> None:
    home, sock, runtime = short_tmp / "claude", short_tmp / f"{PID}.sock", short_tmp / "rt"
    runtime.mkdir(mode=0o700)
    configure(home, sock)
    accepting = find(runtime, home, claude()).obstacle(claude())
    configure(home, sock, inbound=None)
    unset = find(runtime, home, claude()).obstacle(claude())

    assert accepting == f"claude's messaging socket {sock} is gone"
    assert unset == (
        f"no bellhop relay listens at {runtime}/claude-{PID}.sock; "
        "start claude with bellhop's MCP config (`bellhop integration show claude`); "
        f"{home}/settings.json does not accept peer messages"
    )


def unusable() -> Path:
    raise BellhopError("runtime directory /rt has mode 0755", code=RUNTIME_DIR_UNSAFE)


def test_an_unusable_runtime_directory_fails_the_probe_unless_the_socket_route_works(
    short_tmp: Path,
) -> None:
    home, sock = short_tmp / "claude", short_tmp / f"{PID}.sock"
    find = route_finder(RouteSetup(runtime_dir=unusable, claude_dir=lambda session, binding: home))
    configure(home, sock)

    with claude_peer(sock):
        accepting = find(claude(), None)
        assert accepting is not None
        over_socket = (accepting.name, accepting.reachable(claude()))
        configure(home, sock, inbound=None)
        refusing = find(claude(), None)
        assert refusing is not None
        with pytest.raises(BellhopError, match="runtime directory /rt has mode 0755"):
            refusing.reachable(claude())

    assert over_socket == ("claude-socket", True)
    assert refusing.name == "claude-relay"


def test_a_route_keeps_the_choice_it_made_for_its_name_and_its_push(short_tmp: Path) -> None:
    home, sock, runtime = short_tmp / "claude", short_tmp / f"{PID}.sock", short_tmp / "rt"
    runtime.mkdir(mode=0o700)
    configure(home, sock)

    with claude_peer(sock) as received:
        found = find(runtime, home, claude())
        before = (found.reachable(claude()), found.name)
        with pong_server(runtime / f"claude-{PID}.sock", pong_line(SESSION)):
            after = found.name
            result = found.push(claude(), "hello", interrupt=False)
            fresh = find(runtime, home, claude()).name
        (user,) = peer_frames(received)

    assert (before, after, fresh) == ((True, "claude-socket"), "claude-socket", "claude-relay")
    assert (result.outcome, result.detail) == ("accepted", "later")
    assert user["message"] == {"role": "user", "content": "hello"}


def test_an_accepting_claude_whose_socket_is_gone_is_named_and_pushed_as_the_socket_route(
    short_tmp: Path,
) -> None:
    home, sock, runtime = short_tmp / "claude", short_tmp / f"{PID}.sock", short_tmp / "rt"
    runtime.mkdir(mode=0o700)
    configure(home, sock)
    found = find(runtime, home, claude())
    result = found.push(claude(), "hello", interrupt=False)

    assert (found.name, found.reachable(claude())) == ("claude-socket", False)
    assert (result.outcome, result.detail) == (
        "busy",
        f"claude's messaging socket {sock} is gone",
    )


@pytest.mark.parametrize(
    "entry", [{"pid": 5151, "sessionId": SESSION}, {"sessionId": SESSION}], ids=["other", "none"]
)
def test_a_registry_entry_that_does_not_name_our_pid_is_not_ours(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "4242.json").write_text(json.dumps(entry))

    assert registered(tmp_path, 4242) is None


def test_a_socket_is_a_claudes_own_when_it_is_named_after_its_pid() -> None:
    assert own_socket("/tmp/cc-socks/4242.sock", 4242) is True
    assert own_socket("/tmp/cc-socks/5151.sock", 4242) is False
