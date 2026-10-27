"""Onboard each hotel in data/hotels.json as an Airwallex connected account (sandbox).

    python scripts/onboard_hotels.py [--commission 0.12]

The platform (our Airwallex account) owns each card payment and tags it with the hotel's
connected account; after every capture a FundsSplit pays the hotel its share and the commission
stays with the platform (bookings.py). Hotels that already have an ACTIVE account are skipped.
Writes `platform.connected_account` and `platform.commission_pct` back to data/hotels.json.
All business and person details are synthetic. Hotels that sign up themselves (the /join page) go
through the same steps in agentbank/hotel_onboarding.py.
"""
import argparse
import json
import sys

from _bootstrap import ROOT, connect

from agentbank.airwallex import AirwallexError
from agentbank.hotel_onboarding import accepts_cards, open_account

HOTELS = ROOT / "data/hotels.json"


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
                acct = None   # unknown to Airwallex: open a new one
        try:
            ok = open_account(client, h["name"], "alex.rivera@example.com", account=acct,
                              step=lambda status, info: platform.update(info))
            if not ok:
                print(f"{h['name']}: {platform['connected_account']} card capabilities still pending; rerun later")
        except AirwallexError as e:
            print(f"{h['name']}: onboarding failed [{e.status}] {e.code}: {e.message}")
            return 1
        print(f"{h['name']}: onboarded {platform['connected_account']}, commission {platform['commission_pct']}")
        HOTELS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
