"""RFP generator: realistic group inquiries, each with a private brief for the buyer agent.

The generator only writes the inbound: the RFP email and the buyer's brief (answers, goals, limits, a later
change). Everything after that is the real system: the seller agent reads the email, the buyer agent
answers from the brief, and the engine enforces every rule. It never changes deal state or takes a
shortcut on the seller's side, and the seller never sees the brief.

Inquiries are described by traits (dates that fit, nights short of rooms, or dates too soon to sell; price
pressure; deposit limit; payer country; missing details; a later reduction). Presets are named trait combinations. The same seed and
traits give the same RFP and brief, against the same calendar.

    gen = generate(engine, preset="dates_short_haggle", seed=42)
    result = send(engine, gen)          # registers the brief, delivers the email, records the payer
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from . import calc, fx
from .engine import Engine, Refused
from .extract import stay_nights
from .policy import LAYOUTS_ON_FILE

PRESETS: dict[str, dict] = {
    # Rooms free, the quote fits the budget: accept and pay.
    "straight_booking": {"availability": "fits", "price": "comfortable"},
    # Asks for dates too soon to sell to a group; the seller offers other dates and the buyer takes one; the
    # buyer haggles below the seller's floor, and the budget is tight enough that its organizer must sign off.
    "dates_short_haggle": {"availability": "too_soon", "price": "below_floor"},
    # The full money story: attrition traded for a bigger deposit (manager approval), a deposit above the
    # buyer's own limit (organizer sign-off), payment, then fewer rooms and a partial refund.
    "full_lifecycle": {"room_type": "twin", "attrition": 20, "deposit": "low", "meeting": True, "change_rooms": 2},
    # A payer abroad: the deposit's currency side, then a smaller block and a refund.
    "payer_abroad": {"payer_country": "DE", "change_rooms": 1},
    # The RFP leaves out who pays and whether the budget includes taxes: the seller has to ask.
    "missing_details": {"missing": ["payer", "taxes"]},
}

# What each preset shows, for the dashboard's New inquiry drawer.
PRESET_INFO: dict[str, dict] = {
    "straight_booking": {"title": "Straight booking",
                         "shows": "Rooms free and the quote fits the budget: accepted, deposit paid on Airwallex, the "
                                  "hotel's share paid out."},
    "dates_short_haggle": {"title": "Dates unavailable, then a haggle",
                           "shows": "Asks for dates too soon to sell; the seller offers others. The buyer counters below "
                                    "the seller's floor, the floor holds, and the buyer's organizer signs off."},
    "full_lifecycle": {"title": "Full lifecycle with a refund",
                       "shows": "Attrition traded for a bigger deposit (manager approval), organizer sign-off, deposit "
                                "paid, then fewer rooms: partial refund and the hotel's payout share reversed."},
    "payer_abroad": {"title": "Payer abroad",
                     "shows": "A company in Germany paying from EUR, then a smaller block and a refund."},
    "missing_details": {"title": "Missing details",
                        "shows": "The RFP leaves out who pays and whether the budget includes taxes: the seller asks, "
                                 "the buyer answers from its brief."},
}

TRAITS: dict[str, tuple] = {
    "room_type": ("twin", "king"),
    # fits: rooms free on the dates. short_rooms: fewer rooms free than asked on some night (the seller quotes
    # what it has). too_soon: less notice than the hotel needs for a group (the seller offers other dates).
    "availability": ("fits", "short_rooms", "too_soon"),
    "price": ("comfortable", "tight", "below_floor"),
    "deposit": ("standard", "low"),
    "missing": ("payer", "taxes", "layout"),
}
DEFAULTS = {"room_type": None, "rooms": None, "nights": None, "availability": "fits", "price": "comfortable",
            "deposit": "standard", "attrition": None, "meeting": False, "breakfast": True, "missing": [],
            "payer_country": None, "payer_currency": None, "change_rooms": 0, "change_after_hours": 168}

# Company suffixes the extractor recognises as a legal payer.
SUFFIX = {"GB": "Ltd", "DE": "GmbH", "US": "Inc", "AU": "Pty Ltd", "SG": "Pte Ltd", "IE": "Ltd", "NL": "Ltd",
          "FR": "Ltd", "JP": "Ltd"}
COMPANIES = ["Brightwave Studios", "Kestrel Logistics", "Lumen Health", "Orchard Robotics", "Fieldnote Labs",
             "Harrow Capital", "Tidewater Media", "Quill Analytics", "Beacon Bio", "Northwind Retail",
             "Copperleaf Energy", "Saltmarsh Design"]
PEOPLE = [("Priya", "Shah"), ("Tom", "Okafor"), ("Lena", "Fischer"), ("Marco", "Rossi"), ("Aisha", "Rahman"),
          ("Sam", "Whitaker"), ("Hana", "Kim"), ("Owen", "Price"), ("Clara", "Moreau"), ("Dev", "Patel")]
EVENTS = ["team offsite", "sales kickoff", "leadership retreat", "product launch", "board away day",
          "engineering summit"]


def resolve(traits: dict | None = None, preset: str | None = None) -> dict:
    """Merge a preset and explicit traits over the defaults, and check every value."""
    if preset is not None and preset not in PRESETS:
        raise Refused(f"unknown preset {preset!r}; choose one of {', '.join(PRESETS)}")
    t = {**DEFAULTS, **(PRESETS.get(preset) or {}), **(traits or {})}
    unknown = set(t) - set(DEFAULTS)
    if unknown:
        raise Refused(f"unknown traits: {', '.join(sorted(unknown))}")
    for k in ("availability", "price", "deposit"):
        if t[k] not in TRAITS[k]:
            raise Refused(f"{k} must be one of {', '.join(TRAITS[k])}")
    if t["room_type"] is not None and t["room_type"] not in TRAITS["room_type"]:
        raise Refused("room_type must be twin or king")
    t["missing"] = list(t["missing"] or [])
    if any(m not in TRAITS["missing"] for m in t["missing"]):
        raise Refused(f"missing can list {', '.join(TRAITS['missing'])}")
    for k, lo, hi in (("rooms", 2, 20), ("nights", 1, 5), ("change_rooms", 0, 10), ("attrition", 0, 30),
                      ("change_after_hours", 1, 24 * 60)):
        if t[k] is not None:
            try:
                t[k] = int(t[k])
            except (TypeError, ValueError):
                raise Refused(f"{k} must be a whole number")
            if not lo <= t[k] <= hi:
                raise Refused(f"{k} must be between {lo} and {hi}")
    if t["attrition"] == 0:
        t["attrition"] = None
    if t["payer_country"]:
        try:
            fx.payer_profile(t["payer_country"], t["payer_currency"])
        except ValueError as e:
            raise Refused(str(e))
    return t


def generate(engine: Engine, traits: dict | None = None, preset: str | None = None, seed: int | None = None) -> dict:
    """A new inquiry for the current calendar: {seed, traits, from, subject, body, domain, brief, payer}."""
    t = resolve(traits, preset)
    if seed is None:
        seed = random.SystemRandom().randrange(1, 10**6)
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        raise Refused("seed must be a whole number")
    rng = random.Random(seed)
    rt = t["room_type"] or rng.choice(TRAITS["room_type"])
    nights_n = t["nights"] or rng.randint(2, 4)
    lead_min = 60 if t["change_rooms"] else 45      # a later reduction needs time before the first installment
    occ = engine.hotel["room_types"][rt]["max_occupancy"]
    room_cap = min(engine.hotel["availability"][rt], 20)
    if t["meeting"]:   # everyone fits in the meeting room's classroom layout
        room_cap = min(room_cap, engine.hotel["meeting_rooms"]["thames"]["capacity"]["classroom"] // occ)
    lo = 4
    if t["change_rooms"]:
        # The later reduction stays inside the free allowance, so it ends in a refund: the allowance is
        # floor(attrition% of room-nights), with the hotel's standard attrition unless the buyer wins more.
        att = t["attrition"] or engine.policy["attrition"]["standard_pct"]
        lo = max(lo, -(-t["change_rooms"] * 100 // att))
    if lo > room_cap:
        raise Refused(f"a {t['change_rooms']}-room reduction inside the allowance needs at least {lo} rooms; "
                      f"at most {room_cap} fit; ask for attrition or a smaller change")
    if t["availability"] == "short_rooms":
        arrival, rooms = _short_window(engine, rng, rt, nights_n, t["rooms"])
    elif t["availability"] == "too_soon":
        rooms = t["rooms"] or rng.randint(lo, min(room_cap, lo + 6))
        arrival = _too_soon(engine, rng, nights_n)
    else:
        if not t["rooms"]:
            # Ask for no more rooms than some window in the live calendar has free (bookings already hold some).
            most = _most_free(engine, rt, nights_n, lead_min, t["meeting"])
            if most < lo:
                raise Refused(f"no {nights_n}-night stay with {lo} {rt} rooms free in the next months; try fewer rooms")
        rooms = t["rooms"] or rng.randint(lo, min(room_cap, lo + 6, most))
        arrival = _fitting_window(engine, rng, rt, nights_n, rooms, lead_min, t["meeting"])
    nights = stay_nights(arrival, _plus(arrival, nights_n))
    departure = _plus(arrival, nights_n)
    if t["change_rooms"] and t["change_rooms"] >= rooms:
        raise Refused(f"change_rooms ({t['change_rooms']}) must be fewer than the {rooms} rooms booked")

    guests = rooms * occ
    meeting = None
    if t["meeting"]:
        day = _meeting_day(engine, nights)
        if not day:
            raise Refused("no free meeting-room day in that stay; try another seed")
        meeting = {"room": "thames", "date": day, "layout": rng.choice(sorted(LAYOUTS_ON_FILE)), "attendees": guests}

    # Price pressure, from the hotel's own rate bands for this room type.
    bands = engine.rms["rates"][rt]
    bf_unit = engine.hotel["breakfast"]["price_per_person"]
    target_rate = None
    if t["price"] == "tight":
        target_rate = bands["autonomous_floor"]
    elif t["price"] == "below_floor":
        target_rate = _round(bands["absolute_min"] + (bands["autonomous_floor"] - bands["absolute_min"]) // 2, 500)

    def estimate(rate: int) -> int:
        terms = {"room_type": rt, "rate": rate, "rooms": {n: rooms for n in nights}, "released": {n: 0 for n in nights},
                 "breakfast": {_plus(n, 1): guests for n in nights} if t["breakfast"] else {},
                 "meeting": meeting, "attrition_pct": 0, "deposit_pct": 0, "currency": "GBP"}
        return calc.price(terms, engine.hotel, engine.rms)["total"]

    if t["price"] == "comfortable":
        budget = _round_up(estimate(bands["target"]) * 115 // 100, 50000)
    elif t["price"] == "tight":
        budget = _round_up(estimate(bands["autonomous_floor"]), 10000)
    else:   # below the floor: covers the buyer's target but not the hotel's floor, so its organizer must sign off
        want, floor = estimate(target_rate), estimate(bands["autonomous_floor"])
        budget = max(_round((want + floor) // 2, 10000), _round_up(want, 1000))
        if budget >= floor:
            budget = floor - 100
    dep_pct = 25 if t["deposit"] == "low" else 40
    per_night = (target_rate or bands["target"]) + (bf_unit * occ if t["breakfast"] else 0)

    # Who is asking. The domain is unique per seed, so a replay never collides with the duplicate check.
    country = (t["payer_country"] or "GB").upper()
    company = rng.choice(COMPANIES)
    first, last = rng.choice(PEOPLE)
    domain = f"{company.lower().replace(' ', '-')}.r{seed}.example"
    sender = f"{first} {last} <{first.lower()}@{domain}>"
    payer = f"{company} Demo {SUFFIX.get(country, 'Ltd')}"
    event = rng.choice(EVENTS)
    reply_by = _plus(engine.now()[:10], 2)

    body = _email(first, guests, rooms, rt, arrival, departure, t, meeting, per_night, budget, payer, country,
                  reply_by, rng)
    subject = f"RFP | {event.title()} | {guests} guests | {_span(arrival, departure)}"

    brief = {
        "organizer": f"{first} ({company}), with their finance lead as authorizer",
        "known_answers": {"payer": payer, "budget_includes_taxes": True,
                          **({"meeting_layout": meeting["layout"]} if meeting else {})},
        "requirements": f"{rooms} {rt} rooms a night for {guests} guests"
                        + (", breakfast each morning" if t["breakfast"] else "")
                        + (f", meeting room on {meeting['date']}" if meeting else "") + ".",
        "accommodation_target_per_room_night_incl_breakfast": per_night,
        "budget_total": budget, "deposit_limit": budget * dep_pct // 100, "deposit_limit_pct": dep_pct,
        "objectives": {**({"attrition_pct": t["attrition"]} if t["attrition"] else {}),
                       **({"opening_deposit_pct": 25} if t["attrition"] or t["deposit"] == "low" else {}),
                       **({"target_rate": target_rate} if target_rate else {})},
        "flexibility": "May trade a larger deposit for flexibility; anything beyond the limits needs organizer approval.",
        "accepts_fixed_terms": True,
        "mandate_allows_reduction_within_allowance": True,
        "generated": {"seed": seed, "traits": t},
    }
    if t["change_rooms"]:
        n = t["change_rooms"]
        lost = n * occ
        brief["change_event"] = {
            "after_booked_hours": t["change_after_hours"], "release_rooms_per_night": n,
            "text": f"{_word(lost).capitalize()} colleague{'s' if lost != 1 else ''} can no longer attend. Please reduce "
                    f"our block by {_word(n)} {rt} room{'s' if n != 1 else ''} for all {_word(nights_n)} nights and "
                    "return any excess deposit under our agreement."}
    return {"seed": seed, "preset": preset, "traits": t, "from": sender, "subject": subject, "body": body, "domain": domain,
            "brief": brief,
            "payer": {"country": country, "currency": t["payer_currency"]} if t["payer_country"] else None}


def send(engine: Engine, gen: dict) -> dict:
    """Deliver a generated inquiry: register its brief, then the email, then the payer's country if abroad."""
    engine.register_buyer_brief(gen["domain"], gen["brief"])
    r = engine.ingest_email(gen["from"], gen["subject"], gen["body"])
    if gen.get("payer"):
        engine.update_payer(r["deal_id"], gen["payer"]["country"], gen["payer"]["currency"], actor="generator")
    engine.mark_generated(r["deal_id"], gen.get("preset"), gen["seed"])
    return {**r, "seed": gen["seed"], "preset": gen.get("preset"), "traits": gen["traits"],
            "rfp": {"from": gen["from"], "subject": gen["subject"], "body": gen["body"]}}


