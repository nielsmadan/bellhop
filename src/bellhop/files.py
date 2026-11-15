import errno
import os
import stat
from pathlib import Path


def private_file(descriptor: int, path: Path) -> int:
    info = os.fstat(descriptor)
    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid():
        return descriptor
    os.close(descriptor)
    raise OSError(errno.EPERM, "not a private regular file", str(path))


def open_private(path: Path, flags: int, mode: int = 0o600) -> int:
    safe = flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    return private_file(os.open(path, safe, mode), path)


def read_private(path: Path, limit: int) -> str:
    descriptor = open_private(path, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read(limit).decode("utf-8", errors="replace")


def write_private(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}")
    descriptor = open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def drop_planted(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    planted = not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
    if planted:
        path.unlink(missing_ok=True)
