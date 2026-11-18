import argparse
import errno
import os
import socket
import stat
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from bellhop.cli.context import Context
from bellhop.cli.presentation import escape_controls, print_json, print_lines, print_warnings
from bellhop.codes import DOCTOR_FAILED
from bellhop.codex_session import remove_socket
from bellhop.drain import drainer_running, lock_owner, read_status
from bellhop.errors import BellhopError
from bellhop.jsonout import check_object, ok_payload
from bellhop.models import FAKE_ROUTES_ENV, Harness
from bellhop.paths import state_dir
from bellhop.routes import ENDPOINTS
from bellhop.routes.codex import SESSION_SOCKETS
from bellhop.routes.endpoint import PING_SECONDS, EndpointKind
from bellhop.routes.unix import SOCKET_PATH_LIMIT, WIRE, Pong, endpoint_path, ping
from bellhop.runtime import PRIVATE_MODE, resolve_runtime_dir
from bellhop.store import SCHEMA_VERSION, orphaned, prune, schema_version
from bellhop.wiring import fake_routes

PRUNE_AGE = timedelta(days=30)
LONGEST_HARNESS = "claude"
LONGEST_PID = 99_999 if sys.platform == "darwin" else 4_194_304
NEWER_WIRE = "upgrade bellhop to match the integration"
UNVERSIONED_WIRE = 1
REFUSED = frozenset({errno.ECONNREFUSED, errno.ENOENT})


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _dir_check(name: str, path: Path) -> Check:
    try:
        info = path.lstat()
    except OSError as error:
        return Check(name, False, f"{path}: {error.strerror or error}")
    if not stat.S_ISDIR(info.st_mode):
        return Check(name, False, f"{path}: not a directory")
    if info.st_uid != os.getuid():
        return Check(name, False, f"{path}: owned by uid {info.st_uid}")
    mode = stat.S_IMODE(info.st_mode)
    if mode != PRIVATE_MODE:
        return Check(name, False, f"{path}: mode {mode:04o}; it must be 0700")
    if not os.access(path, os.W_OK | os.X_OK):
        return Check(name, False, f"{path}: not writable by this process")
    return Check(name, True, str(path))


def socket_inode(path: Path) -> int | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    linked = stat.S_ISLNK(info.st_mode) and path.match(SESSION_SOCKETS)
    return info.st_ino if stat.S_ISSOCK(info.st_mode) or linked else None


@contextmanager
def inside(directory: Path) -> Iterator[None]:
    home = os.open(".", os.O_RDONLY)
    try:
        os.chdir(directory)
        yield
    finally:
        os.fchdir(home)
        os.close(home)


