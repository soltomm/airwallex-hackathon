"""Kit 10: the hotels as an Airwallex Agentic Commerce merchant.

Airwallex hosts the shopping agents' MCP (product search, checkout) from a catalogue we upload,
and calls our create_checkout URL when an agent buys. This module builds that catalogue from the
live inventory, as Google Merchant Center rows, and answers create_checkout through the offer
desk, so a catalogue checkout gets the same re-check, ledger and card deposit as a negotiated one.

One product per room, rate plan and cancellation term: room only, or breakfast for 1..max guests
(breakfast is priced per guest), each non-refundable or free to cancel until 48h / 24h. `price`
is the published rate per night; the total for the shopper's dates comes back from checkout.
A checkout may instead carry an `offer_id` a hotel's seller agent committed to, and is then
booked at that negotiated price.

Airwallex has published the onboarding and catalogue API but not the create_checkout request,
response or signature. Their assumed shape lives in parse_checkout() and checkout_response(), and
the signature reuses the webhook convention (webhooks.signature_ok: x-timestamp + x-signature, hex
HMAC-SHA256 of timestamp + raw body). Replace those when the spec is shared.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date

from .ledger import DuplicateOperation, OpStatus
from .money import Money
from .offers import OFFER_TTL, Cancellation, Hotel, Offer
from .webhooks import signature_ok

API = "/pa/agentic_commerce"     # under the Airwallex API base (/api/v1)
BATCH = 300                      # most products one direct upsert takes
TENANT = "agentic_commerce"      # bookings made through Airwallex's merchant MCP
MAX_NIGHTS = 30

CANCEL_CODES = {Cancellation.NON_REFUNDABLE: "nr", Cancellation.FREE_48H: "f48", Cancellation.FREE_24H: "f24"}
CANCEL_LABELS = {Cancellation.NON_REFUNDABLE: "non-refundable",
                 Cancellation.FREE_48H: "free cancellation until 48h before arrival",
                 Cancellation.FREE_24H: "free cancellation until 24h before arrival"}
GMC_FIELDS = ("id", "item_group_id", "title", "description", "link", "image_link", "availability", "price",
              "brand", "condition", "product_type", "identifier_exists")
_ID = re.compile(r"(htl_\w+)\.(\w+)\.(ro|bb[1-9])\.(nr|f48|f24)")


class CheckoutError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Plan:
    """What one catalogue product sells."""
    hotel_id: str
    room_code: str
    breakfast_guests: int          # 0: room only
    cancellation: Cancellation

    @property
    def product_id(self) -> str:
        board = f"bb{self.breakfast_guests}" if self.breakfast_guests else "ro"
        return f"{self.hotel_id}.{self.room_code}.{board}.{CANCEL_CODES[self.cancellation]}"

    @classmethod
    def parse(cls, product_id: str) -> "Plan":
        m = _ID.fullmatch(product_id or "")
        if not m:
            raise CheckoutError(422, f"unknown product {product_id!r}")
        cancel = next(c for c, code in CANCEL_CODES.items() if code == m.group(4))
        return cls(m.group(1), m.group(2), 0 if m.group(3) == "ro" else int(m.group(3)[2:]), cancel)


def plans(h: Hotel) -> list[Plan]:
    return [Plan(h.id, room.code, n, cancel) for room in h.rooms.values()
            for n in range(room.max_guests + 1) for cancel in Cancellation]


def nightly_price(h: Hotel, p: Plan) -> Money:
    """Published price per night: the room, breakfast per guest, and the refundable-terms premium."""
    amount = h.rooms[p.room_code].published_rate.amount + h.breakfast_price * p.breakfast_guests
    return Money.of(amount * (1 + h.risk_premium[p.cancellation]), h.currency)


def product(h: Hotel, p: Plan, base_url: str) -> dict:
    room = h.rooms[p.room_code]
    board = f"breakfast for {p.breakfast_guests}" if p.breakfast_guests else "room only"
    guests = f"{p.breakfast_guests} guest{'s' * (p.breakfast_guests > 1)}" if p.breakfast_guests \
        else f"up to {room.max_guests} guests"
    price = nightly_price(h, p)
    return {
        "id": p.product_id, "item_group_id": f"{h.id}.{room.code}",
        "title": f"{h.name}, {room.name}, {board}, {CANCEL_LABELS[p.cancellation].split(' until')[0]}",
        "description": f"{room.name} at {h.name}{f', {h.city}' if h.city else ''}: {board}, {guests}, "
                       f"{CANCEL_LABELS[p.cancellation]}. Price per night; checkout returns the total for your "
                       f"dates and the deposit held on your card. Rates are negotiable with the hotel's agent.",
        "link": f"{base_url}/catalogue/{p.product_id}", "image_link": f"{base_url}/catalogue/{h.id}.svg",
        "availability": "in_stock", "price": f"{price.amount} {price.currency}",
        "brand": h.name, "condition": "new",
        "product_type": " > ".join(x for x in ("Hotels", h.city, h.name) if x), "identifier_exists": "no",
    }


def catalogue(hotels: dict[str, Hotel], base_url: str) -> list[dict]:
    base_url = base_url.rstrip("/")
    return [product(h, p, base_url) for h in list(hotels.values()) for p in plans(h)]


def tsv(rows: list[dict]) -> str:
    """The same rows as a GMC feed file (tab-separated, header row of attribute names)."""
    clean = lambda v: re.sub(r"[\t\r\n]+", " ", str(v))
    return "\n".join(["\t".join(GMC_FIELDS)] + ["\t".join(clean(r.get(f, "")) for f in GMC_FIELDS) for r in rows]) + "\n"


def batches(rows: list[dict], size: int = BATCH) -> list[dict]:
    """Bodies for POST /pa/agentic_commerce/products."""
    return [{"type": "gmc", "gmc_products": rows[i:i + size]} for i in range(0, len(rows), size)]


def image(h: Hotel) -> bytes:
    """Placeholder product image: the hotel's name on a plain card."""
    name = h.name.replace("&", "&amp;").replace("<", "&lt;")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" viewBox="0 0 800 600">'
            f'<rect width="800" height="600" fill="#1f3a5f"/><text x="400" y="290" fill="#fff" font-family="Georgia,serif"'
            f' font-size="40" text-anchor="middle">{name}</text><text x="400" y="350" fill="#c9d6e8"'
            f' font-family="Helvetica,sans-serif" font-size="24" text-anchor="middle">{h.city}</text></svg>').encode()


