"""How much of each hotel's payout the platform holds back, decided in code from the hotel's record.

The platform owns every card payment, so if a hotel fails to deliver a stay (overbooking, closure)
the platform refunds the guest and must recover the hotel's share. A reserve, held as an Airwallex
funds split with auto_release off, covers that until the stay is delivered; the payout cycle then
releases it. New hotels carry the highest reserve; a hotel's own refund history raises it; an
incident freezes its reserves until the incident window passes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

HOTEL_FAILED = ("cancelled_by_hotel", "refunded_by_hotel")   # stays the hotel did not deliver
DELIVERED = ("checked_out", "no_show", "cancelled_fee_charged")   # the hotel earned what was captured


@dataclass(frozen=True)
class ReservePolicy:
    established: Decimal = Decimal("0.05")     # hotels with a track record
    new_hotel: Decimal = Decimal("0.20")       # fewer than `track_record` delivered stays
    track_record: int = 5
    refund_multiplier: Decimal = Decimal("2")  # reserve at least twice the hotel's failed-stay rate
    cap: Decimal = Decimal("0.50")
    incident_hold: timedelta = timedelta(days=30)

    def pct(self, hotel_id: str, bookings: dict[str, dict]) -> tuple[Decimal, str]:
        """(reserve fraction of the hotel's payout, why), from this hotel's bookings only."""
        mine = [b for b in bookings.values() if b["hotel_id"] == hotel_id]
        delivered = sum(b["status"] in DELIVERED for b in mine)
        failed = sum(b["status"] in HOTEL_FAILED for b in mine)
        settled = delivered + failed
        base = self.established if delivered >= self.track_record else self.new_hotel
        why = (f"established hotel ({delivered} stays delivered)" if delivered >= self.track_record
               else f"new hotel ({delivered} of {self.track_record} stays delivered)")
        rate = Decimal(failed) / settled if settled else Decimal(0)
        risk = (rate * self.refund_multiplier).quantize(Decimal("0.01"))
        if risk > base:
            base, why = risk, f"{failed} of {settled} stays not delivered ({rate:.0%}); reserve {self.refund_multiplier}x that"
        return min(base, self.cap), why

    def frozen(self, hotel_id: str, incidents: list[dict], now: datetime) -> dict | None:
        """The hotel's latest incident if its reserves must stay held."""
        recent = [i for i in incidents if i["hotel_id"] == hotel_id
                  and now - datetime.fromisoformat(i["at"]) < self.incident_hold]
        return max(recent, key=lambda i: i["at"]) if recent else None
