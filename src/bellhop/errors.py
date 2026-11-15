from bellhop.codes import HLS_UNAVAILABLE, Code, exit_code_for


class BellhopError(Exception):
    def __init__(self, message: str, *, code: Code) -> None:
        super().__init__(message)
        self.code: Code = code
        self.exit_code = exit_code_for(code)


class HlsUnavailable(BellhopError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=HLS_UNAVAILABLE)