# ---------------------------------------------------------------------------------------------- helpers
def _email(first, guests, rooms, rt, arrival, departure, t, meeting, per_night, budget, payer, country, reply_by, rng):
    a, d = date.fromisoformat(arrival), date.fromisoformat(departure)
    parts = [f"Hello Group Sales, please quote {rooms} {rt} rooms for {guests} colleagues arriving {a.day} "
             f"{a:%B} and departing {d.day} {d:%B %Y}"
             + (", with breakfast each morning." if t["breakfast"] else ", without breakfast.")]
    if meeting:
        m = date.fromisoformat(meeting["date"])
        layout = "" if "layout" in t["missing"] else f" in {meeting['layout']} layout"
        parts.append(f"We need a private meeting room for {guests} people on {m.day} {m:%B}, 09:00-17:00{layout}, "
                     "with projector and Wi-Fi.")
    else:
        parts.append("No meeting room is needed.")
    parts.append(f"Our target is £{per_night // 100} per room/night"
                 + (" including breakfast" if t["breakfast"] else "") + ", and our total budget is "
                 f"£{budget // 100:,}" + ("." if "taxes" in t["missing"] else " inclusive of taxes."))
    if "payer" not in t["missing"]:
        abroad = f", from our office in {fx.payer_profile(country, t['payer_currency'])['country']}" \
            if t["payer_country"] else ""
        parts.append(f"The package is billed to {payer}{abroad}; guests cover incidentals.")
    else:
        parts.append("Guests cover their own incidentals.")
    r = date.fromisoformat(reply_by)
    parts.append(f"Please reply by {r.day} {r:%B} at noon London time. {rng.choice(['Thanks', 'Best regards', 'Regards'])}, "
                 f"{first}")
    return " ".join(parts)


