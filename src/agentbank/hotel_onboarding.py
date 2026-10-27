"""Hotels join the marketplace themselves: a sign-up becomes a live hotel and an Airwallex connected account.

join() validates the hotel's rooms, rates and negotiation style in code and lists the hotel at once:
buyer agents find it in search_hotels and its seller agent negotiates from the next call. Its
connected account is opened through Airwallex's Accounts API (native onboarding) in the background;
until it is ACTIVE with card capabilities, card deposits are collected by the platform without a
payout split, and the revenue desk shows the hotel's payouts as pending.

Sandbox: beyond the hotel's name, country and email, business and person details are synthetic and
the account is activated by simulation. In production the hotel would complete KYC in Airwallex's
embedded onboarding component (or the hosted flow) instead.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from .airwallex import AirwallexError
from .offers import Hotel

INDUSTRY = "ICCV3_1102XX"   # Travel and lodging / Hotels, inns, and motels
CARD_CAPABILITIES = ("payments_visa", "payments_mastercard")
# The sandbox accepts this US company template (scripts/platform_test.py); only names change per hotel.
ADDRESS = {"address_line1": "1 Market St", "suburb": "San Francisco", "state": "CA", "postcode": "94105",
           "country_code": "US"}
COMMISSION = "0.12"

# Commercial presets a hotel picks from; its prices and rooms are its own. Risk settings stay in code
# so a sign-up form can't give a seller agent terms the platform would not stand behind.
STYLES = {
    "boutique": {
        "label": "Boutique: protect the rate, add extras",
        "guidance": "Boutique hotel. Protect the room rate: prefer adding breakfast or late checkout over cutting "
                    "price. Flexible on cancellation if the guest accepts a larger deposit hold.",
        "high_occupancy": "0.85", "approval_band": "0.05",
        "risk_premium": {"non_refundable": "0", "free_until_48h": "0.10", "free_until_24h": "0.15"},
        "min_deposit": {"non_refundable": "1", "free_until_48h": "0.20", "free_until_24h": "0.30"},
    },
    "business": {
        "label": "Business: little discounting when busy",
        "guidance": "Business hotel. Little room to discount on busy nights; never give late checkout on full days. "
                    "Reward guests who take non-refundable terms.",
        "high_occupancy": "0.85", "approval_band": "0.05",
        "risk_premium": {"non_refundable": "0", "free_until_48h": "0.12", "free_until_24h": "0.18"},
        "min_deposit": {"non_refundable": "1", "free_until_48h": "0.25", "free_until_24h": "0.35"},
    },
    "budget": {
        "label": "Budget: fill rooms, small extras",
        "guidance": "Budget hotel. Fill rooms: discount readily on quiet nights, keep extras simple, and ask for a "
                    "bigger deposit hold in exchange for flexible cancellation.",
        "high_occupancy": "0.90", "approval_band": "0.03",
        "risk_premium": {"non_refundable": "0", "free_until_48h": "0.08", "free_until_24h": "0.12"},
        "min_deposit": {"non_refundable": "1", "free_until_48h": "0.30", "free_until_24h": "0.40"},
    },
    "luxury": {
        "label": "Luxury: never discount, add experiences",
        "guidance": "Luxury hotel. Rarely discount the room; win the guest with breakfast, late checkout and "
                    "flexible terms instead. Keep the tone warm and discreet.",
        "high_occupancy": "0.80", "approval_band": "0.08",
        "risk_premium": {"non_refundable": "0", "free_until_48h": "0.10", "free_until_24h": "0.15"},
        "min_deposit": {"non_refundable": "1", "free_until_48h": "0.20", "free_until_24h": "0.30"},
    },
}
MAX_ROOM_TYPES = 4
_NAME = re.compile(r"^[\w .,'&()\-]+$")      # letters (accents too), digits and plain punctuation
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class SignupError(ValueError):
    """The sign-up can't be accepted; the message says which field and why."""


def ascii_name(name: str) -> str:
    """Store names accept Latin letters without accents ("Hôtel" -> "Hotel")."""
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()


def _slug(text: str, n: int) -> str:
    return re.sub(r"[^a-z0-9]+", "_", ascii_name(text).lower()).strip("_")[:n].strip("_")


def _text(form: dict, key: str, label: str, lo: int, hi: int, pattern=_NAME) -> str:
    v = " ".join(str(form.get(key) or "").split())
    if not lo <= len(v) <= hi:
        raise SignupError(f"{label}: {lo} to {hi} characters")
    if pattern and not pattern.match(v):
        raise SignupError(f"{label}: letters, digits and simple punctuation only")
    return v


