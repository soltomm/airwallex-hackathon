"""Create the synthetic beneficiaries in data/beneficiaries.json and print their ids.

Payloads use real routing identifiers with made-up account numbers: Airwallex validates
routing codes, sort codes and IBAN checksums at creation time.
"""
import json

from _bootstrap import ROOT, connect

client, api = connect()
for b in json.loads((ROOT / "data" / "beneficiaries.json").read_text()):
    created = api.create_beneficiary(b["beneficiary"], b["transfer_methods"], nickname=b["nickname"])
    print(f"{b['nickname']:<24} {created.get('beneficiary_id') or created.get('id')}")