# ---- create_checkout --------------------------------------------------------
@dataclass(frozen=True)
class CheckoutRequest:
    checkout_id: str
    product_id: str | None
    offer_id: str | None
    check_in: str | None
    check_out: str | None
    guests: int | None


def parse_checkout(body: dict) -> CheckoutRequest:
    """ASSUMED shape until Airwallex shares it: an id for the checkout, one item (a product id, or a
    negotiated offer_id), the stay dates and guests, at the top level or on the item."""
    if not isinstance(body, dict):
        raise CheckoutError(400, "send the checkout as a JSON object")
    items = body.get("items") or body.get("line_items") or [{}]
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise CheckoutError(422, "one room per checkout")
    item = items[0]
    pick = lambda *keys: next((src[k] for k in keys for src in (item, body) if src.get(k) not in (None, "")), None)
    if int(pick("quantity") or 1) != 1:
        raise CheckoutError(422, "one room per checkout")
    checkout_id = pick("checkout_id", "checkout_session_id", "id")
    if not checkout_id:
        raise CheckoutError(400, "missing checkout id")
    guests = pick("guests", "number_of_guests")
    return CheckoutRequest(str(checkout_id), pick("product_id", "sku"), pick("offer_id"), pick("check_in"),
                           pick("check_out"), int(guests) if guests is not None else None)


def checkout_response(checkout_id: str, booking: dict, pay_url: str) -> dict:
    """ASSUMED shape: our order number, what was booked, and how the shopper pays the deposit."""
    hold = booking["cancellation"] != Cancellation.NON_REFUNDABLE.value
    return {"checkout_id": checkout_id, "status": "created", "merchant_order_id": booking["booking_ref"],
            "currency": booking["currency"], "total": booking["total"],
            "line_items": [{"product_id": booking.get("product_id"), "offer_id": booking.get("offer_id"),
                            "title": f"{booking['hotel']}, {booking['room']}", "quantity": 1,
                            "check_in": booking["check_in"], "check_out": booking["check_out"],
                            "guests": booking["guests"], "extras": booking["extras"],
                            "cancellation": booking["cancellation"]}],
            "payment": {"type": "deposit_hold" if hold else "charge", "amount": booking["deposit"],
                        "currency": booking["currency"], "url": pay_url}}


