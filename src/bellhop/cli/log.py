import argparse
import json
from collections.abc import Mapping
from datetime import datetime

from bellhop.cli.context import Context
from bellhop.cli.presentation import escape_controls, print_json, print_lines, print_warnings
from bellhop.jsonout import ok_payload
from bellhop.trace import ENV, OFF, read_events, trace_path

HEAD = ("at", "src", "pid", "event")


def clock_time(at: object) -> str:
    try:
        moment = datetime.fromisoformat(str(at)).astimezone()
    except ValueError:
        return str(at)
    return moment.strftime("%H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def shown(value: object) -> str:
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False) if not value or " " in value else value


def event_line(event: Mapping[str, object]) -> str:
    fields = " ".join(f"{name}={shown(value)}" for name, value in event.items() if name not in HEAD)
    head = (
        f"{clock_time(event.get('at'))} {event.get('src')}[{event.get('pid')}] {event.get('event')}"
    )
    return escape_controls(f"{head} {fields}".rstrip())


def log(args: argparse.Namespace, ctx: Context) -> int:
    path = trace_path(ctx.env)
    if ctx.env.get(ENV) == OFF:
        ctx.warnings.append(f"tracing is off here ({ENV}={OFF}); showing what is on file")
    events = read_events(ctx.env, count=args.lines, words=args.words, failures=args.errors)
    if args.json:
        print_json(ok_payload(ctx.warnings, {"file": str(path), "events": events}))
        return 0
    print_warnings(ctx.warnings)
    print_lines([event_line(event) for event in events] or [f"no matching events in {path}"])
    return 0
