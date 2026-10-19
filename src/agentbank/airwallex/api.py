"""Thin, typed wrappers over the endpoints the starter kits use.

Paths are relative to https://api.sandbox.airwallex.com/api/v1. Use the docs MCP server
for exact payload fields; bodies here are passed through so you can add what you need.
Every money-moving call takes an explicit request_id: get it from OperationLedger.
"""
from __future__ import annotations

from ..money import Money
from .client import AirwallexClient


class AirwallexAPI:
    def __init__(self, client: AirwallexClient):
        self.c = client

    # ---- wallet -------------------------------------------------------------
    def balances(self, *, on_behalf_of: str | None = None) -> dict[str, dict]:
        rows = self.c.get("/balances/current", on_behalf_of=on_behalf_of)
        return {row["currency"]: row for row in rows}

    def available(self, currency: str, *, on_behalf_of: str | None = None) -> Money:
        row = self.balances(on_behalf_of=on_behalf_of).get(currency.upper(), {})
        return Money.of(row.get("available_amount", 0), currency)

    def global_accounts(self, *, on_behalf_of: str | None = None) -> list[dict]:
        return self.c.get("/global_accounts", on_behalf_of=on_behalf_of).get("items", [])

    def create_global_account(self, request_id: str, country_code: str, currency: str, **kw) -> dict:
        body = {"request_id": request_id, "country_code": country_code,
                "required_features": [{"currency": currency, "transfer_method": "LOCAL"}]}
        return self.c.post("/global_accounts/create", body, **kw)

    def global_account_transactions(self, global_account_id: str, **kw) -> list[dict]:
        return self.c.get(f"/global_accounts/{global_account_id}/transactions", **kw).get("items", [])

    # ---- FX (no MCP tool books conversions; leave x-api-version off) ---------
    def fx_rate(self, sell_currency: str, buy_currency: str, buy_amount: Money | None = None, **kw) -> dict:
        params = {"sell_currency": sell_currency, "buy_currency": buy_currency,
                  "buy_amount": buy_amount.api() if buy_amount else None}
        return self.c.get("/fx/rates/current", params=params, **kw)  # indicative: read `rate`

    def fx_quote(self, sell_currency: str, buy: Money, validity: str = "MIN_1", **kw) -> dict:
        body = {"sell_currency": sell_currency, "buy_currency": buy.currency,
                "buy_amount": buy.api(), "validity": validity}
        return self.c.post("/fx/quotes/create", body, **kw)  # firm: read `client_rate`, single use

    def fx_convert(self, request_id: str, sell_currency: str, buy: Money, quote_id: str | None = None, **kw) -> dict:
        body = {"request_id": request_id, "sell_currency": sell_currency,
                "buy_currency": buy.currency, "buy_amount": buy.api()}
        if quote_id:
            body["quote_id"] = quote_id
        return self.c.post("/fx/conversions/create", body, **kw)  # settles immediately

    # ---- beneficiaries & transfers -----------------------------------------
    def beneficiary_schema(self, bank_country_code: str, account_currency: str,
                           payment_method: str = "LOCAL", entity_type: str = "COMPANY") -> dict:
        return self.c.post("/beneficiaries/schema", {
            "bank_country_code": bank_country_code, "account_currency": account_currency,
            "payment_method": payment_method, "entity_type": entity_type})

    def validate_beneficiary(self, beneficiary: dict, transfer_methods: list[str], **kw) -> dict:
        return self.c.post("/beneficiaries/validate",
                           {"beneficiary": beneficiary, "transfer_methods": transfer_methods}, **kw)

    def create_beneficiary(self, beneficiary: dict, transfer_methods: list[str], nickname: str | None = None, **kw) -> dict:
        body = {"beneficiary": beneficiary, "transfer_methods": transfer_methods}
        if nickname:
            body["nickname"] = nickname
        return self.c.post("/beneficiaries/create", body, **kw)

    def create_transfer(self, request_id: str, beneficiary_id: str, amount: Money, *, source_currency: str,
                        transfer_method: str, reference: str, reason: str = "payment_to_supplier", **kw) -> dict:
        if transfer_method not in ("LOCAL", "SWIFT"):
            raise ValueError("transfer_method must be LOCAL or SWIFT")
        return self.c.post("/transfers/create", {
            "request_id": request_id, "beneficiary_id": beneficiary_id,
            "source_currency": source_currency, "transfer_currency": amount.currency,
            "transfer_amount": amount.api(), "transfer_method": transfer_method,
            "reference": reference, "reason": reason}, **kw)

    def get_transfer(self, transfer_id: str, **kw) -> dict:
        return self.c.get(f"/transfers/{transfer_id}", **kw)

    def find_transfer_by_request_id(self, request_id: str, **kw) -> dict | None:
        items = self.c.get("/transfers", params={"request_id": request_id}, **kw).get("items", [])
        return items[0] if items else None

    # ---- issuing --------------------------------------------------------------
    def create_card(self, body: dict, **kw) -> dict:
        return self.c.post("/issuing/cards/create", body, **kw)

    def set_card_status(self, card_id: str, status: str) -> dict:
        """ACTIVE, INACTIVE (freeze) or CLOSED. Use it to revoke an agent's authority mid-run."""
        return self.c.post(f"/issuing/cards/{card_id}/update", {"card_status": status})

    def card_limits(self, card_id: str) -> dict:
        return self.c.get(f"/issuing/cards/{card_id}/limits")

    def issuing_transactions(self, **params) -> list[dict]:
        return self.c.get("/issuing/transactions", params=params).get("items", [])

    # ---- payments & disputes ------------------------------------------------
    def create_payment_intent(self, request_id: str, amount: Money, merchant_order_id: str) -> dict:
        return self.c.post("/pa/payment_intents/create", {
            "request_id": request_id, "amount": amount.api(), "currency": amount.currency,
            "merchant_order_id": merchant_order_id})

    def disputes(self, **params) -> list[dict]:
        return self.c.get("/pa/payment_disputes", params=params).get("items", [])

    def accept_dispute(self, dispute_id: str, body: dict) -> dict:
        return self.c.post(f"/pa/payment_disputes/{dispute_id}/accept", body)

    def challenge_dispute(self, dispute_id: str, body: dict) -> dict:
        return self.c.post(f"/pa/payment_disputes/{dispute_id}/challenge", body)
    # Kit 4 evidence uploads go to files.sandbox.airwallex.com (config.files_base) as JPG or PDF.

    # ---- platform (kits 5-8; needs connected accounts + platform payments) ---
    def connected_account_transfer(self, request_id: str, destination: str, amount: Money,
                                   reason: str, reference: str) -> dict:
        return self.c.post("/connected_account_transfers/create", {
            "request_id": request_id, "destination": destination, "amount": amount.api(),
            "currency": amount.currency, "reason": reason, "reference": reference})

    def charge(self, request_id: str, source: str, amount: Money, reason: str, reference: str) -> dict:
        return self.c.post("/charges/create", {
            "request_id": request_id, "source": source, "amount": amount.api(),
            "currency": amount.currency, "reason": reason, "reference": reference})
