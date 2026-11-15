import argparse
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path

from bellhop.cli.context import Context
from bellhop.cli.presentation import print_json, print_lines, print_warnings
from bellhop.codes import EXPORT_FAILED, USAGE
from bellhop.errors import BellhopError
from bellhop.jsonout import integration_object, ok_payload
from bellhop.routes.pi import UNLISTED_PI
from bellhop.wiring import pi_agent_dir

FILES = {"claude": "bellhop-mcp.json", "pi": "bellhop.ts"}
SHARED_WRITE = stat.S_IWGRP | stat.S_IWOTH


def packaged(harness: str) -> Traversable:
    name = FILES.get(harness)
    if name is None:
        known = ", ".join(sorted(FILES))
        raise BellhopError(f"no {harness} integration yet; available: {known}", code=USAGE)
    return files("bellhop") / "_integrations" / harness / name


def install_dir(harness: str, ctx: Context) -> Path | None:
    if harness != "pi":
        return None
    agent_dir = pi_agent_dir(ctx.env)
    return (agent_dir if agent_dir is not None else Path("~/.pi/agent")) / "extensions"


def claude_lines(source: Path, target: Path | None) -> list[str]:
    return [
        (
            "bellhop's Claude relay is an MCP server, `bellhop _relay`. It listens on "
            "<runtime_dir>/claude-<pid>.sock and delivers through Claude's own messaging socket."
        ),
        f"packaged file: {source}",
        "install for every session: claude mcp add --scope user bellhop -- bellhop _relay",
        (
            "or for one run: claude --mcp-config <exported dir>/bellhop-mcp.json -- <prompt> "
            "(end the --mcp-config list before a prompt)"
        ),
        (
            "bellhop must be on the PATH Claude starts MCP servers with; a running session keeps "
            "the relay it started with until it is restarted."
        ),
        "bellhop never edits Claude's configuration.",
    ]


def pi_lines(source: Path, target: Path | None) -> list[str]:
    return [
        "bellhop's Pi extension listens on <runtime_dir>/pi-<pid>.sock and delivers into the session.",
        f"packaged file: {source}",
        f"install: bellhop integration export pi --to {target}",
        "Pi loads every *.ts in that directory at start; run /reload in a running Pi.",
        "or load it for one run: pi -e <exported dir>/bellhop.ts",
        f"{UNLISTED_PI}.",
    ]


LINES: Mapping[str, Callable[[Path, Path | None], list[str]]] = {
    "claude": claude_lines,
    "pi": pi_lines,
}


def show(args: argparse.Namespace, ctx: Context) -> int:
    resource = packaged(args.harness)
    target = install_dir(args.harness, ctx)
    with as_file(resource) as source:
        lines = LINES[args.harness](source, target)
        if args.json:
            fields = integration_object(args.harness, source, install_dir=target, lines=lines)
            print_json(ok_payload(ctx.warnings, {"integration": fields}))
            return 0
    print_warnings(ctx.warnings)
    print_lines(lines)
    return 0


def safe_directory(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = directory.resolve()
    info = resolved.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise BellhopError(f"{directory} is not a directory", code=USAGE)
    if info.st_uid != os.getuid() or info.st_mode & SHARED_WRITE:
        raise BellhopError(f"{directory} must be owned by you and writable only by you", code=USAGE)
    return resolved


def copy_into(resource: Traversable, directory: Path) -> Path:
    target = directory / resource.name
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{resource.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(resource.read_bytes())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def export(args: argparse.Namespace, ctx: Context) -> int:
    resource = packaged(args.harness)
    directory = Path(args.to).expanduser()
    try:
        target = copy_into(resource, safe_directory(directory))
    except OSError as error:
        raise BellhopError(
            f"cannot export to {directory}: {error.strerror or error}", code=EXPORT_FAILED
        ) from error
    if args.json:
        fields = integration_object(args.harness, target)
        print_json(ok_payload(ctx.warnings, {"integration": fields}))
        return 0
    print_warnings(ctx.warnings)
    print_lines([f"exported the {args.harness} integration to {target}"])
    return 0