def _amount(v, label: str, lo: Decimal, hi: Decimal, whole: bool = True) -> Decimal:
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise SignupError(f"{label}: enter a number") from None
    if not d.is_finite() or not lo <= d <= hi:
        raise SignupError(f"{label}: between {lo} and {hi}")
    if whole and d != d.to_integral_value():
        raise SignupError(f"{label}: whole numbers only")
    return d


def hotel_from_signup(form: dict, taken_ids) -> dict:
    """A hotels.json record from the sign-up form, or SignupError. Prices are EUR per night."""
    name = _text(form, "name", "Hotel name", 3, 60)
    city = _text(form, "city", "City", 2, 40)
    email = _text(form, "email", "Contact email", 5, 120, _EMAIL)
    style = form.get("style")
    if style not in STYLES:
        raise SignupError(f"Style: one of {', '.join(STYLES)}")
    note = " ".join(str(form.get("guidance") or "").split())
    if len(note) > 300:
        raise SignupError("Guidance for your agent: 300 characters at most")
    rooms_in = form.get("rooms") or []
    if not isinstance(rooms_in, list) or not 1 <= len(rooms_in) <= MAX_ROOM_TYPES:
        raise SignupError(f"Rooms: 1 to {MAX_ROOM_TYPES} room types")
    rooms, codes = [], set()
    for i, r in enumerate(rooms_in, 1):
        if not isinstance(r, dict):
            raise SignupError(f"Room {i}: missing details")
        rname = _text(r, "name", f"Room {i} name", 2, 40)
        published = _amount(r.get("published_rate"), f"{rname}: published rate", Decimal(30), Decimal(5000))
        floor = _amount(r.get("floor_rate"), f"{rname}: lowest rate", Decimal(20), Decimal(5000))
        if floor > published:
            raise SignupError(f"{rname}: the lowest rate can't be above the published rate")
        if floor < published * Decimal("0.4"):
            raise SignupError(f"{rname}: the lowest rate must be at least 40% of the published rate")
        code = (_slug(rname, 3).replace("_", "").upper() or "RM")[:3]
        code = next(c for c in (code, *(f"{code}{n}" for n in range(2, 10))) if c not in codes)
        codes.add(code)
        rooms.append({"code": code, "name": rname,
                      "max_guests": int(_amount(r.get("max_guests"), f"{rname}: sleeps", Decimal(1), Decimal(6))),
                      "total_rooms": int(_amount(r.get("total_rooms"), f"{rname}: rooms", Decimal(1), Decimal(500))),
                      "published_rate": str(published), "floor_rate": str(floor)})
    breakfast = _amount(form.get("breakfast_price"), "Breakfast price", Decimal(5), Decimal(100))
    late = _amount(form.get("late_checkout_price"), "Late checkout price", Decimal(0), Decimal(200))
    preset = STYLES[style]
    base = f"htl_{_slug(city, 10)}_{_slug(name, 16)}"
    hid = next(i for i in (base, *(f"{base}_{n}" for n in range(2, 100))) if i not in taken_ids)
    return {
        "id": hid, "name": name, "city": city, "currency": "EUR", "rooms": rooms, "booked": [],
        "rules": {
            "breakfast_cost": str((breakfast * Decimal("0.45")).quantize(Decimal(1))),   # assumed food cost
            "breakfast_price": str(breakfast), "late_checkout_price": str(late),
            **{k: preset[k] for k in ("high_occupancy", "approval_band", "risk_premium", "min_deposit")},
            "negotiation_style": preset["guidance"] + (f" The hotel adds: {note}" if note else ""),
        },
        "platform": {"commission_pct": COMMISSION},
        "onboarding": {"style": style, "email": email, "status": "listed"},
    }


