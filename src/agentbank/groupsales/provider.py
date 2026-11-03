"""Payment providers: the Airwallex sandbox, and a simulated twin with the same shape.

Both return plain dicts with amounts in pence. The engine never sees API credentials beyond
this module, and neither agent ever sees this module.

Airwallex endpoints used (Payment Acceptance):
  POST /pa/payment_intents/create          GET /pa/payment_intents/{id}
  GET  /pa/payment_intents?merchant_order_id=   POST /pa/payment_intents/{id}/cancel
  POST /pa/refunds/create                  GET /pa/refunds/{id}      GET /pa/refunds?payment_intent_id=
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path

from ..airwallex.errors import AirwallexError, AmbiguousResult  # noqa: F401  (re-exported for the engine)


def to_pence(amount) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal(1)))


def to_major(pence: int) -> str:
    return str((Decimal(pence) / 100).quantize(Decimal("0.01")))


def _intent(raw: dict) -> dict:
    return {"id": raw.get("id"), "status": raw.get("status"), "amount": to_pence(raw.get("amount", 0)),
            "currency": raw.get("currency"), "merchant_order_id": raw.get("merchant_order_id"),
            "client_secret": raw.get("client_secret"), "metadata": raw.get("metadata") or {},
            "captured_amount": to_pence(raw.get("captured_amount") or 0), "request_id": raw.get("request_id")}


def _refund(raw: dict) -> dict:
    return {"id": raw.get("id"), "status": raw.get("status"), "amount": to_pence(raw.get("amount", 0)),
            "currency": raw.get("currency"), "payment_intent_id": raw.get("payment_intent_id"),
            "request_id": raw.get("request_id"), "metadata": raw.get("metadata") or {},
            "failure_reason": raw.get("failure_reason")}


class AirwallexProvider:
    name = "airwallex"
    hosted = True

    def __init__(self, client=None):
        if client is None:
            from ..airwallex.client import AirwallexClient
            from ..config import Config
            client = AirwallexClient(Config.from_env())
        self.c = client

    def create_intent(self, request_id: str, amount: int, currency: str, merchant_order_id: str,
                      metadata: dict, return_url: str | None = None) -> dict:
        body = {"request_id": request_id, "amount": to_major(amount), "currency": currency,
                "merchant_order_id": merchant_order_id, "metadata": metadata,
                "descriptor": "Harbour House deposit"}
        if return_url:
            body["return_url"] = return_url
        return _intent(self.c.post("/pa/payment_intents/create", body))

    def get_intent(self, intent_id: str) -> dict:
        return _intent(self.c.get(f"/pa/payment_intents/{intent_id}"))

    def find_intents(self, merchant_order_id: str) -> list[dict]:
        r = self.c.get("/pa/payment_intents", params={"merchant_order_id": merchant_order_id})
        return [_intent(x) for x in r.get("items", [])]

    def cancel_intent(self, request_id: str, intent_id: str, reason: str) -> dict:
        return _intent(self.c.post(f"/pa/payment_intents/{intent_id}/cancel",
                                   {"request_id": request_id, "cancellation_reason": reason}))

    def create_refund(self, request_id: str, intent_id: str, amount: int, reason: str, metadata: dict) -> dict:
        return _refund(self.c.post("/pa/refunds/create", {
            "request_id": request_id, "payment_intent_id": intent_id, "amount": to_major(amount),
            "reason": reason, "metadata": metadata}))

    def get_refund(self, refund_id: str) -> dict:
        return _refund(self.c.get(f"/pa/refunds/{refund_id}"))

    def find_refunds(self, intent_id: str) -> list[dict]:
        r = self.c.get("/pa/refunds", params={"payment_intent_id": intent_id})
        return [_refund(x) for x in r.get("items", [])]


class SimulatedProvider:
    """An in-process stand-in for the sandbox with Airwallex's statuses and idempotency.

    Its records survive a hotel reset, exactly as Airwallex's do. Knobs (`knobs`) let tests and
    the presenter make the next creation time out, a refund fail, or a refund settle slowly.
    """
    name = "simulated"
    hosted = False

    def __init__(self, path: str | Path | None = None, refund_settle_seconds: float = 2.0):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.refund_settle_seconds = refund_settle_seconds
        self.knobs = {"timeout_next_create": False, "fail_next_refund": False}
        self.db = {"intents": {}, "refunds": {}, "requests": {}}
        if self.path and self.path.exists():
            self.db = json.loads(self.path.read_text())

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.db, indent=1))
            tmp.replace(self.path)

    def _dupe(self, request_id: str):
        if request_id in self.db["requests"]:
            raise AirwallexError(400, "duplicate_request_id", f"request_id {request_id} was already used",
                                 {"code": "duplicate_request_id"})

    def _maybe_timeout(self, request_id: str, method: str, path: str):
        if self.knobs.get("timeout_next_create"):
            self.knobs["timeout_next_create"] = False
            return True
        return False

    def create_intent(self, request_id, amount, currency, merchant_order_id, metadata, return_url=None):
        with self.lock:
            self._dupe(request_id)
            iid = "int_sim_" + uuid.uuid4().hex[:18]
            self.db["intents"][iid] = {"id": iid, "status": "REQUIRES_PAYMENT_METHOD", "amount": amount,
                                       "currency": currency, "merchant_order_id": merchant_order_id,
                                       "client_secret": "sim_secret_" + uuid.uuid4().hex[:12],
                                       "metadata": metadata, "captured_amount": 0, "request_id": request_id,
                                       "return_url": return_url, "created": time.time()}
            self.db["requests"][request_id] = iid
            self._save()
            if self._maybe_timeout(request_id, "POST", "/pa/payment_intents/create"):
                raise AmbiguousResult("POST", "/pa/payment_intents/create", request_id, "simulated timeout")
            return dict(self.db["intents"][iid])

    def get_intent(self, intent_id):
        with self.lock:
            if intent_id not in self.db["intents"]:
                raise AirwallexError(404, "resource_not_found", "no such intent", {})
            return dict(self.db["intents"][intent_id])

    def find_intents(self, merchant_order_id):
        with self.lock:
            return [dict(i) for i in self.db["intents"].values() if i["merchant_order_id"] == merchant_order_id]

    def cancel_intent(self, request_id, intent_id, reason):
        with self.lock:
            i = self.db["intents"][intent_id]
            if i["status"] in ("SUCCEEDED",):
                raise AirwallexError(400, "invalid_status_for_operation", "already succeeded", {})
            i["status"] = "CANCELLED"
            self._save()
            return dict(i)

    def pay(self, intent_id: str, card_number: str) -> dict:
        """What the simulated hosted page does when the presenter submits a card."""
        with self.lock:
            i = self.db["intents"][intent_id]
            if i["status"] != "REQUIRES_PAYMENT_METHOD":
                return dict(i)
            digits = card_number.replace(" ", "")
            if digits.startswith("4000000000000002"):
                i["last_error"] = "card declined"
            else:
                i["status"] = "SUCCEEDED"
                i["captured_amount"] = i["amount"]
            self._save()
            return dict(i)

    def create_refund(self, request_id, intent_id, amount, reason, metadata):
        with self.lock:
            self._dupe(request_id)
            i = self.db["intents"].get(intent_id)
            if not i or i["status"] != "SUCCEEDED":
                raise AirwallexError(400, "invalid_status_for_operation", "intent not refundable", {})
            refunded = sum(r["amount"] for r in self.db["refunds"].values()
                           if r["payment_intent_id"] == intent_id and r["status"] != "FAILED")
            if refunded + amount > i["captured_amount"]:
                raise AirwallexError(400, "refund_amount_exceeded", "amount exceeds refundable", {})
            rid = "rfd_sim_" + uuid.uuid4().hex[:18]
            fail = self.knobs.get("fail_next_refund")
            self.knobs["fail_next_refund"] = False
            self.db["refunds"][rid] = {"id": rid, "status": "RECEIVED", "amount": amount, "currency": i["currency"],
                                       "payment_intent_id": intent_id, "request_id": request_id, "reason": reason,
                                       "metadata": metadata, "created": time.time(), "will_fail": fail}
            self.db["requests"][request_id] = rid
            self._save()
            if self._maybe_timeout(request_id, "POST", "/pa/refunds/create"):
                raise AmbiguousResult("POST", "/pa/refunds/create", request_id, "simulated timeout")
            return self._public_refund(rid)

    def _advance_refund(self, rid):
        r = self.db["refunds"][rid]
        if r["status"] == "RECEIVED" and time.time() - r["created"] >= self.refund_settle_seconds:
            r["status"] = "FAILED" if r.get("will_fail") else "ACCEPTED"
            if r["status"] == "FAILED":
                r["failure_reason"] = "simulated provider failure"
            self._save()

    def _public_refund(self, rid):
        r = dict(self.db["refunds"][rid])
        r.pop("will_fail", None)
        return r

    def get_refund(self, refund_id):
        with self.lock:
            if refund_id not in self.db["refunds"]:
                raise AirwallexError(404, "resource_not_found", "no such refund", {})
            self._advance_refund(refund_id)
            return self._public_refund(refund_id)

    def find_refunds(self, intent_id):
        with self.lock:
            out = []
            for rid, r in self.db["refunds"].items():
                if r["payment_intent_id"] == intent_id:
                    self._advance_refund(rid)
                    out.append(self._public_refund(rid))
            return out


def make_provider(mode: str | None, var_dir: Path):
    """PAYMENTS=airwallex | simulated. Default: Airwallex when sandbox credentials are present."""
    import os
    from ..config import load_dotenv
    load_dotenv()
    mode = (mode or os.environ.get("PAYMENTS") or "").lower()
    if not mode:
        mode = "airwallex" if os.environ.get("AWX_CLIENT_ID") and os.environ.get("AWX_API_KEY") else "simulated"
    if mode == "airwallex":
        return AirwallexProvider()
    return SimulatedProvider(var_dir / "sim_provider.json")
