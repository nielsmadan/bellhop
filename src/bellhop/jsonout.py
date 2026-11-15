import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from bellhop.errors import BellhopError
from bellhop.models import Session

SCHEMA_VERSION = 1


def ok_payload(warnings: Sequence[str], fields: Mapping[str, object]) -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "status": "ok", "warnings": list(warnings), **fields}


def error_payload(error: BellhopError) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "exit_code": error.exit_code,
        "error": {"code": error.code, "message": str(error)},
    }


def dumps(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_object(session: Session) -> dict[str, object]:
    return {
        "session_id": session.session_id,
        "harness": session.harness,
        "title": session.title,
        "cwd": str(session.cwd) if session.cwd else None,
        "phase": session.phase,
        "observed_at": iso_utc(session.observed_at),
        "agent": None,
        "reachable": False,
        "route": None,
        "pending": 0,
    }
