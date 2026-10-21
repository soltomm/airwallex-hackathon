"""Run PayeeCheck on the seeded suppliers plus a few deliberately bad payees.

    python scripts/fetch_sanctions.py     # once
    python scripts/payee_check_demo.py
"""
import copy
import json

from _bootstrap import ROOT, connect

from agentbank.payee_check import SanctionsList, check_payee

_, api = connect()
sdn = SanctionsList.load_ofac_sdn(ROOT / "data" / "sanctions" / "sdn.csv")
print(f"sanctions list: {len(sdn.entries):,} names\n")

seeded = {b["nickname"]: b for b in json.loads((ROOT / "data" / "beneficiaries.json").read_text())}
cases = [(name, b) for name, b in seeded.items()]


def variant(base, label, **changes):
    b = copy.deepcopy(seeded[base])
    for path, value in changes.items():
        node = b["beneficiary"]
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    cases.append((label, b))


variant("rhein-logistik-de", "DE, IBAN with a typo", bank_details__iban="DE89370400440532013001")
variant("acme-freight-us", "US, wrong routing number", bank_details__account_routing_value1="123456789")
variant("rhein-logistik-de", "DE, SWIFT code for another country", bank_details__swift_code="BARCGB22XXX")
variant("acme-freight-us", "US, payee on the SDN list",
        company_name="Aerocaribbean Airlines", bank_details__account_name="Aerocaribbean Airlines")
variant("acme-freight-us", "US, name close to an SDN entry",
        company_name="Aero Caribean Airline Ltd", bank_details__account_name="Aero Caribean Airline Ltd")

for label, b in cases:
    r = check_payee(api, sdn, b["beneficiary"], b["transfer_methods"])
    print(f"{label:<36} {r.summary()}")
