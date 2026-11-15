from collections.abc import Mapping, Sequence

from bellhop.codes import HLS_UNAVAILABLE, Code, exit_code_for
from bellhop.models import Message


class BellhopError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: Code,
        candidates: Sequence[str] | None = None,
        stored: Message | None = None,
        fields: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: Code = code
        self.exit_code = exit_code_for(code)
        self.candidates = list(candidates) if candidates is not None else None
        self.stored = stored
        self.fields = dict(fields or {})


class HlsUnavailable(BellhopError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=HLS_UNAVAILABLE)
