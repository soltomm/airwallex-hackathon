"""Card deposits in the guest's currency, while the hotel prices, negotiates and settles in EUR.

Airwallex Multi-Currency Pricing: when the PaymentIntent opens, a 24-hour conversion quote fixes the
EUR -> guest rate and is applied to the intent, which then reads in the guest's currency. The booking
keeps that rate, so every later hold change (capture, increment at check-out) is the EUR amount at the
same rate, rounded half-to-even as Airwallex does. Offers, floors and the seller's policy never see
anything but EUR.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from .airwallex import AirwallexAPI
from .ledger import OperationLedger
from .money import Money, _places

GUEST_CURRENCIES = ("EUR", "USD", "GBP")


def convert(amount: Money, rate: str | Decimal, currency: str) -> Money:
    cur = currency.upper()
    return Money.of((amount.amount * Decimal(str(rate))).quantize(_places(cur), ROUND_HALF_EVEN), cur)


def lock_rate(api: AirwallexAPI, ledger: OperationLedger, ref: str, intent_id: str,
              total: Money, guest_currency: str) -> dict:
    """Quote and apply the rate to the intent once; returns the booking's `fx` record."""
    quote = ledger.run(f"fxquote:{ref}", lambda rid: api.create_conversion_quote(
        rid, total.currency, guest_currency))
    pi = ledger.run(f"fxapply:{intent_id}", lambda rid: api.apply_conversion_quote(rid, intent_id, quote["id"]))
    rate = str(quote["conversion_rate"])
    deposit = Money.of(pi["amount"], pi["currency"])   # what Airwallex will authorise, to the cent
    return {"currency": deposit.currency, "rate": rate, "quote_id": quote["id"],
            "quote_expires_at": quote.get("expires_at"), "deposit": str(deposit.amount),
            "total": str(convert(total, rate, deposit.currency).amount)}


def charged(b: dict, field: str) -> Money:
    """`deposit` or `total` in the currency the card is held in."""
    fx = b.get("fx")
    if fx and fx.get("currency"):
        return Money.of(fx[field], fx["currency"])
    return Money.of(b[field], b["currency"])


def describe(b: dict, field: str) -> str:
    """'425.17 GBP (490.00 EUR)' for guest-currency bookings, else '490.00 EUR'."""
    eur = f"{b[field]} {b['currency']}"
    fx = b.get("fx")
    return f"{fx[field]} {fx['currency']} ({eur})" if fx and fx.get("currency") else eur
