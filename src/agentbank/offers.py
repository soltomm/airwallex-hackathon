"""Hotel offers and the seller's commercial policy.

An Offer is the structured object buyer and seller agents exchange: room, dates, extras,
cancellation terms, deposit hold and total price. Agents propose offers; SellerPolicy
decides whether the hotel can commit to one. The hotel's floor price never leaves this
module: buyers only see public reasons, the audit log gets the numbers.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

from .approvals import ApprovalRequest
from .money import Money
from .policy import Verdict


class Cancellation(str, Enum):
    NON_REFUNDABLE = "non_refundable"
    FREE_48H = "free_until_48h"   # free until 48h before check-in
    FREE_24H = "free_until_24h"


EXTRAS = ("breakfast", "late_checkout")
OFFER_TTL = timedelta(minutes=15)   # how long a committed offer stays bookable


@dataclass(frozen=True)
class Offer:
    hotel_id: str
    room_code: str
    check_in: date
    check_out: date
    guests: int
    extras: tuple[str, ...]
    cancellation: Cancellation
    deposit_pct: Decimal            # share of total held on the card at booking (0..1)
    total: Money
    expires_at: datetime

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def deposit(self) -> Money:
        # Non-refundable offers are charged in full at booking.
        pct = Decimal(1) if self.cancellation is Cancellation.NON_REFUNDABLE else self.deposit_pct
        return Money.of(self.total.amount * pct, self.total.currency)

    def terms(self) -> dict:
        return {
            "hotel_id": self.hotel_id, "room": self.room_code,
            "check_in": self.check_in.isoformat(), "check_out": self.check_out.isoformat(),
            "guests": self.guests, "extras": sorted(self.extras),
            "cancellation": self.cancellation.value, "deposit": str(self.deposit),
            "expires_at": self.expires_at.isoformat(),
        }

    def to_approval(self, hotel_name: str) -> ApprovalRequest:
        """What the traveller approves. Any change to price or terms needs a new approval."""
        extras = ", ".join(sorted(self.extras)) or "no extras"
        return ApprovalRequest(
            action="hotel_booking", amount=self.total, counterparty=self.hotel_id,
            summary=f"{hotel_name}, {self.nights} nights, {self.room_code}, {extras}, "
                    f"{self.cancellation.value}, deposit {self.deposit}",
            terms=self.terms())


@dataclass(frozen=True)
class RoomType:
    code: str
    name: str
    max_guests: int
    total_rooms: int
    published_rate: Money           # per night
    floor_rate: Money               # per night; lowest the hotel will go at normal demand


@dataclass
class Hotel:
    """Simulated inventory and commercial settings for one property."""
    id: str
    name: str
    currency: str
    rooms: dict[str, RoomType]
    booked: dict[tuple[str, date], int] = field(default_factory=dict)   # (room, night) -> rooms sold
    breakfast_cost: Decimal = Decimal("12")     # per guest per night, hotel's cost
    breakfast_price: Decimal = Decimal("25")    # published add-on price
    late_checkout_price: Decimal = Decimal("40")
    high_occupancy: Decimal = Decimal("0.85")   # at or above: no discounting, no late checkout
    risk_premium: dict[Cancellation, Decimal] = field(default_factory=lambda: {
        Cancellation.NON_REFUNDABLE: Decimal("0"),
        Cancellation.FREE_48H: Decimal("0.10"),
        Cancellation.FREE_24H: Decimal("0.15"),
    })
    min_deposit: dict[Cancellation, Decimal] = field(default_factory=lambda: {
        Cancellation.NON_REFUNDABLE: Decimal("1"),
        Cancellation.FREE_48H: Decimal("0.20"),
        Cancellation.FREE_24H: Decimal("0.30"),
    })
    approval_band: Decimal = Decimal("0.05")    # within 5% above floor, a revenue manager signs off
    negotiation_style: str = ""                 # free-text guidance for the seller agent
    metal_payee: str = ""                       # address Metal deposits are paid to
    usdz_per_eur: Decimal = Decimal("0.001")    # demo testnet rate: faucet amounts are small

    @classmethod
    def from_dict(cls, d: dict) -> "Hotel":
        cur = d["currency"]
        rooms = {r["code"]: RoomType(r["code"], r["name"], r["max_guests"], r["total_rooms"],
                                     Money.of(r["published_rate"], cur), Money.of(r["floor_rate"], cur))
                 for r in d["rooms"]}
        booked = {(b["room"], date.fromisoformat(b["night"])): b["sold"] for b in d.get("booked", [])}
        rules = dict(d.get("rules", {}))
        style = rules.pop("negotiation_style", "")
        kw = {k: Decimal(v) for k, v in rules.items() if k not in ("risk_premium", "min_deposit")}
        for k in ("risk_premium", "min_deposit"):
            if k in rules:
                kw[k] = {Cancellation(c): Decimal(v) for c, v in rules[k].items()}
        metal = d.get("metal", {})
        if "payee" in metal:
            kw["metal_payee"] = metal["payee"]
        if "usdz_per_eur" in metal:
            kw["usdz_per_eur"] = Decimal(metal["usdz_per_eur"])
        return cls(d["id"], d["name"], cur, rooms, booked, negotiation_style=style, **kw)

    def rules(self) -> dict:
        """The commercial rules as the seller agent sees them."""
        return {"currency": self.currency, "negotiation_style": self.negotiation_style,
                "breakfast_cost_per_guest_night": str(self.breakfast_cost),
                "breakfast_list_price_per_guest_night": str(self.breakfast_price),
                "late_checkout_list_price": str(self.late_checkout_price),
                "high_occupancy_threshold": str(self.high_occupancy),
                "autonomy_band_above_floor": str(self.approval_band),
                "cancellation_risk_premium": {c.value: str(v) for c, v in self.risk_premium.items()},
                "min_deposit_hold": {c.value: str(v) for c, v in self.min_deposit.items()},
                "rooms": {r.code: {"name": r.name, "max_guests": r.max_guests, "total_rooms": r.total_rooms,
                                   "published_rate": str(r.published_rate.amount),
                                   "floor_rate": str(r.floor_rate.amount)} for r in self.rooms.values()}}

    def occupancy(self, room_code: str, night: date) -> Decimal:
        room = self.rooms[room_code]
        return Decimal(self.booked.get((room_code, night), 0)) / room.total_rooms

    def available(self, room_code: str, night: date) -> bool:
        return self.booked.get((room_code, night), 0) < self.rooms[room_code].total_rooms

    def nights(self, offer: Offer) -> list[date]:
        return [offer.check_in + timedelta(days=i) for i in range(offer.nights)]

    def published_total(self, offer: Offer) -> Money:
        """List price for these terms: the anchor the seller negotiates down from."""
        room = self.rooms[offer.room_code]
        amount = room.published_rate.amount * offer.nights
        if "breakfast" in offer.extras:
            amount += self.breakfast_price * offer.guests * offer.nights
        if "late_checkout" in offer.extras:
            amount += self.late_checkout_price
        amount *= 1 + self.risk_premium[offer.cancellation]
        return Money.of(amount, self.currency)

    def floor_total(self, offer: Offer) -> Money:
        """Lowest total the hotel accepts. Busy nights are sold at the published rate;
        refundable terms carry a risk premium that a larger deposit hold reduces."""
        room = self.rooms[offer.room_code]
        rooms = sum(room.published_rate.amount if self.occupancy(offer.room_code, n) >= self.high_occupancy
                    else room.floor_rate.amount for n in self.nights(offer))
        extras = self.breakfast_cost * offer.guests * offer.nights if "breakfast" in offer.extras else Decimal(0)
        premium = self.risk_premium[offer.cancellation] * (1 - offer.deposit_pct)
        if offer.cancellation is Cancellation.NON_REFUNDABLE:
            premium = Decimal(0)
        return Money.of(rooms * (1 + premium) + extras, self.currency)


@dataclass(frozen=True)
class SellerDecision:
    verdict: Verdict
    public: tuple[str, ...]          # safe to tell the buyer agent
    internal: tuple[str, ...] = ()   # numbers for the hotel and the audit log only

    @property
    def allowed(self) -> bool:
        return self.verdict is not Verdict.REJECT


class SellerPolicy:
    """EXECUTE: the seller agent may commit. NEEDS_APPROVAL: a revenue manager signs off.
    REJECT: the hotel cannot sell these terms; the public reasons say what to change."""

    def __init__(self, hotel: Hotel, now=lambda: datetime.now(timezone.utc)):
        self.hotel = hotel
        self.now = now

    def evaluate(self, offer: Offer) -> SellerDecision:
        h = self.hotel
        if offer.hotel_id != h.id or offer.room_code not in h.rooms:
            return SellerDecision(Verdict.REJECT, ("unknown hotel or room",))
        if offer.total.currency != h.currency:
            return SellerDecision(Verdict.REJECT, (f"prices are in {h.currency}",))
        if offer.nights < 1:
            return SellerDecision(Verdict.REJECT, ("check-out must be after check-in",))
        if offer.expires_at <= self.now():
            return SellerDecision(Verdict.REJECT, ("offer expired",))
        room = h.rooms[offer.room_code]
        if offer.guests < 1 or offer.guests > room.max_guests:
            return SellerDecision(Verdict.REJECT, (f"{room.name} sleeps up to {room.max_guests}",))
        unknown = set(offer.extras) - set(EXTRAS)
        if unknown:
            return SellerDecision(Verdict.REJECT, (f"extras not offered: {', '.join(sorted(unknown))}",))
        full = [n for n in h.nights(offer) if not h.available(offer.room_code, n)]
        if full:
            return SellerDecision(Verdict.REJECT, (f"no {room.name} left on {', '.join(map(str, full))}",))
        if "late_checkout" in offer.extras and h.occupancy(offer.room_code, offer.check_out) >= h.high_occupancy:
            return SellerDecision(Verdict.REJECT, ("late checkout unavailable: the room is needed that day",))
        if not Decimal(0) <= offer.deposit_pct <= 1:
            return SellerDecision(Verdict.REJECT, ("deposit must be between 0% and 100%",))
        min_dep = h.min_deposit[offer.cancellation]
        if offer.cancellation is not Cancellation.NON_REFUNDABLE and offer.deposit_pct < min_dep:
            return SellerDecision(Verdict.REJECT, (
                f"{offer.cancellation.value} needs a deposit hold of at least {min_dep:.0%}",))

        floor = h.floor_total(offer)
        auto = Money.of(floor.amount * (1 + h.approval_band), h.currency)
        internal = (f"floor {floor}", f"autonomous floor {auto}", f"published {h.published_total(offer)}")
        if offer.total < floor:
            hint = ("price too low for these terms; a larger deposit or fewer extras would help"
                    if offer.cancellation is not Cancellation.NON_REFUNDABLE
                    else "price too low for these terms")
            return SellerDecision(Verdict.REJECT, (hint,), internal)
        if offer.total < auto:
            return SellerDecision(Verdict.NEEDS_APPROVAL, ("needs hotel sign-off",), internal)
        return SellerDecision(Verdict.EXECUTE, ("hotel can commit",), internal)

    def counter(self, offer: Offer) -> Offer | None:
        """The closest offer the seller agent can commit to alone, or None if none exists.
        Drops late checkout on busy days, raises the deposit to the minimum, then lifts the
        price to the autonomous floor. Never lowers a price the buyer already offered."""
        h = self.hotel
        o = offer
        if "late_checkout" in o.extras and h.occupancy(o.room_code, o.check_out) >= h.high_occupancy:
            o = replace(o, extras=tuple(e for e in o.extras if e != "late_checkout"))
        if o.cancellation is not Cancellation.NON_REFUNDABLE:
            o = replace(o, deposit_pct=max(o.deposit_pct, h.min_deposit[o.cancellation]))
        auto = Money.of(h.floor_total(o).amount * (1 + h.approval_band), h.currency)
        o = replace(o, total=max(o.total, auto, key=lambda m: m.amount))
        return o if self.evaluate(o).verdict is Verdict.EXECUTE else None


def load_hotels(path: str | Path) -> dict[str, Hotel]:
    return {d["id"]: Hotel.from_dict(d) for d in json.loads(Path(path).read_text())}


def offer_from_dict(d: dict, currency: str) -> Offer:
    """Build an Offer from agent tool input (strings and numbers)."""
    return Offer(
        hotel_id=d["hotel_id"], room_code=d["room_code"],
        check_in=date.fromisoformat(d["check_in"]), check_out=date.fromisoformat(d["check_out"]),
        guests=int(d["guests"]), extras=tuple(d.get("extras", ())),
        cancellation=Cancellation(d["cancellation"]), deposit_pct=Decimal(str(d.get("deposit_pct", "0"))),
        total=Money.of(d["total"], currency), expires_at=datetime.fromisoformat(d["expires_at"]))


def offer_to_dict(o: Offer) -> dict:
    d = asdict(o)
    d.update(check_in=o.check_in.isoformat(), check_out=o.check_out.isoformat(), extras=list(o.extras),
             cancellation=o.cancellation.value, deposit_pct=str(o.deposit_pct),
             total=str(o.total.amount), expires_at=o.expires_at.isoformat())
    return d


class Negotiation:
    """Offer history between one buyer and one hotel, capped at max_rounds proposals."""

    def __init__(self, policy: SellerPolicy, max_rounds: int = 4, audit=None):
        self.policy = policy
        self.max_rounds = max_rounds
        self.audit = audit
        self.history: list[tuple[str, Offer, SellerDecision]] = []

    @property
    def rounds_left(self) -> int:
        return self.max_rounds - len(self.history)

    def propose(self, by: str, offer: Offer) -> SellerDecision:
        if self.rounds_left <= 0:
            raise RuntimeError("negotiation round limit reached")
        decision = self.policy.evaluate(offer)
        self.history.append((by, offer, decision))
        if self.audit:
            self.audit.record("offer_proposed", by=by, round=len(self.history), offer=offer_to_dict(offer),
                              verdict=decision.verdict.value, public=decision.public, internal=decision.internal)
        return decision
