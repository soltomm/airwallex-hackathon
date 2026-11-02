"""Kit 10: the hotels as an Airwallex Agentic Commerce merchant.

Airwallex hosts the shopping agents' MCP (product search, checkout) from a catalogue we upload,
calls our create_checkout URL to price a cart, takes the payment itself (managed payment), and
tells us on the payment_intent.succeeded webhook. Contract: airwallexdev.com/agentic-commerce.

A cart carries no dates, so a product is one room-night: a room, rate plan (room only, or
breakfast for 1..max guests, priced per guest) and cancellation term on one date. A stay is the
consecutive nights of one plan in one cart.

create_checkout prices the cart and books nothing: it records the quote under our checkout id
and offers one DIGITAL fulfillment method whose id carries that checkout id. The paid webhook
echoes that id back (agentic_payment_data.fulfillment), and only then is the room booked through
the offer desk, at the quoted price, with the same policy re-check, ledger and payouts as any
booking. A payment that cannot be booked (sold out meanwhile, wrong amount) is refunded.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .guest_currency import GUEST_CURRENCIES, convert
from .ledger import DuplicateOperation
from .money import Money
from .offers import OFFER_TTL, Cancellation, Hotel, Offer
from .policy import Verdict
from .webhooks import signature_ok

API = "/pa/agentic_commerce"     # under the Airwallex API base (/api/v1)
BATCH = 300                      # most products one direct upsert takes
TENANT = "agentic_commerce"      # bookings made through Airwallex's merchant MCP
MAX_NIGHTS = 30
DAYS = 60                        # nights on sale, from today

CANCEL_CODES = {Cancellation.NON_REFUNDABLE: "nr", Cancellation.FREE_48H: "f48", Cancellation.FREE_24H: "f24"}
CANCEL_LABELS = {Cancellation.NON_REFUNDABLE: "non-refundable",
                 Cancellation.FREE_48H: "free cancellation until 48h before arrival",
                 Cancellation.FREE_24H: "free cancellation until 24h before arrival"}
GMC_FIELDS = ("id", "title", "description", "link", "image_link", "availability", "price",
              "brand", "condition", "product_type", "identifier_exists")
_PLAN = r"(htl_\w+)\.(\w+)\.(ro|bb[1-9])\.(nr|f48|f24)"
_ID = re.compile(_PLAN + r"\.(\d{8})")
_CHECKOUT = re.compile(r"(?:stay|confirmation)_(chk_[0-9a-f]{16})")
_B64 = re.compile(r"[A-Za-z0-9_-]{16,}")


def checkout_id_in(fulfillment) -> str | None:
    """Our checkout id in the paid webhook's fulfillment echo. Airwallex hands our method and option
    ids back base64url-encoded (dot-joined), so look in the decoded pieces as well as the plain text."""
    text = json.dumps(fulfillment or {})
    pieces = [text]
    for token in _B64.findall(text):
        try:
            pieces.append(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("ascii"))
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
    return next((m.group(1) for piece in pieces if (m := _CHECKOUT.search(piece))), None)


class CheckoutError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Plan:
    """A room, rate plan and cancellation term."""
    hotel_id: str
    room_code: str
    breakfast_guests: int          # 0: room only
    cancellation: Cancellation

    @property
    def product_id(self) -> str:
        board = f"bb{self.breakfast_guests}" if self.breakfast_guests else "ro"
        return f"{self.hotel_id}.{self.room_code}.{board}.{CANCEL_CODES[self.cancellation]}"


@dataclass(frozen=True)
class Night:
    """What one catalogue product sells: a plan on one date."""
    plan: Plan
    night: date

    @property
    def product_id(self) -> str:
        return f"{self.plan.product_id}.{self.night:%Y%m%d}"

    @classmethod
    def parse(cls, product_id: str) -> "Night":
        m = _ID.fullmatch(product_id or "")
        if not m:
            raise CheckoutError(422, f"unknown product {product_id!r}")
        cancel = next(c for c, code in CANCEL_CODES.items() if code == m.group(4))
        try:
            night = date(int(m.group(5)[:4]), int(m.group(5)[4:6]), int(m.group(5)[6:]))
        except ValueError:
            raise CheckoutError(422, f"unknown product {product_id!r}") from None
        return cls(Plan(m.group(1), m.group(2), 0 if m.group(3) == "ro" else int(m.group(3)[2:]), cancel), night)


def plans(h: Hotel) -> list[Plan]:
    return [Plan(h.id, room.code, n, cancel) for room in h.rooms.values()
            for n in range(room.max_guests + 1) for cancel in Cancellation]


def night_price(h: Hotel, p: Plan, night: date) -> Money:
    """Published price of one night: the room, breakfast per guest, and the refundable-terms premium."""
    amount = h.rate(p.room_code, night) + h.breakfast_price * p.breakfast_guests
    return Money.of(amount * (1 + h.risk_premium[p.cancellation]), h.currency)


def product(h: Hotel, n: Night, base_url: str) -> dict:
    p = n.plan
    room = h.rooms[p.room_code]
    board = f"breakfast for {p.breakfast_guests}" if p.breakfast_guests else "room only"
    guests = f"{p.breakfast_guests} guest{'s' * (p.breakfast_guests > 1)}" if p.breakfast_guests \
        else f"up to {room.max_guests} guests"
    price = night_price(h, p, n.night)
    return {
        # No item_group_id: the sandbox fails (HTTP 500) on any upsert holding two variants of one product
        "id": n.product_id,
        "title": f"{h.name}, {room.name}, {board}, {CANCEL_LABELS[p.cancellation].split(' until')[0]}, "
                 f"night of {n.night:%a %d %b %Y}",
        "description": f"{room.name} at {h.name}{f', {h.city}' if h.city else ''}: {board}, {guests}, "
                       f"{CANCEL_LABELS[p.cancellation]}. One night, arriving {n.night:%a %d %b %Y}; for a longer "
                       f"stay add the following nights of the same rate to the cart. Paid in full at checkout.",
        "link": f"{base_url}/catalogue/{n.product_id}", "image_link": f"{base_url}/catalogue/{h.id}.svg",
        "availability": "in_stock" if h.available(p.room_code, n.night) else "out_of_stock",
        "price": f"{price.amount} {price.currency}",
        "brand": h.name, "condition": "new",
        "product_type": [" > ".join(x for x in ("Hotels", h.city, h.name) if x)],   # a list in the JSON API
        "identifier_exists": "no",
    }


def catalogue(hotels: dict[str, Hotel], base_url: str, start: date, days: int = DAYS) -> list[dict]:
    base_url = base_url.rstrip("/")
    return [product(h, Night(p, start + timedelta(days=i)), base_url)
            for h in list(hotels.values()) for p in plans(h) for i in range(days)]


def tsv(rows: list[dict]) -> str:
    """The same rows as a GMC feed file (tab-separated, header row of attribute names)."""
    clean = lambda v: re.sub(r"[\t\r\n]+", " ", ",".join(v) if isinstance(v, list) else str(v))
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


def minor(m: Money) -> int:
    return int((m.amount * 100).to_integral_value())


# ---- create_checkout --------------------------------------------------------
@dataclass(frozen=True)
class Cart:
    """A create_checkout request read as a stay: consecutive nights of one plan."""
    plan: Plan
    check_in: date
    check_out: date
    rooms: int                       # requested_quantity, the same on every night
    line_items: tuple[dict, ...]     # as sent (id, item_id, requested_quantity)
    currency: str
    customer: dict


def parse_checkout(body: dict) -> Cart:
    if not isinstance(body, dict):
        raise CheckoutError(400, "send the checkout as a JSON object")
    items = body.get("line_items")
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
        raise CheckoutError(400, "line_items must be a non-empty list")
    if len(items) > MAX_NIGHTS:
        raise CheckoutError(422, f"a stay is 1 to {MAX_NIGHTS} nights")
    nights = [Night.parse(str(i.get("item_id", ""))) for i in items]
    if len({n.plan for n in nights}) != 1:
        raise CheckoutError(422, "one room and rate per checkout: every night must be the same plan")
    dates = sorted(n.night for n in nights)
    if any((b - a).days != 1 for a, b in zip(dates, dates[1:])):
        raise CheckoutError(422, "the nights of a stay must be consecutive, each once")
    try:
        qty = {int(i.get("requested_quantity", 1)) for i in items}
    except (TypeError, ValueError):
        raise CheckoutError(400, "requested_quantity must be a number") from None
    if len(qty) != 1 or min(qty) < 1:
        raise CheckoutError(422, "book the same number of rooms on every night")
    return Cart(nights[0].plan, dates[0], dates[-1] + timedelta(days=1), qty.pop(), tuple(items),
                str(body.get("currency") or "").upper(), body.get("customer") or {})


class MerchantCheckout:
    """Answers Airwallex's create_checkout calls and books paid checkouts. Off (404) without a shared secret."""

    def __init__(self, desk, secret: str, tenant: str = TENANT):
        self.desk = desk
        self.secret = secret
        self.tenant = tenant
        self.rejected: list[dict] = []   # recent refusals, newest last (GET /agentic-commerce/rejections)

    def receive(self, headers: dict, raw: bytes, now_ms: int | None = None) -> tuple[int, dict]:
        if not self.secret:
            return 404, {"error": "not found"}
        h = {k.lower(): v for k, v in headers.items()}
        if not signature_ok(self.secret, h.get("x-timestamp", ""), raw, h.get("x-signature", ""), now_ms):
            self._reject(401, "bad signature", raw, timestamp=h.get("x-timestamp"))
            return 401, {"error": "bad signature"}
        body = None
        try:
            body = json.loads(raw)
            return 200, self.checkout(parse_checkout(body))
        except ValueError as e:   # CheckoutError, or malformed JSON / numbers
            self._reject(getattr(e, "status", 400), str(e), raw)
            items = body.get("line_items") if isinstance(body, dict) else None
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                return getattr(e, "status", 400), {"error": str(e)}
            # Airwallex reads every reply as a checkout: refuse the cart as one, with the reason
            return 200, {"id": "chk_" + uuid.uuid4().hex[:16],
                         "line_items": [{**i, "available_quantity": 0} for i in items],
                         "totals": [{"type": t, "amount": 0, "display_text": t.title()} for t in ("subtotal", "total")],
                         "messages": [{"type": "error", "content_type": "plain", "content": str(e)}]}

    def _reject(self, status: int, reason: str, raw: bytes, **extra) -> None:
        try:
            items = [{k: i.get(k) for k in ("id", "item_id", "requested_quantity")}
                     for i in json.loads(raw).get("line_items") or []]
        except (ValueError, AttributeError, TypeError):
            items = raw[:300].decode("utf-8", "replace")
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": status, "reason": reason,
                 "line_items": items, **extra}
        self.rejected = self.rejected[-49:] + [entry]
        print(f"agentic checkout refused: {json.dumps(entry)}", file=sys.stderr)   # stdout is MCP

    def _offer(self, h: Hotel, cart: Cart, total: Money) -> Offer:
        p = cart.plan
        return Offer(h.id, p.room_code, cart.check_in, cart.check_out, p.breakfast_guests or 1,
                     ("breakfast",) if p.breakfast_guests else (), p.cancellation, Decimal(1), total,
                     self.desk.now() + OFFER_TTL)

    def checkout(self, cart: Cart) -> dict:
        """Price the cart at the published nightly rates and record the quote; nothing is booked yet."""
        p = cart.plan
        h = self.desk.hotels.get(p.hotel_id)
        if h is None or p.room_code not in h.rooms or p.breakfast_guests > h.rooms[p.room_code].max_guests:
            raise CheckoutError(422, f"unknown product {cart.line_items[0].get('item_id')!r}")
        total = Money.of(sum(night_price(h, p, cart.check_in + timedelta(days=i)).amount
                             for i in range((cart.check_out - cart.check_in).days)), h.currency)
        charge, rate = self._in_currency(total, cart.currency or h.currency)
        offer = self._offer(h, cart, total)
        open_ = cart.check_in >= self.desk.now().date() and \
            self.desk._policy(h.id).evaluate(offer).verdict is Verdict.EXECUTE
        checkout_id = "chk_" + uuid.uuid4().hex[:16]
        ids = [str(i.get("id", "")) for i in cart.line_items]
        if open_:   # one room per checkout: Airwallex shows the shopper the adjusted quantity
            self.desk.ledger.run(f"agentic_checkout:{checkout_id}", lambda _rid: {
                "hotel_id": h.id, "room_code": p.room_code, "breakfast_guests": p.breakfast_guests,
                "cancellation": p.cancellation.value, "check_in": cart.check_in.isoformat(),
                "check_out": cart.check_out.isoformat(), "total": str(total.amount), "currency": total.currency,
                "charge": str(charge.amount), "charge_currency": charge.currency, "rate": rate,
                "line_item_ids": ids, "customer": cart.customer})
        out = {"id": checkout_id,
               "line_items": [{**i, "available_quantity": 1 if open_ else 0} for i in cart.line_items],
               "totals": [{"type": t, "amount": minor(charge) if open_ else 0, "display_text": t.title()}
                          for t in ("subtotal", "total")],
               "permalink_url": f"{self.desk.pay_base_url}/catalogue/{Night(p, cart.check_in).product_id}"}
        if open_:
            out["fulfillment"] = {"methods": [{
                "id": f"stay_{checkout_id}", "type": "DIGITAL", "line_item_ids": ids,
                "options": [{"id": f"confirmation_{checkout_id}", "cost": 0,
                             "title": f"Booking confirmation from {h.name}; nothing more to pay at the hotel"}]}]}
        return out

    def _in_currency(self, total: Money, currency: str) -> tuple[Money, str | None]:
        """The hotel's price in the shopper's currency, at Airwallex's current rate (no rate: same currency).
        Airwallex prices the whole checkout in the currency it asks for."""
        if currency == total.currency:
            return total, None
        if currency not in GUEST_CURRENCIES:
            raise CheckoutError(422, f"prices are available in {', '.join(GUEST_CURRENCIES)}")
        rate = str(self.desk.payments._api().fx_rate(total.currency, currency)["rate"])
        return convert(total, rate, currency), rate

    # ---- payment_intent.succeeded ------------------------------------------
    def on_payment(self, event: dict) -> str | None:
        """Book the stay a managed payment paid for. Returns the booking ref, or None if the event is
        not an agentic checkout of ours. Runs at most once per payment intent."""
        obj = (event.get("data") or {}).get("object") or {}
        if event.get("name") != "payment_intent.succeeded" or not obj.get("agentic_payment_data"):
            return None
        checkout_id = checkout_id_in(obj["agentic_payment_data"].get("fulfillment"))
        quote = self.desk.ledger.get(f"agentic_checkout:{checkout_id}") if checkout_id else None
        if quote is None or not quote.result:
            self.desk.audit.record("agentic_payment_unmatched", payment_intent_id=obj.get("id"))
            return None
        try:
            done = self.desk.ledger.run(f"agentic_paid:{obj['id']}",
                                        lambda _rid: self._book(checkout_id, quote.result, obj))
        except DuplicateOperation as e:
            done = e.op.result or {}
        return done.get("booking_ref")

    def _book(self, checkout_id: str, q: dict, pi: dict) -> dict:
        h = self.desk.hotels[q["hotel_id"]]
        total = Money.of(Decimal(q["total"]), q["currency"])
        charge = Money.of(Decimal(q.get("charge", q["total"])), q.get("charge_currency", q["currency"]))
        paid = Money.of(Decimal(str(pi.get("amount", 0))), str(pi.get("currency", "")).upper())
        if paid != charge:
            return self._refund(pi, paid, f"paid {paid}, checkout {checkout_id} was {charge}")
        plan = Plan(h.id, q["room_code"], q["breakfast_guests"], Cancellation(q["cancellation"]))
        cart = Cart(plan, date.fromisoformat(q["check_in"]), date.fromisoformat(q["check_out"]), 1, (), total.currency,
                    q.get("customer") or {})
        offer_id = self.desk.commit(self._offer(h, cart, total), self.tenant)
        out = self.desk.book(offer_id, traveller_confirmed=True, payment_method="agentic", tenant=self.tenant)
        if out["result"] not in ("booked", "already_booked"):
            return self._refund(pi, paid, out.get("reason", out["result"]))
        ref = out["booking_ref"]
        self.desk.bookings.update(ref, f"paid {paid} through Airwallex Agentic Commerce", status="paid",
                                  payment_intent_id=pi["id"], agentic_checkout=checkout_id,
                                  **({"fx": {"currency": charge.currency, "rate": q["rate"], "deposit": str(charge.amount),
                                             "total": str(charge.amount)}} if q.get("rate") else {}),
                                  guest={k: cart.customer.get(k) for k in ("first_name", "last_name", "email")
                                         if cart.customer.get(k)})
        try:
            self.desk.payments.settle(ref, "deposit")   # the hotel's share; retried from hotel_ops.py if this fails
        except Exception as e:
            self.desk.audit.record("agentic_settle_failed", booking_ref=ref, error=str(e))
        self.desk.note_booking(ref)
        return {"booking_ref": ref}

    def _refund(self, pi: dict, amount: Money, reason: str) -> dict:
        self.desk.ledger.run(f"refund:agentic:{pi['id']}", lambda rid: self.desk.payments._api().create_refund(
            rid, pi["id"], amount, f"Could not book: {reason}"))
        self.desk.audit.record("agentic_payment_refunded", payment_intent_id=pi["id"], amount=str(amount), reason=reason)
        return {"refunded": str(amount), "reason": reason}
