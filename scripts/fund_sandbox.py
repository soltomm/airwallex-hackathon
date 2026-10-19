"""Simulate a deposit into a Global Account in the given currency (default USD).

    python scripts/fund_sandbox.py 25000 [USD] [payer name]

Reuses an existing Global Account in that currency; creates one only if none exists.
"""
import sys
import time
import uuid

from _bootstrap import connect

from agentbank.simulation import SandboxSimulator

EURO_COUNTRY = "NL"  # country_code is the account's country, not its currency

amount = float(sys.argv[1]) if len(sys.argv) > 1 else 25000
currency = (sys.argv[2] if len(sys.argv) > 2 else "USD").upper()
payer = sys.argv[3] if len(sys.argv) > 3 else "Seed funding"

client, api = connect()
match = [a for a in api.global_accounts()
         if a["status"] == "ACTIVE" and any(f["currency"] == currency for f in a.get("required_features", []))]
if match:
    ga = match[0]
    print(f"using global account {ga['id']}")
else:
    country = {"USD": "US", "GBP": "GB", "EUR": EURO_COUNTRY}.get(currency)
    if not country:
        raise SystemExit(f"no Global Account in {currency}; fund USD and convert instead")
    ga = api.create_global_account(str(uuid.uuid4()), country, currency)
    print(f"created global account {ga['id']} ({ga.get('status')})")

before = api.available(currency)
dep = SandboxSimulator(client).deposit(ga["id"], amount, payer)
print(f"deposit {dep.get('id')}: {dep.get('status')}")
# The response says PENDING; the balance follows within a few seconds (not always instantly).
after = before
for _ in range(10):
    after = api.available(currency)
    if after != before:
        break
    time.sleep(1)
print(f"{currency} available: {before} -> {after}")