def refuses(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(1.0)
    try:
        if len(str(path).encode()) < SOCKET_PATH_LIMIT:
            probe.connect(str(path))
        else:
            with inside(path.parent):
                probe.connect(path.name)
    except OSError as error:
        return error.errno in REFUSED
    finally:
        probe.close()
    return False


def dead_sockets(runtime: Path) -> dict[Path, int]:
    try:
        candidates = sorted(runtime.glob("*.sock"))
    except OSError:
        return {}
    inodes = {path: socket_inode(path) for path in candidates}
    return {path: ino for path, ino in inodes.items() if ino is not None and refuses(path)}


def remove_if_unchanged(path: Path, inode: int) -> bool:
    if socket_inode(path) != inode:
        return False
    if path.is_symlink():
        remove_socket(path, os.getuid())
    else:
        path.unlink(missing_ok=True)
    return True


def answering_endpoints(runtime: Path, harness: str) -> dict[str, tuple[int, Pong]]:
    try:
        candidates = sorted(runtime.glob(f"{harness}-*.sock"))
    except OSError:
        return {}
    pids = {path: path.stem.removeprefix(f"{harness}-") for path in candidates}
    pongs = {path: ping(path, timeout=PING_SECONDS) for path, pid in pids.items() if pid.isdigit()}
    return {path.name: (int(pids[path]), pong) for path, pong in pongs.items() if pong is not None}


def wire_problem(kind: EndpointKind, name: str, wire: int) -> str:
    hint = kind.old_wire if wire < WIRE else NEWER_WIRE
    return f"{name} speaks bellhop wire {wire}, this bellhop wire {WIRE}; {hint}"


def endpoint_problems(
    kind: EndpointKind, pongs: Mapping[str, tuple[int, Pong]], listed: set[int]
) -> list[str]:
    unlisted = [name for name, (pid, _) in pongs.items() if pid not in listed]
    nameless = [
        name
        for name, (pid, pong) in pongs.items()
        if pid in listed and pong.current and pong.session is None
    ]
    problems = [
        wire_problem(kind, name, UNVERSIONED_WIRE if pong.wire is None else pong.wire)
        for name, (_, pong) in pongs.items()
        if not pong.current
    ]
    if nameless:
        problems.append(f"{', '.join(nameless)} answers but cannot name its session")
    if unlisted:
        names = ", ".join(unlisted)
        why = f"; {kind.unlisted}" if kind.unlisted else ""
        problems.append(f"hooklinesinker does not list the pid behind {names}{why}")
    return problems


def _endpoint(ctx: Context, runtime: Path, harness: Harness) -> Check | None:
    try:
        listed = {s.pid for s in ctx.sessions.sessions if s.pid is not None}
    except BellhopError:
        return None
    kind = ENDPOINTS[harness]
    problems = endpoint_problems(kind, answering_endpoints(runtime, harness), listed)
    name = f"{harness} sessions"
    if not problems:
        return Check(name, True, f"every answering {harness} {kind.piece} is listed and current")
    return Check(name, False, "; ".join(problems))


def _hls(ctx: Context) -> Check:
    try:
        count = len(ctx.sessions.sessions)
    except BellhopError as error:
        return Check("hooklinesinker", False, str(error))
    return Check("hooklinesinker", True, f"{count} live sessions")


def _runtime(ctx: Context) -> tuple[Check, Path | None]:
    try:
        path = resolve_runtime_dir(ctx.env, ctx.config)
    except BellhopError as error:
        return Check("runtime directory", False, str(error)), None
    return _dir_check("runtime directory", path), path


def _socket_paths(runtime: Path) -> Check:
    try:
        longest = endpoint_path(runtime, LONGEST_HARNESS, LONGEST_PID)
    except BellhopError as error:
        return Check("socket paths", False, str(error))
    return Check("socket paths", True, f"fit: {len(str(longest).encode())} bytes at most")


def _store(ctx: Context) -> Check:
    try:
        version = schema_version(ctx.conn)
    except BellhopError as error:
        return Check("store", False, str(error))
    return Check("store", True, f"schema version {version} of {SCHEMA_VERSION}")


def _work(ctx: Context) -> Check:
    waiting = orphaned(ctx.conn)
    try:
        return _drainer(ctx, waiting)
    except OSError as error:
        detail = f"{waiting} messages wait; cannot check or start the drainer: {error}"
        return Check("pending work", False, detail)


def _drainer_health(ctx: Context) -> list[Check]:
    status = read_status(ctx.env)
    running = status is not None and lock_owner(ctx.env) == status.pid
    if not running or status is None or not drainer_running(ctx.env):
        return []
    if status.hls_ok is False:
        detail = (
            f"drainer {status.pid} cannot run hooklinesinker ({status.hooklinesinker or 'not found'}"
            f"): {status.last_error}; stop it; the next bellhop command starts a fresh one"
        )
        return [Check("drainer", False, detail)]
    found = status.hooklinesinker or "not found at its start"
    return [Check("drainer", True, f"pid {status.pid}, hooklinesinker {found}")]


def _drainer(ctx: Context, waiting: int) -> Check:
    if waiting and not drainer_running(ctx.env):
        started = ctx.starter(ctx.env)
        note = "; started the drainer" if started is not None else ""
        return Check("pending work", False, f"{waiting} messages wait and no drainer runs{note}")
    return Check("pending work", True, f"{waiting} messages wait for the drainer")


def _fake_routes(ctx: Context) -> list[Check]:
    if not ctx.env.get(FAKE_ROUTES_ENV) or fake_routes(ctx.env):
        return []
    detail = f"{FAKE_ROUTES_ENV} is set and ignored: it acts only on a non-default state dir"
    return [Check("fake routes", False, detail)]


def run_checks(ctx: Context) -> tuple[list[Check], Path | None]:
    hls = _hls(ctx)
    store = _store(ctx)
    state = _dir_check("state directory", state_dir(ctx.env))
    runtime, runtime_path = _runtime(ctx)
    checks = [hls, state, runtime, store, *_fake_routes(ctx)]
    if store.ok:
        checks.append(_work(ctx))
    try:
        checks += _drainer_health(ctx)
    except OSError as error:
        checks.append(Check("drainer", False, f"cannot check the drainer: {error}"))
    if runtime_path is not None and runtime.ok:
        checks.append(_socket_paths(runtime_path))
        dead = dead_sockets(runtime_path)
        names = ", ".join(path.name for path in dead)
        checks.append(Check("sockets", not dead, f"dead: {names}" if dead else "none dead"))
        found = [_endpoint(ctx, runtime_path, harness) for harness in ENDPOINTS]
        checks += [check for check in found if check is not None]
    return checks, runtime_path


def prune_all(ctx: Context, runtime: Path | None) -> dict[str, int]:
    removed = prune(ctx.conn, ctx.now() - PRUNE_AGE)
    dead = dead_sockets(runtime) if runtime is not None else {}
    unlinked = [path for path, inode in dead.items() if remove_if_unchanged(path, inode)]
    return {"messages": removed, "sockets": len(unlinked)}


def check_line(check: Check) -> str:
    return f"{'ok  ' if check.ok else 'FAIL'} {check.name}: {escape_controls(check.detail)}"


def doctor(args: argparse.Namespace, ctx: Context) -> int:
    checks, runtime = run_checks(ctx)
    store_ok = any(check.name == "store" and check.ok for check in checks)
    pruned = prune_all(ctx, runtime) if args.prune and store_ok else None
    if pruned is not None:
        checks, runtime = run_checks(ctx)
    failed = [check.name for check in checks if not check.ok]
    fields = {"checks": [check_object(c.name, c.ok, c.detail) for c in checks], "pruned": pruned}
    if not args.json:
        print_warnings(ctx.warnings)
        lines = [check_line(check) for check in checks]
        if pruned is not None:
            lines.append(f"pruned {pruned['messages']} messages and {pruned['sockets']} sockets")
        print_lines(lines)
    if failed:
        raise BellhopError(
            f"{len(failed)} doctor checks failed: {', '.join(failed)}",
            code=DOCTOR_FAILED,
            fields=fields,
        )
    if args.json:
        print_json(ok_payload(ctx.warnings, fields))
    return 0