# ---- Airwallex connected account (native API) ---------------------------------
def account_details(name: str, email: str = "alex.rivera@example.com") -> dict:
    return {
        # Card acceptance (payments_visa/mastercard capabilities) needs a store; without one the
        # account is ACTIVE but PaymentIntents tagged with it fail "not enabled for payment acceptance".
        "store_details": {
            "store_name": ascii_name(name), "store_description": "Hotel rooms, breakfast and late checkout",
            "industry_code": INDUSTRY, "mcc": "7011", "operating_models": ["ONLINE_CHECKOUT"],
            "payment_distribution": [{"payment_type": "CARDS", "percentage": "100"}],
            "estimated_transaction_volume": {"currency": "USD", "average_amount_per_transaction": "500",
                                             "max_amount_per_transaction": "5000", "monthly_transaction_amount": "50000"},
            "cross_border_transaction_percent": "60", "dispute_percent": "0.5", "refund_percent": "5",
            "fulfillment_days": 30, "store_address": ADDRESS, "store_websites": [{"url": "https://example.com"}],
        },
        "business_details": {
            "business_name": name, "business_structure": "COMPANY",
            "business_identifiers": [{"country_code": "US", "number": "12-3456789", "type": "EIN"}],
            "industry_category_code": INDUSTRY, "business_address": ADDRESS, "registration_address": ADDRESS,
            "operating_country": ["US"], "url": "https://example.com",
            "description_of_goods_or_services": "Hotel rooms and stays",
            "contact_number": "4155550100", "state_of_incorporation": "DE", "business_start_date": "2020-01-15",
            "account_usage": {"estimated_monthly_revenue": {"amount": "50000", "currency": "USD"},
                              "product_reference": ["RECEIVE_TRANSFERS", "MAKE_TRANSFERS"]},
        },
        "business_person_details": [{
            "first_name": "Alex", "last_name": "Rivera", "date_of_birth": "1985-04-12",
            "email": email, "nationality": "US",
            "roles": ["DIRECTOR", "AUTHORISED_PERSON", "BENEFICIAL_OWNER"], "residential_address": ADDRESS,
            "identifications": {"primary": {"identification_type": "PASSPORT", "issuing_country_code": "US",
                                            "passport": {"number": "X12345678", "expire_at": "2034-01-01"}}},
        }],
    }


def accepts_cards(client, acct: str) -> bool:
    return client.get("/account_capabilities/payments_visa", on_behalf_of=acct).get("status") in ("ENABLED", "ACTIVE")


def open_account(client, name: str, email: str, request_id: str | None = None, account: str | None = None,
                 step: Callable[[str, dict], None] = lambda status, info: None, sleep=time.sleep) -> bool:
    """Create (or resume) a hotel's connected account, activate it in the sandbox and enable card
    capabilities. Reports progress through step(status, info); True once it accepts cards."""
    if not account:
        account = client.post("/accounts/create", {
            "request_id": request_id or str(uuid.uuid4()), "account_details": account_details(name, email),
            "customer_agreements": {"agreed_to_data_usage": True, "agreed_to_terms_and_conditions": True},
            "primary_contact": {"email": email}})["id"]
        step("account_created", {"connected_account": account})
    status = client.get(f"/accounts/{account}").get("status")
    if status != "ACTIVE":
        if status in (None, "CREATED", "ACTION_REQUIRED"):
            client.post(f"/accounts/{account}/submit", {})
            step("submitted", {})
            sleep(2)
        client.post(f"/simulation/accounts/{account}/update_status", {"next_status": "ACTIVE", "force": True})
        for _ in range(10):
            if client.get(f"/accounts/{account}").get("status") == "ACTIVE":
                break
            sleep(2)
        else:
            raise AirwallexError(409, "not_active", f"account {account} did not become ACTIVE", {})
        step("active", {})
        sleep(2)   # enabling capabilities right after activation fails validation
    if accepts_cards(client, account):
        return True
    for cap in CARD_CAPABILITIES:
        client.post(f"/account_capabilities/{cap}/enable", {"id": cap}, on_behalf_of=account)
    step("capabilities_requested", {})
    for _ in range(30):   # PENDING -> ENABLED is reviewed asynchronously, even in the sandbox
        if accepts_cards(client, account):
            return True
        sleep(5)
    return False


