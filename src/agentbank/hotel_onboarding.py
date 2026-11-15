"""A hotel's Airwallex connected account: created through the Accounts API (native onboarding),
activated in the sandbox and given card capabilities, so deposits can be collected for it and its
share paid by funds split.

Sandbox: beyond the hotel's name and email, business and person details are synthetic and the
account is activated by simulation. In production the hotel would complete KYC in Airwallex's
embedded onboarding component (or the hosted flow) instead.
"""
from __future__ import annotations

import time
import unicodedata
import uuid
from typing import Callable

from .airwallex import AirwallexError

INDUSTRY = "ICCV3_1102XX"   # Travel and lodging / Hotels, inns, and motels
CARD_CAPABILITIES = ("payments_visa", "payments_mastercard")
# The sandbox accepts this US company template; only names change per hotel.
ADDRESS = {"address_line1": "1 Market St", "suburb": "San Francisco", "state": "CA", "postcode": "94105",
           "country_code": "US"}


def ascii_name(name: str) -> str:
    """Store names accept Latin letters without accents ("Hôtel" -> "Hotel")."""
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()


# ---- Airwallex connected account (native API) ---------------------------------
def account_details(name: str, email: str = "alex.rivera@example.com") -> dict:
    return {
        # Card acceptance (payments_visa/mastercard capabilities) needs a store; without one the
        # account is ACTIVE but PaymentIntents tagged with it fail "not enabled for payment acceptance".
        "store_details": {
            "store_name": ascii_name(name), "store_description": "Hotel rooms, breakfast and late checkout",
            "industry_code": INDUSTRY, "mcc": "7011", "operating_models": ["ONLINE_CHECKOUT"],
            "payment_distribution": [{"payment_type": "CARDS", "percentage": "100"}],
            "estimated_transaction_volume": {"currency": "USD", "average_amount_per_transaction": "500",
                                             "max_amount_per_transaction": "5000", "monthly_transaction_amount": "50000"},
            "cross_border_transaction_percent": "60", "dispute_percent": "0.5", "refund_percent": "5",
            "fulfillment_days": 30, "store_address": ADDRESS, "store_websites": [{"url": "https://example.com"}],
        },
        "business_details": {
            "business_name": name, "business_structure": "COMPANY",
            "business_identifiers": [{"country_code": "US", "number": "12-3456789", "type": "EIN"}],
            "industry_category_code": INDUSTRY, "business_address": ADDRESS, "registration_address": ADDRESS,
            "operating_country": ["US"], "url": "https://example.com",
            "description_of_goods_or_services": "Hotel rooms and stays",
            "contact_number": "4155550100", "state_of_incorporation": "DE", "business_start_date": "2020-01-15",
            "account_usage": {"estimated_monthly_revenue": {"amount": "50000", "currency": "USD"},
                              "product_reference": ["RECEIVE_TRANSFERS", "MAKE_TRANSFERS"]},
        },
        "business_person_details": [{
            "first_name": "Alex", "last_name": "Rivera", "date_of_birth": "1985-04-12",
            "email": email, "nationality": "US",
            "roles": ["DIRECTOR", "AUTHORISED_PERSON", "BENEFICIAL_OWNER"], "residential_address": ADDRESS,
            "identifications": {"primary": {"identification_type": "PASSPORT", "issuing_country_code": "US",
                                            "passport": {"number": "X12345678", "expire_at": "2034-01-01"}}},
        }],
    }


def accepts_cards(client, acct: str) -> bool:
    return client.get("/account_capabilities/payments_visa", on_behalf_of=acct).get("status") in ("ENABLED", "ACTIVE")


def open_account(client, name: str, email: str, request_id: str | None = None, account: str | None = None,
                 step: Callable[[str, dict], None] = lambda status, info: None, sleep=time.sleep) -> bool:
    """Create (or resume) a hotel's connected account, activate it in the sandbox and enable card
    capabilities. Reports progress through step(status, info); True once it accepts cards."""
    if not account:
        account = client.post("/accounts/create", {
            "request_id": request_id or str(uuid.uuid4()), "account_details": account_details(name, email),
            "customer_agreements": {"agreed_to_data_usage": True, "agreed_to_terms_and_conditions": True},
            "primary_contact": {"email": email}})["id"]
        step("account_created", {"connected_account": account})
    status = client.get(f"/accounts/{account}").get("status")
    if status != "ACTIVE":
        if status in (None, "CREATED", "ACTION_REQUIRED"):
            client.post(f"/accounts/{account}/submit", {})
            step("submitted", {})
            sleep(2)
        client.post(f"/simulation/accounts/{account}/update_status", {"next_status": "ACTIVE", "force": True})
        for _ in range(10):
            if client.get(f"/accounts/{account}").get("status") == "ACTIVE":
                break
            sleep(2)
        else:
            raise AirwallexError(409, "not_active", f"account {account} did not become ACTIVE", {})
        step("active", {})
        sleep(2)   # enabling capabilities right after activation fails validation
    if accepts_cards(client, account):
        return True
    for cap in CARD_CAPABILITIES:
        client.post(f"/account_capabilities/{cap}/enable", {"id": cap}, on_behalf_of=account)
    step("capabilities_requested", {})
    for _ in range(30):   # PENDING -> ENABLED is reviewed asynchronously, even in the sandbox
        if accepts_cards(client, account):
            return True
        sleep(5)
    return False
