"""Low-level Airwallex REST client: auth, token cache, retries, on-behalf-of."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ..config import Config
from .errors import AirwallexError, AmbiguousResult


@dataclass
class Response:
    status: int
    body: dict | list
    raw: bytes = b""


# (method, url, headers, body_bytes) -> Response. Swap it out in tests.
Transport = Callable[[str, str, dict, "bytes | None"], Response]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 30) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        # The API edge answers some rejections with an HTML page instead of JSON.
        parsed = {"code": "non_json_response", "message": raw[:200].decode(errors="replace")}
    return Response(status, parsed, raw)


class AirwallexClient:
    TOKEN_REFRESH_MARGIN = 60  # seconds before expiry
    MAX_RETRIES = 4

    def __init__(self, config: Config, transport: Transport = urllib_transport, sleep=time.sleep):
        self.config = config
        self._transport = transport
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires = 0.0

    # ---- auth ---------------------------------------------------------------
    def token(self) -> str:
        if self._token and time.time() < self._token_expires - self.TOKEN_REFRESH_MARGIN:
            return self._token
        r = self._transport(
            "POST",
            self.config.api_base + "/authentication/login",
            {"x-client-id": self.config.client_id, "x-api-key": self.config.api_key},
            None,
        )
        if r.status != 201 or not isinstance(r.body, dict) or "token" not in r.body:
            hint = ""
            if r.body.get("code") == "non_json_response":
                hint = " (HTML 403 usually means a malformed client ID or API key)"
            raise AirwallexError(r.status, r.body.get("code"), f"login failed{hint}", r.body)
        self._token = r.body["token"]
        self._token_expires = _parse_expiry(r.body.get("expires_at"))
        return self._token

    # ---- requests -----------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        params: dict | None = None,
        on_behalf_of: str | None = None,
        base: str | None = None,
    ) -> dict | list:
        url = (base or self.config.api_base) + path
        if params:
            from urllib.parse import urlencode

            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        payload = json.dumps(body).encode() if body is not None else None
        request_id = (body or {}).get("request_id")
        obo = on_behalf_of or self.config.on_behalf_of
        is_read = method == "GET"

        for attempt in range(self.MAX_RETRIES + 1):
            headers = {"Authorization": "Bearer " + self.token(), "Content-Type": "application/json"}
            if obo:
                headers["x-on-behalf-of"] = obo
            try:
                r = self._transport(method, url, headers, payload)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if is_read and attempt < self.MAX_RETRIES:
                    self._backoff(attempt)
                    continue
                raise AmbiguousResult(method, path, request_id, e) if not is_read else e

            if r.status == 401 and attempt == 0:
                self._token = None  # token expired early; log in again once
                continue
            if r.status == 429 and attempt < self.MAX_RETRIES:
                self._backoff(attempt)  # rate-limited requests were not processed: safe to retry
                continue
            if r.status >= 500:
                if is_read and attempt < self.MAX_RETRIES:
                    self._backoff(attempt)
                    continue
                if not is_read:
                    raise AmbiguousResult(method, path, request_id, f"HTTP {r.status}: {r.body}")
            if r.status >= 400:
                b = r.body if isinstance(r.body, dict) else {}
                raise AirwallexError(r.status, b.get("code"), b.get("message", ""), b)
            return r.body
        raise AirwallexError(429, "rate_limited", "gave up after retries")

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body: dict | None = None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(0.5 * 2**attempt, 8))


def _parse_expiry(value: str | None) -> float:
    if not value:
        return time.time() + 25 * 60
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(value.replace("Z", "+0000"), fmt).timestamp()
        except ValueError:
            pass
    return time.time() + 25 * 60


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