class MerchantCheckout:
    """Answers Airwallex's create_checkout calls. Off (404) unless a shared secret is configured."""

    def __init__(self, desk, secret: str, tenant: str = TENANT):
        self.desk = desk
        self.secret = secret
        self.tenant = tenant

    def receive(self, headers: dict, raw: bytes, now_ms: int | None = None) -> tuple[int, dict]:
        if not self.secret:
            return 404, {"error": "not found"}
        h = {k.lower(): v for k, v in headers.items()}
        if not signature_ok(self.secret, h.get("x-timestamp", ""), raw, h.get("x-signature", ""), now_ms):
            return 401, {"error": "bad signature"}
        try:
            req = parse_checkout(json.loads(raw))
            return 200, self.checkout(req)
        except ValueError as e:   # CheckoutError, or malformed JSON / numbers
            return getattr(e, "status", 400), {"status": "rejected", "error": str(e)}

    def checkout(self, req: CheckoutRequest) -> dict:
        """At most one booking per checkout id: a retry returns the first outcome, never a second room."""
        key = f"agentic_checkout:{req.checkout_id}"
        try:
            done = self.desk.ledger.run(key, lambda _rid: self._book(req))
        except DuplicateOperation as e:
            if e.op.status is not OpStatus.SUCCEEDED:
                return {"checkout_id": req.checkout_id, "status": "in_progress",
                        "error": "this checkout is being processed; read it again shortly"}
            done = e.op.result
        if "booking_ref" not in done:
            return {"checkout_id": req.checkout_id, **done}
        booking = self.desk.bookings.get(done["booking_ref"])
        return checkout_response(req.checkout_id, {**booking, "product_id": done.get("product_id")},
                                 f"{self.desk.pay_base_url}/pay/{booking['booking_ref']}")

    def _book(self, req: CheckoutRequest) -> dict:
        offer_id, tenant = (self._negotiated(req) if req.offer_id else self._from_catalogue(req))
        # The shopper confirmed this item in their agent before Airwallex called create_checkout.
        out = self.desk.book(offer_id, traveller_confirmed=True, payment_method="card", tenant=tenant)
        if out["result"] in ("booked", "booked_payment_unavailable", "already_booked"):
            return {"booking_ref": out["booking_ref"], "offer_id": offer_id, "product_id": req.product_id}
        if out["result"] == "terms_changed":
            new = out["new_offer"]
            return {"status": "price_changed", "reason": out["reason"], "offer_id": new["offer_id"],
                    "total": new["total"], "next_step": "show the shopper the new terms; check out again with "
                                                        "this offer_id once they agree"}
        return {"status": "unavailable", "reason": out.get("reason", out["result"])}

    def _negotiated(self, req: CheckoutRequest) -> tuple[str, str]:
        """An offer a hotel's seller agent committed to, at its negotiated price."""
        offer = self.desk.offers.get(req.offer_id)
        if offer is None:
            raise CheckoutError(422, f"unknown offer {req.offer_id}")
        if req.product_id:
            p = Plan.parse(req.product_id)
            if (p.hotel_id, p.room_code) != (offer.hotel_id, offer.room_code):
                raise CheckoutError(422, "offer_id is for a different hotel or room than product_id")
        return req.offer_id, self.desk.offer_tenant[req.offer_id]

    def _from_catalogue(self, req: CheckoutRequest) -> tuple[str, str]:
        """The product at its published price for the shopper's dates, committed as an offer."""
        p = Plan.parse(req.product_id or "")
        h = self.desk.hotels.get(p.hotel_id)
        if h is None or p.room_code not in h.rooms:
            raise CheckoutError(422, f"unknown product {req.product_id!r}")
        room = h.rooms[p.room_code]
        if p.breakfast_guests > room.max_guests:
            raise CheckoutError(422, f"unknown product {req.product_id!r}")
        try:
            check_in, check_out = date.fromisoformat(req.check_in or ""), date.fromisoformat(req.check_out or "")
        except ValueError:
            raise CheckoutError(422, "check_in and check_out must be ISO dates (2026-11-20)") from None
        if not 0 < (check_out - check_in).days <= MAX_NIGHTS:
            raise CheckoutError(422, f"a stay is 1 to {MAX_NIGHTS} nights")
        guests = req.guests or p.breakfast_guests or 1
        if p.breakfast_guests and guests != p.breakfast_guests:
            raise CheckoutError(422, f"this rate includes breakfast for {p.breakfast_guests}; "
                                     f"pick the product for {guests} guests")
        if guests > room.max_guests:
            raise CheckoutError(422, f"{room.name} sleeps at most {room.max_guests}")
        offer = Offer(h.id, room.code, check_in, check_out, guests, ("breakfast",) if p.breakfast_guests else (),
                      p.cancellation, h.min_deposit[p.cancellation], Money.of(0, h.currency),
                      self.desk.now() + OFFER_TTL)
        offer = replace(offer, total=h.published_total(offer))
        return self.desk.commit(offer, self.tenant), self.tenant
