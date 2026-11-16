class HavocError(Exception):
    """An actionable error that can be shown without a traceback."""


class GraphError(HavocError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        hint = ""
        if status == 401:
            hint = " Run havoc365 login again."
        elif status == 403:
            hint = " Check admin consent, the required Entra roles, and tenant licensing."
        super().__init__(f"Microsoft Graph returned {status} ({code}): {message}.{hint}")
