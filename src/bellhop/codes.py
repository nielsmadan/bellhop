from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

Code = Literal[
    "binding_conflict",
    "caller_unknown",
    "doctor_failed",
    "export_failed",
    "hls_unavailable",
    "internal_error",
    "interrupted",
    "invalid_config",
    "invalid_environment",
    "launch_failed",
    "no_route",
    "push_rejected",
    "runtime_dir_unsafe",
    "store_error",
    "target_ambiguous",
    "target_not_found",
    "usage",
]

BINDING_CONFLICT: Code = "binding_conflict"
CALLER_UNKNOWN: Code = "caller_unknown"
DOCTOR_FAILED: Code = "doctor_failed"
EXPORT_FAILED: Code = "export_failed"
HLS_UNAVAILABLE: Code = "hls_unavailable"
INTERNAL_ERROR: Code = "internal_error"
INTERRUPTED: Code = "interrupted"
INVALID_CONFIG: Code = "invalid_config"
INVALID_ENVIRONMENT: Code = "invalid_environment"
LAUNCH_FAILED: Code = "launch_failed"
NO_ROUTE: Code = "no_route"
PUSH_REJECTED: Code = "push_rejected"
RUNTIME_DIR_UNSAFE: Code = "runtime_dir_unsafe"
STORE_ERROR: Code = "store_error"
TARGET_AMBIGUOUS: Code = "target_ambiguous"
TARGET_NOT_FOUND: Code = "target_not_found"
USAGE: Code = "usage"

FAILURE_EXIT = 1
USAGE_EXIT = 2
TARGET_EXIT = 3
NO_ROUTE_EXIT = 4
HLS_UNAVAILABLE_EXIT = 5
INTERRUPTED_EXIT = 130

EXIT_CODES: Mapping[Code, int] = MappingProxyType(
    {
        BINDING_CONFLICT: FAILURE_EXIT,
        CALLER_UNKNOWN: TARGET_EXIT,
        DOCTOR_FAILED: FAILURE_EXIT,
        EXPORT_FAILED: FAILURE_EXIT,
        HLS_UNAVAILABLE: HLS_UNAVAILABLE_EXIT,
        INTERNAL_ERROR: FAILURE_EXIT,
        INTERRUPTED: INTERRUPTED_EXIT,
        INVALID_CONFIG: USAGE_EXIT,
        INVALID_ENVIRONMENT: FAILURE_EXIT,
        LAUNCH_FAILED: FAILURE_EXIT,
        NO_ROUTE: NO_ROUTE_EXIT,
        PUSH_REJECTED: FAILURE_EXIT,
        RUNTIME_DIR_UNSAFE: FAILURE_EXIT,
        STORE_ERROR: FAILURE_EXIT,
        TARGET_AMBIGUOUS: TARGET_EXIT,
        TARGET_NOT_FOUND: TARGET_EXIT,
        USAGE: USAGE_EXIT,
    }
)


def exit_code_for(code: Code) -> int:
    return EXIT_CODES[code]
