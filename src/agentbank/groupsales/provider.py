"""Payment providers: the Airwallex sandbox, and a simulated twin with the same shape.

Both return plain dicts with amounts in pence. The engine never sees API credentials beyond
this module, and neither agent ever sees this module.

Airwallex endpoints used (Payment Acceptance and Payments for Platforms):
  POST /pa/payment_intents/create          GET /pa/payment_intents/{id}
  GET  /pa/payment_intents?merchant_order_id=   POST /pa/payment_intents/{id}/cancel
  POST /pa/refunds/create                  GET /pa/refunds/{id}      GET /pa/refunds?payment_intent_id=
  POST /pa/funds_splits/create             GET /pa/funds_splits?source_id=
  POST /pa/funds_split_reversals/create
  POST /accounts/create, /accounts/{id}/submit, /account_capabilities/{cap}/enable   (hotel onboarding)
  GET  /accounts/{id}, /account_capabilities/payments_visa

The platform owns each card payment ("collect payments directly") and tags it with the hotel's
connected account; once the payment has SUCCEEDED, a funds split pays the hotel its share and the
platform's commission stays behind. A refund is paid from the platform, and a funds split reversal
takes the hotel's share of it back.
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


def _split(raw: dict) -> dict:
    return {"id": raw.get("id"), "status": raw.get("status"), "amount": to_pence(raw.get("amount", 0)),
            "currency": raw.get("currency"), "destination": raw.get("destination"),
            "source_id": raw.get("source_id"), "request_id": raw.get("request_id"),
            "metadata": raw.get("metadata") or {}}


def _refund(raw: dict) -> dict:
    return {"id": raw.get("id"), "status": raw.get("status"), "amount": to_pence(raw.get("amount", 0)),
            "currency": raw.get("currency"), "payment_intent_id": raw.get("payment_intent_id"),
            "request_id": raw.get("request_id"), "metadata": raw.get("metadata") or {},
            "failure_reason": raw.get("failure_reason")}


class AirwallexProvider:
    name = "airwallex"
    hosted = True
    default_account = None   # the hotel's connected account comes from GS_HOTEL_ACCOUNT

    def __init__(self, client=None):
        if client is None:
            from ..airwallex.client import AirwallexClient
            from ..config import Config
            client = AirwallexClient(Config.from_env())
        self.c = client

    def create_intent(self, request_id: str, amount: int, currency: str, merchant_order_id: str,
                      metadata: dict, return_url: str | None = None,
                      connected_account_id: str | None = None) -> dict:
        body = {"request_id": request_id, "amount": to_major(amount), "currency": currency,
                "merchant_order_id": merchant_order_id, "metadata": metadata,
                "descriptor": "Harbour House deposit"}
        if return_url:
            body["return_url"] = return_url
        if connected_account_id:
            body["connected_account_id"] = connected_account_id
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

    # Payments for Platforms: only on a SUCCEEDED intent; whatever is not split stays with the platform.
    def create_funds_split(self, request_id: str, intent_id: str, amount: int, destination: str,
                           metadata: dict) -> dict:
        return _split(self.c.post("/pa/funds_splits/create", {
            "request_id": request_id, "source_id": intent_id, "source_type": "PAYMENT_INTENT",
            "amount": to_major(amount), "destination": destination, "metadata": metadata,
            "auto_release": True}))

    def find_funds_splits(self, intent_id: str) -> list[dict]:
        r = self.c.get("/pa/funds_splits", params={"source_id": intent_id})
        return [_split(x) for x in r.get("items", [])]

    def reverse_funds_split(self, request_id: str, split_id: str, amount: int, metadata: dict) -> dict:
        r = self.c.post("/pa/funds_split_reversals/create", {
            "request_id": request_id, "funds_split_id": split_id, "amount": to_major(amount),
            "metadata": metadata})
        return {"id": r.get("id"), "status": r.get("status"),
                "amount": to_pence(r["amount"]) if r.get("amount") is not None else amount,
                "funds_split_id": r.get("funds_split_id", split_id), "request_id": r.get("request_id")}

    # Hotel onboarding: a connected account under the platform, activated in the sandbox, with card
    # capabilities (without them, intents tagged with the account fail "not enabled for payment acceptance").
    def onboard_account(self, name: str, email: str, request_id: str, account: str | None, step) -> bool:
        """Create (or resume) the connected account; step(status, info) reports progress. True once it
        accepts cards. Reuses the onboarding the Hotel Negotiation Desk verified against the sandbox."""
        from ..hotel_onboarding import open_account
        return open_account(self.c, name, email, request_id=request_id, account=account, step=step)

    def account_status(self, account: str) -> dict:
        from ..hotel_onboarding import accepts_cards
        a = self.c.get(f"/accounts/{account}")
        return {"id": a.get("id", account), "status": a.get("status"), "name": (a.get("account_details") or {})
                .get("business_details", {}).get("business_name"), "accepts_cards": accepts_cards(self.c, account)}

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
    default_account = "acct_sim_harbour_house"   # a stand-in connected account for the hotel

    def __init__(self, path: str | Path | None = None, refund_settle_seconds: float = 2.0):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.refund_settle_seconds = refund_settle_seconds
        self.knobs = {"timeout_next_create": False, "fail_next_refund": False,
                      "timeout_next_split": False, "fail_next_split": False}
        from ..storage import doc
        self.store = doc(self.path) if self.path else None
        self.db = (self.store.read() if self.store else None) or {"intents": {}, "refunds": {}, "requests": {}}
        self.db.setdefault("splits", {})
        self.db.setdefault("reversals", {})

    def _save(self):
        if self.store:
            self.store.write(self.db)

    def _dupe(self, request_id: str):
        if request_id in self.db["requests"]:
            raise AirwallexError(400, "duplicate_request_id", f"request_id {request_id} was already used",
                                 {"code": "duplicate_request_id"})

    def _maybe_timeout(self, request_id: str, method: str, path: str):
        if self.knobs.get("timeout_next_create"):
            self.knobs["timeout_next_create"] = False
            return True
        return False

    def create_intent(self, request_id, amount, currency, merchant_order_id, metadata, return_url=None,
                      connected_account_id=None):
        with self.lock:
            self._dupe(request_id)
            iid = "int_sim_" + uuid.uuid4().hex[:18]
            self.db["intents"][iid] = {"id": iid, "status": "REQUIRES_PAYMENT_METHOD", "amount": amount,
                                       "currency": currency, "merchant_order_id": merchant_order_id,
                                       "client_secret": "sim_secret_" + uuid.uuid4().hex[:12],
                                       "metadata": metadata, "captured_amount": 0, "request_id": request_id,
                                       "return_url": return_url, "connected_account_id": connected_account_id,
                                       "created": time.time()}
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

    def onboard_account(self, name, email, request_id, account, step) -> bool:
        if self._knob("fail_next_onboarding"):
            raise AirwallexError(400, "invalid_argument", "simulated onboarding refusal", {})
        acct = account or "acct_sim_" + uuid.uuid4().hex[:12]
        with self.lock:
            self.db.setdefault("accounts", {})[acct] = {"id": acct, "name": name, "status": "ACTIVE", "cards": True}
            self._save()
        if not account:
            step("account_created", {"connected_account": acct})
        step("active", {})
        return True

    def account_status(self, account):
        a = self.db.get("accounts", {}).get(account) or (
            {"id": account, "name": "Harbour House", "status": "ACTIVE", "cards": True}
            if account == self.default_account else None)
        if not a:
            raise AirwallexError(404, "resource_not_found", "no such connected account", {})
        return {"id": a["id"], "status": a["status"], "name": a["name"], "accepts_cards": a["cards"]}

    def _knob(self, name: str) -> bool:
        on = bool(self.knobs.get(name))
        self.knobs[name] = False
        return on

    def create_funds_split(self, request_id, intent_id, amount, destination, metadata):
        with self.lock:
            self._dupe(request_id)
            i = self.db["intents"].get(intent_id)
            if not i or i["status"] != "SUCCEEDED":
                raise AirwallexError(400, "invalid_status_for_operation", "funds split needs a SUCCEEDED intent", {})
            if not str(destination).startswith("acct_"):
                raise AirwallexError(400, "invalid_argument", "destination must be a connected account", {})
            split = sum(x["amount"] for x in self.db["splits"].values() if x["source_id"] == intent_id)
            if split + amount > i["captured_amount"]:
                raise AirwallexError(400, "amount_exceeded", "splits exceed the captured amount", {})
            if self._knob("fail_next_split"):
                raise AirwallexError(400, "connected_account_not_enabled", "simulated split refusal", {})
            sid = "fs_sim_" + uuid.uuid4().hex[:18]
            self.db["splits"][sid] = {"id": sid, "status": "RELEASED", "amount": amount, "currency": i["currency"],
                                      "destination": destination, "source_id": intent_id, "request_id": request_id,
                                      "metadata": metadata, "created": time.time()}
            self.db["requests"][request_id] = sid
            self._save()
            if self._knob("timeout_next_split"):
                raise AmbiguousResult("POST", "/pa/funds_splits/create", request_id, "simulated timeout")
            return dict(self.db["splits"][sid])

    def find_funds_splits(self, intent_id):
        with self.lock:
            return [dict(x) for x in self.db["splits"].values() if x["source_id"] == intent_id]

    def reverse_funds_split(self, request_id, split_id, amount, metadata):
        with self.lock:
            self._dupe(request_id)
            sp = self.db["splits"].get(split_id)
            if not sp:
                raise AirwallexError(404, "resource_not_found", "no such funds split", {})
            reversed_ = sum(x["amount"] for x in self.db["reversals"].values() if x["funds_split_id"] == split_id)
            if reversed_ + amount > sp["amount"]:
                raise AirwallexError(400, "amount_exceeded", "reversals exceed the split", {})
            rid = "fsr_sim_" + uuid.uuid4().hex[:18]
            self.db["reversals"][rid] = {"id": rid, "status": "SUCCEEDED", "amount": amount, "funds_split_id": split_id,
                                         "request_id": request_id, "metadata": metadata, "created": time.time()}
            self.db["requests"][request_id] = rid
            self._save()
            if self._knob("timeout_next_split"):
                raise AmbiguousResult("POST", "/pa/funds_split_reversals/create", request_id, "simulated timeout")
            return dict(self.db["reversals"][rid])

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
    return SimulatedProvider(var_dir / "groupsales_sim_provider.json")
