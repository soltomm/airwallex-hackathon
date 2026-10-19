from __future__ import annotations

# The same "not enough money" state comes back under three codes depending on the endpoint.
INSUFFICIENT_FUNDS_CODES = frozenset({"balance_insufficient", "insufficient_fund", "insufficient_funds"})


class AirwallexError(Exception):
    """A definite failure: Airwallex answered and rejected the request."""

    def __init__(self, status: int, code: str | None, message: str, body: dict | None = None):
        super().__init__(f"[{status}] {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.body = body or {}

    @property
    def insufficient_funds(self) -> bool:
        return self.code in INSUFFICIENT_FUNDS_CODES

    @property
    def duplicate_request(self) -> bool:
        return self.code == "duplicate_request_id"


class AmbiguousResult(Exception):
    """A money-moving call whose outcome is unknown (timeout, 5xx, dropped connection).

    Do not retry blindly. Look the object up by its request_id first, so a retry can
    never create a second transfer or charge.
    """

    def __init__(self, method: str, path: str, request_id: str | None, cause: object):
        super().__init__(f"{method} {path} outcome unknown (request_id={request_id}): {cause}")
        self.method = method
        self.path = path
        self.request_id = request_id
        self.cause = cause
