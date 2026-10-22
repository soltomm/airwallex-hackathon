"""Scripted buyer/seller negotiation over simulated Paris inventory (offline, no LLM).

The traveller wants 2 nights for 2 with breakfast, late checkout and free cancellation
for EUR 500. No published rate fits; the seller policy finds one that does.
Run: python scripts/negotiation_demo.py
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import _bootstrap  # noqa: F401
from _bootstrap import ROOT

from agentbank.audit import AuditLog
from agentbank.money import Money
from agentbank.offers import Cancellation, Negotiation, Offer, SellerPolicy, load_hotels
from agentbank.policy import Verdict

BUDGET = Money.of(500, "EUR")


def show(by, o, d):
    print(f"  {by:6} {o.total} | {', '.join(o.extras) or '-'} | {o.cancellation.value} "
          f"| deposit {o.deposit_pct:.0%} -> {d.verdict.value}: {'; '.join(d.public)}")


def main():
    hotels = load_hotels(ROOT / "data/hotels.json")
    audit = AuditLog(ROOT / "var/audit.jsonl")
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    ask = dict(check_in=date(2026, 11, 13), check_out=date(2026, 11, 15), guests=2,
               extras=("breakfast", "late_checkout"), cancellation=Cancellation.FREE_48H, expires_at=expires)

    for hotel in hotels.values():
        print(f"\n{hotel.name}")
        request = Offer(hotel.id, "DBL", deposit_pct=Decimal(0), total=BUDGET, **ask)
        print(f"  published price for these terms: {hotel.published_total(request)}")
        policy = SellerPolicy(hotel)
        n = Negotiation(policy, audit=audit)

        show("buyer", request, n.propose("buyer", request))
        counter = policy.counter(request)
        if counter is None:
            print("  seller has nothing to offer")
            continue
        show("seller", counter, n.propose("seller", counter))
        if counter.total <= BUDGET:
            print(f"  -> deal at {counter.total}")
            continue
        # Over budget: buyer trades a larger deposit hold for the price it can afford.
        retry = Offer(hotel.id, "DBL", deposit_pct=Decimal("0.6"), total=BUDGET,
                      **{**ask, "extras": counter.extras})
        d = n.propose("buyer", retry)
        show("buyer", retry, d)
        if d.verdict is Verdict.EXECUTE:
            print(f"  -> deal at {retry.total}, {retry.deposit} held on the card; next: traveller approval + payment")
        else:
            print("  -> no deal within budget")


if __name__ == "__main__":
    main()
