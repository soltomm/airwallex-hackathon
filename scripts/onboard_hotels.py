"""Onboard each hotel in data/hotels.json as an Airwallex connected account (sandbox).

    python scripts/onboard_hotels.py [--commission 0.12]

The platform (our Airwallex account) owns each card payment and tags it with the hotel's
connected account; after every capture a FundsSplit pays the hotel its share and the commission
stays with the platform (bookings.py). Hotels that already have an ACTIVE account are skipped.
Writes `platform.connected_account` and `platform.commission_pct` back to data/hotels.json.
All business and person details are synthetic.
"""
import argparse
import json
import sys
import time
import unicodedata
import uuid

from _bootstrap import ROOT, connect

from agentbank.airwallex import AirwallexError

HOTELS = ROOT / "data/hotels.json"
INDUSTRY = "ICCV3_1102XX"   # Travel and lodging / Hotels, inns, and motels
CARD_CAPABILITIES = ("payments_visa", "payments_mastercard")

# The sandbox accepts this US company template (scripts/platform_test.py); only names change per hotel.
ADDRESS = {"address_line1": "1 Market St", "suburb": "San Francisco", "state": "CA", "postcode": "94105",
           "country_code": "US"}


def ascii_name(name: str) -> str:
    """Store names accept Latin letters without accents ("Hôtel" -> "Hotel")."""
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()


def account_details(name: str) -> dict:
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
            "email": "alex.rivera@example.com", "nationality": "US",
            "roles": ["DIRECTOR", "AUTHORISED_PERSON", "BENEFICIAL_OWNER"], "residential_address": ADDRESS,
            "identifications": {"primary": {"identification_type": "PASSPORT", "issuing_country_code": "US",
                                            "passport": {"number": "X12345678", "expire_at": "2034-01-01"}}},
        }],
    }


def onboard(client, name: str) -> str:
    acct = client.post("/accounts/create", {
        "request_id": str(uuid.uuid4()), "account_details": account_details(name),
        "customer_agreements": {"agreed_to_data_usage": True, "agreed_to_terms_and_conditions": True},
        "primary_contact": {"email": "alex.rivera@example.com"}})["id"]
    client.post(f"/accounts/{acct}/submit", {})
    time.sleep(2)
    client.post(f"/simulation/accounts/{acct}/update_status", {"next_status": "ACTIVE", "force": True})
    for _ in range(10):
        if client.get(f"/accounts/{acct}").get("status") == "ACTIVE":
            break
        time.sleep(2)
    else:
        raise SystemExit(f"{name}: account {acct} did not become ACTIVE")
    time.sleep(2)   # enabling capabilities right after activation fails validation
    for cap in CARD_CAPABILITIES:
        client.post(f"/account_capabilities/{cap}/enable", {"id": cap}, on_behalf_of=acct)
    for _ in range(30):   # PENDING -> ENABLED is reviewed asynchronously, even in the sandbox
        if accepts_cards(client, acct):
            return acct
        time.sleep(5)
    print(f"{name}: {acct} card capabilities still pending; rerun this script later to check")
    return acct


def accepts_cards(client, acct: str) -> bool:
    return client.get("/account_capabilities/payments_visa", on_behalf_of=acct).get("status") in ("ENABLED", "ACTIVE")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commission", default="0.12", help="platform commission as a fraction (default 0.12)")
    args = ap.parse_args()
    client, _ = connect()
    data = json.loads(HOTELS.read_text())
    for h in data:
        platform = h.setdefault("platform", {})
        platform.setdefault("commission_pct", args.commission)
        acct = platform.get("connected_account")
        if acct:
            try:
                if client.get(f"/accounts/{acct}").get("status") == "ACTIVE" and accepts_cards(client, acct):
                    print(f"{h['name']}: {acct} already ACTIVE and accepting cards")
                    continue
            except AirwallexError:
                pass
        try:
            platform["connected_account"] = onboard(client, h["name"])
        except AirwallexError as e:
            print(f"{h['name']}: onboarding failed [{e.status}] {e.code}: {e.message}")
            return 1
        print(f"{h['name']}: onboarded {platform['connected_account']}, commission {platform['commission_pct']}")
        HOTELS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