def _fitting_window(engine: Engine, rng: random.Random, rt: str, n: int, rooms: int, lead_min: int,
                    meeting: bool) -> str:
    today = date.fromisoformat(engine.now()[:10])
    options = []
    for k in range(lead_min, lead_min + 150):
        a = (today + timedelta(days=k)).isoformat()
        nights = stay_nights(a, _plus(a, n))
        if all(_free(engine, x, rt) >= rooms for x in nights) and (not meeting or _meeting_day(engine, nights)):
            options.append(a)
    if not options:
        raise Refused(f"no {n}-night stay with {rooms} {rt} rooms free in the next months; try fewer rooms")
    return rng.choice(options)


def _most_free(engine: Engine, rt: str, n: int, lead_min: int, meeting: bool) -> int:
    """The most rooms of this type free on every night of some n-night stay that _fitting_window would search."""
    today = date.fromisoformat(engine.now()[:10])
    best = 0
    for k in range(lead_min, lead_min + 150):
        a = (today + timedelta(days=k)).isoformat()
        nights = stay_nights(a, _plus(a, n))
        free = [_free(engine, x, rt) for x in nights]
        if free and None not in free and (not meeting or _meeting_day(engine, nights)):
            best = max(best, min(free))
    return best


def _too_soon(engine: Engine, rng: random.Random, n: int) -> str:
    """An arrival inside the hotel's minimum notice for groups, in the calendar (so it isn't a different problem)."""
    today = date.fromisoformat(engine.now()[:10])
    min_lead = engine.policy["min_days_before_arrival"]
    options = [a for a in ((today + timedelta(days=k)).isoformat() for k in range(7, min_lead))
               if a in engine.hotel["capacity"] and _plus(a, n - 1) in engine.hotel["capacity"]]
    if not options:
        raise Refused("the calendar has no nights inside the minimum notice period right now")
    return rng.choice(options)


