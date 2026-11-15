import json
from collections.abc import Mapping, Sequence

from bellhop.errors import BellhopError

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
