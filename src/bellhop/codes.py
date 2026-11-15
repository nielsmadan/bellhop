from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

Code = Literal[
    "hls_unavailable",
    "internal_error",
    "interrupted",
    "invalid_environment",
    "usage",
]

HLS_UNAVAILABLE: Code = "hls_unavailable"
INTERNAL_ERROR: Code = "internal_error"
INTERRUPTED: Code = "interrupted"
INVALID_ENVIRONMENT: Code = "invalid_environment"
USAGE: Code = "usage"

FAILURE_EXIT = 1
USAGE_EXIT = 2
HLS_UNAVAILABLE_EXIT = 5
INTERRUPTED_EXIT = 130

EXIT_CODES: Mapping[Code, int] = MappingProxyType(
    {
        HLS_UNAVAILABLE: HLS_UNAVAILABLE_EXIT,
        INTERNAL_ERROR: FAILURE_EXIT,
        INTERRUPTED: INTERRUPTED_EXIT,
        INVALID_ENVIRONMENT: FAILURE_EXIT,
        USAGE: USAGE_EXIT,
    }
)


def exit_code_for(code: Code) -> int:
    return EXIT_CODES[code]