def _short_window(engine: Engine, rng: random.Random, rt: str, n: int, rooms: int | None) -> tuple[str, int]:
    """Nights where fewer rooms are free than the group asks for, with other dates nearby that do fit."""
    today = date.fromisoformat(engine.now()[:10])
    min_lead = engine.policy["min_days_before_arrival"]
    last = engine.hotel["bookable"]["last_night"]
    options = []
    for k in range(min_lead + 1, 366):
        a = (today + timedelta(days=k)).isoformat()
        nights = stay_nights(a, _plus(a, n))
        if nights[-1] > last:
            break
        free = [_free(engine, x, rt) for x in nights]
        if None in free:
            continue
        cap = min(engine.hotel["availability"][rt], max(free) + 4)
        lo = min(free) + 1
        want = rooms if rooms is not None else None
        if want is not None and not (want > min(free)):
            continue
        hi = want if want is not None else min(cap, lo + 3)
        if hi < lo or hi < 2:
            continue
        # Other dates within two weeks that would take the whole group.
        if any(all((_free(engine, x, rt) or 0) >= hi for x in stay_nights(b, _plus(b, n)))
               for b in (_plus(a, j) for j in range(-14, 15) if j)
               if engine.lead_days(b) >= min_lead and _plus(b, n - 1) <= last):
            options.append((a, (lo, hi)))
    if not options:
        raise Refused("no nights are short of rooms in the calendar right now; use availability 'fits'")
    a, (lo, hi) = rng.choice(options)
    return a, (rooms if rooms is not None else rng.randint(lo, hi))


def _free(engine: Engine, night: str, rt: str) -> int | None:
    inv = engine.inventory_at(night)
    if not inv:
        return None
    x = inv[rt]
    return x["capacity"] - x["held"] - x["booked"]


def _meeting_day(engine: Engine, nights: list[str]) -> str | None:
    """The first stay day the Thames Room is open and free (from the second day, so guests have arrived)."""
    days = nights[1:] + [_plus(nights[-1], 1)] if len(nights) > 1 else [nights[0]]
    with engine.lock:
        for day in days:
            slot = engine._slot(day)
            if slot and slot["status"] == "free":
                return day
    return None


def _plus(d: str, days: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


def _span(a: str, b: str) -> str:
    x, y = date.fromisoformat(a), date.fromisoformat(b)
    return f"{x.day}–{y.day} {y:%B %Y}" if (x.year, x.month) == (y.year, y.month) else f"{x.day} {x:%B}–{y.day} {y:%B %Y}"


def _round(p: int, step: int) -> int:
    return p // step * step


def _round_up(p: int, step: int) -> int:
    return -(-p // step) * step


def _word(n: int) -> str:
    words = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
    return words[n] if 0 <= n < len(words) else str(n)
