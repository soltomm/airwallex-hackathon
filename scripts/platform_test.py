"""Kits 5-8 smoke test: create and activate a connected account, act on its behalf,
move platform money to it, collect a fee, and pull a balance report.

    python scripts/platform_test.py            # creates a new connected account
    python scripts/platform_test.py acct_...   # reuses an ACTIVE one

All data is synthetic. Steps print PASS/FAIL and keep going where they can.
"""
import json
import sys
import time
import uuid

from _bootstrap import connect

from agentbank.airwallex import AirwallexError
from agentbank.money import Money
from agentbank.simulation import SandboxSimulator

client, api = connect()
sim = SandboxSimulator(client)


def step(label, fn):
    try:
        out = fn()
        print(f"PASS {label}: {json.dumps(out, default=str)[:300]}")
        return out
    except AirwallexError as e:
        print(f"FAIL {label}: [{e.status}] {e.code}: {e.message} {json.dumps(e.body)[:400]}")
    return None


def rid():
    return str(uuid.uuid4())


def pick(d, *keys):
    return {k: d.get(k) for k in keys} if isinstance(d, dict) else d


def account_details(industry_code):
    address = {"address_line1": "1 Market St", "suburb": "San Francisco", "state": "CA",
               "postcode": "94105", "country_code": "US"}
    return {
        "business_details": {
            "business_name": "Sandbox Business",
            "business_structure": "COMPANY",
            "business_identifiers": [{"country_code": "US", "number": "12-3456789", "type": "EIN"}],
            "industry_category_code": industry_code,
            "business_address": address,
            "registration_address": address,
            "operating_country": ["US"],
            "url": "https://example.com",
            "description_of_goods_or_services": "B2B software consulting",  # required by submit
            "contact_number": "4155550100",
            "state_of_incorporation": "DE",
            "business_start_date": "2020-01-15",
            "account_usage": {"estimated_monthly_revenue": {"amount": "50000", "currency": "USD"},
                              "product_reference": ["RECEIVE_TRANSFERS", "MAKE_TRANSFERS"]},
        },
        "business_person_details": [{
            "first_name": "Alex", "last_name": "Rivera", "date_of_birth": "1985-04-12",
            "email": "alex.rivera@example.com", "nationality": "US",
            "roles": ["DIRECTOR", "AUTHORISED_PERSON", "BENEFICIAL_OWNER"],
            "residential_address": address,
            "identifications": {"primary": {"identification_type": "PASSPORT", "issuing_country_code": "US",
                                            "passport": {"number": "X12345678", "expire_at": "2034-01-01"}}},
        }],
    }


acct = sys.argv[1] if len(sys.argv) > 1 else None
if not acct:
    cats = step("industry categories", lambda: client.get("/reference/industry_categories"))
    # A tree of {"type": "LIST", "values": [...]} groups ending in {"type": "CODE", "value": "ICCV3_..."}.
    def codes(nodes):
        for n in nodes or []:
            if n.get("type") == "CODE":
                yield n["value"]
            yield from codes(n.get("values"))
    code = next(codes(cats if isinstance(cats, list) else (cats or {}).get("items")), None)
    print(f"     using industry code {code}")
    created = step("create connected account", lambda: client.post("/accounts/create", {
        "request_id": rid(), "account_details": account_details(code),
        "customer_agreements": {"agreed_to_data_usage": True, "agreed_to_terms_and_conditions": True},
        "primary_contact": {"email": "alex.rivera@example.com"}}))
    if not created:
        sys.exit(1)
    acct = created["id"]
    step("submit", lambda: client.post(f"/accounts/{acct}/submit", {}))
    time.sleep(2)
    step("simulate activation", lambda: client.post(f"/simulation/accounts/{acct}/update_status",
                                                    {"next_status": "ACTIVE", "force": True}))

info = step("account status", lambda: pick(client.get(f"/accounts/{acct}"), "id", "status"))
print(f"     connected account: {acct}")

# ---- act on behalf of the customer ------------------------------------------
ga = step("create customer Global Account (on behalf)",
          lambda: api.create_global_account(rid(), "US", "USD", on_behalf_of=acct))
if ga:
    step("simulate customer deposit 2,000 USD (on behalf)",
         lambda: pick(sim.deposit(ga["id"], 2000, "Customer revenue", on_behalf_of=acct), "id", "status"))
time.sleep(3)
step("customer USD balance (on behalf)", lambda: str(api.available("USD", on_behalf_of=acct)))

# ---- platform money movement ---------------------------------------------------
def poll(path, label):
    for _ in range(10):
        obj = client.get(path)
        if obj.get("status") in ("SETTLED", "FAILED", "CANCELLED"):
            return pick(obj, "id", "status", "amount")
        time.sleep(2)
    return pick(obj, "id", "status", "amount")

t = step("platform -> customer transfer 500 USD",
         lambda: pick(api.connected_account_transfer(rid(), acct, Money.of(500, "USD"), "wages_salary", "bridge advance"),
                      "id", "status"))
if t:
    step("  ... settles", lambda: poll(f"/connected_account_transfers/{t['id']}", "transfer"))
c = step("customer -> platform charge 25 USD",
         lambda: pick(api.charge(rid(), acct, Money.of(25, "USD"), "professional_business_services", "platform fee"),
                      "id", "status"))
if c:
    step("  ... settles", lambda: poll(f"/charges/{c['id']}", "charge"))
step("customer USD balance after", lambda: str(api.available("USD", on_behalf_of=acct)))

# ---- FX on behalf (may say unconfigured_client_fee for a few minutes after activation)
step("FX quote on behalf (100 EUR)", lambda: pick(api.fx_quote("USD", Money.of(100, "EUR"), on_behalf_of=acct),
                                                    "quote_id", "client_rate"))

# ---- reporting ------------------------------------------------------------------
step("platform balance report", lambda: pick(client.post("/platform_reports/create",
                                                          {"type": "BALANCE_REPORT", "file_format": "CSV"}), "id", "status"))