class HotelOnboarding:
    """Self-serve sign-ups: lists each hotel in `hotels` (the desk's live inventory) and opens its
    Airwallex connected account in the background. Sign-ups persist in `path` across restarts."""

    def __init__(self, hotels: dict[str, Hotel], path: str | Path | None = None, client=None, audit=None,
                 max_hotels: int | None = None, background: bool = True, sleep=time.sleep,
                 now=lambda: datetime.now(timezone.utc)):
        self.hotels = hotels
        self.path = Path(path) if path else None
        self.client = client          # raw AirwallexClient; None: hotels are listed without payouts
        self.audit = audit
        self.max_hotels = max_hotels
        self.background = background
        self.sleep = sleep
        self.now = now
        self._lock = threading.RLock()
        self.records: dict[str, dict] = {}
        if self.path and self.path.exists():
            for rec in json.loads(self.path.read_text()):
                self.records[rec["id"]] = rec
                if rec["id"] not in hotels:
                    hotels[rec["id"]] = Hotel.from_dict(rec)

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.records.values()), indent=2, ensure_ascii=False))
            tmp.replace(self.path)

    def _record(self, event: str, **data) -> None:
        if self.audit:
            self.audit.record(event, **data)

    def join(self, form: dict) -> dict:
        """List a new hotel now and start its Airwallex onboarding. Raises SignupError."""
        with self._lock:
            if self.max_hotels is not None and len(self.records) >= self.max_hotels:
                raise SignupError("this demo has reached its limit of new hotels; ask the platform team")
            rec = hotel_from_signup(form, set(self.hotels))
            if any(h.name.casefold() == rec["name"].casefold() and h.city.casefold() == rec["city"].casefold()
                   for h in self.hotels.values()):
                raise SignupError(f"{rec['name']} in {rec['city']} is already on the marketplace")
            rec["onboarding"].update(joined_at=self.now().isoformat(), request_id=str(uuid.uuid4()),
                                     status="listed" if self.client else "no_airwallex")
            self.records[rec["id"]] = rec
            self.hotels[rec["id"]] = Hotel.from_dict(rec)   # buyer agents see it from the next search
            self._save()
        self._record("hotel_joined", hotel_id=rec["id"], name=rec["name"], city=rec["city"],
                     style=rec["onboarding"]["style"])
        if self.client:
            self._start(rec["id"])
        return self.status(rec["id"])

    def retry(self, hotel_id: str) -> dict:
        """Resume a sign-up whose Airwallex onboarding failed or was cut off by a restart."""
        rec = self.records.get(hotel_id)
        if rec is None:
            return {"error": f"{hotel_id} did not join through sign-up"}
        if not self.client:
            return {"error": "Airwallex is not configured on this server"}
        if rec["onboarding"]["status"] == "accepting_cards":
            return self.status(hotel_id)
        self._start(hotel_id)
        return self.status(hotel_id)

    def _start(self, hotel_id: str) -> None:
        self._update(hotel_id, "opening_account", error=None)
        if self.background:
            threading.Thread(target=self._open, args=(hotel_id,), daemon=True).start()
        else:
            self._open(hotel_id)

    def _update(self, hotel_id: str, status: str, **info) -> None:
        with self._lock:
            ob = self.records[hotel_id]["onboarding"]
            ob.update(status=status, updated_at=self.now().isoformat(),
                      **{k: v for k, v in info.items() if k != "connected_account"})
            if acct := info.get("connected_account"):
                self.records[hotel_id]["platform"]["connected_account"] = acct
            self._save()

    def _open(self, hotel_id: str) -> None:
        rec = self.records[hotel_id]
        try:
            ok = open_account(self.client, rec["name"], rec["onboarding"]["email"], rec["onboarding"]["request_id"],
                              rec["platform"].get("connected_account"),
                              step=lambda status, info: self._update(hotel_id, status, **info), sleep=self.sleep)
        except AirwallexError as e:
            self._update(hotel_id, "failed", error=f"Airwallex [{e.status}] {e.code}: {e.message}")
            self._record("hotel_onboarding_failed", hotel_id=hotel_id, error=str(e))
            return
        except Exception as e:   # a network error must not leave the sign-up stuck in "opening_account"
            self._update(hotel_id, "failed", error=f"{type(e).__name__}: {e}")
            return
        if not ok:
            self._update(hotel_id, "capabilities_pending",
                         error="Airwallex is still reviewing card acceptance; retry in a few minutes")
            return
        acct = rec["platform"]["connected_account"]
        self.hotels[hotel_id].connected_account = acct   # card payouts split to the hotel from now on
        self._update(hotel_id, "accepting_cards")
        self._record("hotel_onboarded", hotel_id=hotel_id, connected_account=acct)

    def status(self, hotel_id: str) -> dict:
        rec = self.records.get(hotel_id)
        if rec is None:
            return {"error": f"unknown hotel {hotel_id}"}
        ob = rec["onboarding"]
        return {"hotel_id": hotel_id, "name": rec["name"], "city": rec["city"], "status": ob["status"],
                "listed": hotel_id in self.hotels, "connected_account": rec["platform"].get("connected_account"),
                "error": ob.get("error"), "joined_at": ob.get("joined_at"),
                "rooms": [{"code": r["code"], "name": r["name"]} for r in rec["rooms"]]}

    def summary(self) -> list[dict]:
        """Every hotel on the marketplace and how far its payouts are set up (revenue desk)."""
        out = []
        for h in list(self.hotels.values()):
            if h.id in self.records:
                out.append(self.status(h.id))
            else:
                out.append({"hotel_id": h.id, "name": h.name, "city": h.city, "joined_at": None, "error": None,
                            "status": "accepting_cards" if h.connected_account else "no_payouts",
                            "connected_account": h.connected_account or None, "listed": True})
        return out
