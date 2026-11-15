import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from bellhop.errors import BellhopError
from bellhop.models import Binding, Harness, Message, Session

SCHEMA_VERSION = 1


def ok_payload(warnings: Sequence[str], fields: Mapping[str, object]) -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "status": "ok", "warnings": list(warnings), **fields}


def error_payload(error: BellhopError) -> dict[str, object]:
    detail: dict[str, object] = {"code": error.code, "message": str(error)}
    if error.candidates is not None:
        detail["candidates"] = error.candidates
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "exit_code": error.exit_code,
        "error": detail,
    }
    if error.stored is not None:
        payload["message"] = message_object(error.stored)
    return {**payload, **error.fields}


def dumps(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def iso_utc(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def message_object(message: Message) -> dict[str, object]:
    delivered = message.state in ("sent", "confirmed")
    is_agent = message.target_kind == "agent"
    return {
        "id": message.id,
        "key": message.key,
        "sender": message.sender,
        "urgency": message.urgency,
        "state": message.state,
        "degraded_from": message.degraded_from,
        "route": message.route or "none",
        "detail": message.detail,
        "created_at": iso_utc(message.created_at),
        "sent_at": iso_utc(message.sent_at) if delivered else None,
        "confirmed_at": iso_utc(message.confirmed_at),
        "target": {
            "kind": message.target_kind,
            "agent": message.target if is_agent else None,
            "session_id": message.session_id if is_agent else message.target,
            "harness": message.harness,
            "title": message.title,
        },
    }


def session_object(
    session: Session, *, agent: str | None, route: str | None, reachable: bool, pending: int
) -> dict[str, object]:
    return {
        "session_id": session.session_id,
        "harness": session.harness,
        "title": session.title,
        "cwd": str(session.cwd) if session.cwd else None,
        "phase": session.phase,
        "observed_at": iso_utc(session.observed_at),
        "agent": agent,
        "reachable": reachable,
        "route": route,
        "pending": pending,
    }


def binding_object(binding: Binding | None) -> dict[str, object] | None:
    if binding is None:
        return None
    return {
        "state": binding.state,
        "session_id": binding.session_id,
        "cause": binding.cause,
        "detail": binding.detail,
    }


def agent_object(
    name: str, harness: Harness | None, *, configured: bool, binding: Binding | None
) -> dict[str, object]:
    return {
        "agent": name,
        "harness": harness,
        "configured": configured,
        "binding": binding_object(binding),
    }


def check_object(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "ok": ok, "detail": detail}


def whoami_object(session: Session, agent: str | None) -> dict[str, object]:
    return {
        "session_id": session.session_id,
        "harness": session.harness,
        "title": session.title,
        "agent": agent,
    }
