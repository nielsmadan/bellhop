import os
import threading
from pathlib import Path

import pytest

from bellhop.drain import DrainStatus, read_status, status_path, write_status
from bellhop.files import drop_planted, open_private, read_private, write_private


def victim(tmp_path: Path) -> Path:
    path = tmp_path / "zshrc"
    path.write_text("mine\n")
    return path


def test_writing_over_a_planted_link_replaces_the_link_and_leaves_its_target(
    tmp_path: Path,
) -> None:
    target = victim(tmp_path)
    status = tmp_path / "drain.status.json"
    status.symlink_to(target)

    write_private(status, "{}")

    assert (target.read_text(), status.is_symlink(), status.read_text()) == ("mine\n", False, "{}")


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_a_planted_link_is_never_opened_for_writing(tmp_path: Path, kind: str) -> None:
    target = victim(tmp_path)
    planted = tmp_path / "drain.log"
    if kind == "symlink":
        planted.symlink_to(target)
    else:
        os.link(target, planted)

    with pytest.raises(OSError):
        os.close(open_private(planted, os.O_WRONLY | os.O_APPEND | os.O_CREAT))

    assert target.read_text() == "mine\n"


def test_a_planted_link_is_dropped_and_a_real_file_kept(tmp_path: Path) -> None:
    target = victim(tmp_path)
    planted = tmp_path / "bellhop.sqlite-wal"
    planted.symlink_to(target)
    real = tmp_path / "bellhop.sqlite-shm"
    real.write_text("shm")

    drop_planted(planted)
    drop_planted(real)

    assert (planted.is_symlink(), real.read_text(), target.read_text()) == (False, "shm", "mine\n")


def test_reading_a_planted_fifo_neither_blocks_nor_reads(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    status_path(env).parent.mkdir(parents=True)
    os.mkfifo(status_path(env))
    found: list[DrainStatus | None] = []

    reader = threading.Thread(target=lambda: found.append(read_status(env)), daemon=True)
    reader.start()
    reader.join(5)

    assert found == [None]


def test_reading_through_a_planted_link_is_refused(tmp_path: Path) -> None:
    target = victim(tmp_path)
    link = tmp_path / "drain.lock"
    link.symlink_to(target)

    with pytest.raises(OSError):
        read_private(link, 100)


def test_the_status_file_round_trips(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    status_path(env).parent.mkdir(parents=True)
    status = DrainStatus(4242, "2026-11-14T09:00:00+00:00", "/bin/hls", True, None)

    write_status(status_path(env), status)

    assert read_status(env) == status
    assert [p.name for p in status_path(env).parent.iterdir()] == ["drain.status.json"]


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "drain.lock"
    os.mkfifo(fifo)

    with pytest.raises(OSError):
        os.close(open_private(fifo, os.O_RDONLY))


def test_the_status_file_is_never_written_through_a_planted_link(tmp_path: Path) -> None:
    target = victim(tmp_path)
    status = tmp_path / "drain.status.json"
    status.symlink_to(target)

    write_status(status, DrainStatus(4242, "2026-11-14T09:00:00+00:00", None))

    assert (target.read_text(), status.is_symlink()) == ("mine\n", False)


def test_a_planted_hard_link_is_dropped(tmp_path: Path) -> None:
    target = victim(tmp_path)
    planted = tmp_path / "bellhop.sqlite-shm"
    os.link(target, planted)

    drop_planted(planted)

    assert (planted.exists(), target.read_text()) == (False, "mine\n")
