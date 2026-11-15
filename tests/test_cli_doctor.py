import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bellhop.cli import main
from bellhop.cli.doctor import remove_if_unchanged, socket_inode
from bellhop.store import connect, insert_message
from conftest import ALPHA, Bell, check_golden, make_message


def checks(payload: dict[str, object]) -> dict[str, tuple[bool, str]]:
    rows = payload["checks"]
    assert isinstance(rows, list)
    return {row["name"]: (row["ok"], row["detail"]) for row in rows}


@pytest.fixture
def runtime(bell: Bell, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = Path("/tmp") / f"bh-doctor-{bell.tmp.name[-12:]}"
    monkeypatch.setenv("BELLHOP_RUNTIME_DIR", str(path))
    yield path
    for child in path.glob("*") if path.exists() else []:
        child.unlink()
    if path.exists():
        path.rmdir()


def test_a_healthy_setup_passes(bell: Bell, runtime: Path) -> None:
    code, payload = bell.run("doctor")

    assert code == 0
    found = checks(payload)
    assert found["hooklinesinker"] == (True, "4 live sessions")
    assert found["runtime directory"] == (True, str(runtime))
    assert found["store"] == (True, "schema version 2 of 2")
    assert found["state directory"] == (True, str(bell.tmp / "state-home" / "bellhop"))
    assert found["sockets"] == (True, "none dead")
    assert payload["pruned"] is None


def test_an_unsafe_runtime_directory_fails_and_names_the_path(bell: Bell, runtime: Path) -> None:
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)

    code, payload = bell.run("doctor")

    assert code == 1
    assert checks(payload)["runtime directory"] == (
        False,
        f"runtime directory {runtime} has mode 0755; it must be 0700",
    )
    assert payload["error"] == {
        "code": "doctor_failed",
        "message": "1 doctor checks failed: runtime directory",
    }
    check_golden("doctor-failed", payload, {str(runtime): "<runtime>", str(bell.tmp): "<tmp>"})


def test_a_state_directory_others_can_read_fails(bell: Bell, runtime: Path) -> None:
    bell.run("doctor")
    state = bell.tmp / "state-home" / "bellhop"
    state.chmod(0o755)

    code, payload = bell.run("doctor")

    assert code == 1
    assert checks(payload)["state directory"] == (False, f"{state}: mode 0755; it must be 0700")


def test_doctor_text_lists_the_checks_and_names_the_failure(
    bell: Bell, runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)

    code = main(["doctor"])

    out, err = capsys.readouterr()
    assert code == 1
    assert f"FAIL runtime directory: runtime directory {runtime} has mode 0755" in out
    assert err == "bellhop: 1 doctor checks failed: runtime directory\n"


def test_dead_sockets_are_reported_and_pruned(bell: Bell, runtime: Path) -> None:
    runtime.mkdir(mode=0o700)
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(str(runtime / "claude-1.sock"))
    dead.close()

    live = socket.socket(socket.AF_UNIX)
    live.bind(str(runtime / "pi-2.sock"))
    live.listen()
    (runtime / "notes.sock").write_text("not a socket")

    code, payload = bell.run("doctor", "--prune")
    live.close()

    assert code == 1
    assert checks(payload)["sockets"] == (False, "dead: claude-1.sock")
    assert sorted(path.name for path in runtime.iterdir()) == ["notes.sock", "pi-2.sock"]
    assert payload["pruned"] == {"messages": 0, "sockets": 1}
    assert not (runtime / "claude-1.sock").exists()


def test_prune_deletes_final_messages_older_than_30_days(bell: Bell, runtime: Path) -> None:
    bell.run("doctor")
    conn = connect(bell.tmp / "state-home" / "bellhop" / "bellhop.sqlite")
    old = datetime.now(UTC) - timedelta(days=31)
    insert_message(conn, make_message(1, state="failed", created_at=old))
    insert_message(conn, make_message(2, urgency="hold", created_at=old))
    conn.close()

    code, payload = bell.run("doctor", "--prune")

    assert code == 0
    assert payload["pruned"] == {"messages": 1, "sockets": 0}


def test_pending_work_without_a_drainer_starts_one(
    bell: Bell, runtime: Path, drainer_starts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bell.route(ALPHA, outcomes=["busy"])
    bell.run("send", ALPHA, "waits", "--from", "enki")
    drainer_starts.clear()
    monkeypatch.setattr("bellhop.cli.doctor.drainer_running", lambda env: False)

    code, payload = bell.run("doctor")

    assert code == 1
    assert checks(payload)["pending work"] == (
        False,
        "1 messages wait and no drainer runs; started the drainer",
    )
    assert drainer_starts != []


def test_a_socket_replaced_after_the_probe_is_kept(runtime: Path) -> None:
    runtime.mkdir(mode=0o700)
    path, fresh = runtime / "claude-1.sock", runtime / "claude-1.tmp"
    for name in (path, fresh):
        bound = socket.socket(socket.AF_UNIX)
        bound.bind(str(name))
        bound.close()
    probed = socket_inode(path)
    assert probed is not None
    fresh.rename(path)

    assert remove_if_unchanged(path, probed) is False
    assert socket_inode(path) not in (None, probed)


def test_doctor_text_prints_hls_problems_as_warnings(
    bell: Bell, runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bell.hls.respond([], problems=[{"message": "sink unreachable"}])

    main(["doctor"])

    assert capsys.readouterr().err.startswith("bellhop: warning: ")
