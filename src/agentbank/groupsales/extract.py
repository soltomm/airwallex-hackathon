"""Rule-based extraction of a group RFP into structured requirements.

Used by the rules seller and as a cross-check in Claude mode. It is deliberately literal: what it
can't find it lists as missing, and anything outside the hotel's supported services or dates is
flagged so the deal goes to a person.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
WORDS = {w: i for i, w in enumerate(
    ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
     "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"])}
UNSUPPORTED = {
    "dinner": "group dinner", "lunch": "lunch", "airport transfer": "airport transfers", "transfer": "transfers",
    "spa": "spa", "parking": "parking", "suite": "suites", "upgrade": "upgrades", "split bill": "split billing",
    "separate invoices": "split billing", "coach": "coach transport", "cocktail": "drinks reception",
}
NUM = r"(\d+|" + "|".join(WORDS) + r")"


def _n(s: str) -> int:
    s = s.lower()
    return WORDS[s] if s in WORDS else int(s)


def _date(day: str, month: str, year: int) -> str:
    return date(year, MONTHS[month.lower()], int(day)).isoformat()


def extract(subject: str, body: str, sender: str = "", default_year: int = 2027) -> dict:
    text = f"{subject}\n{body}"
    low = text.lower()
    req: dict = {"room_type": None, "rooms": None, "guests": None, "arrival": None, "departure": None,
                 "breakfast": False, "meeting": None, "budget_total": None, "budget_includes_taxes": None,
                 "target_per_room_night": None, "target_includes_breakfast": None, "payer": None,
                 "reply_by": None, "contact": None, "occupancy": None, "unsupported": [], "notes": []}

    m = re.search(NUM + r"\s+(?:single[- ]occupancy\s+|double[- ]occupancy\s+)?(twin|king|double|queen|single)\s+rooms?", low)
    if m:
        req["rooms"] = _n(m.group(1))
        rt = m.group(2)
        req["room_type"] = {"double": "king", "queen": "king"}.get(rt, rt)
        if rt == "single":
            req["unsupported"].append("single rooms")
            req["room_type"] = None
    if "single-occupancy" in low or "single occupancy" in low:
        req["occupancy"] = 1
    elif req["room_type"] == "twin":
        req["occupancy"] = 2
    m = re.search(r"for\s+" + NUM + r"\s+(?:guests|employees|people|colleagues|attendees)", low)
    if m:
        req["guests"] = _n(m.group(1))

    year = default_year
    y = re.search(r"\b(20\d\d)\b", text)
    if y:
        year = int(y.group(1))
    m = re.search(r"arriv\w*\s+(?:on\s+)?(\d{1,2})\s+([a-z]+)", low)
    if m and m.group(2) in MONTHS:
        req["arrival"] = _date(m.group(1), m.group(2), year)
    m = re.search(r"depart\w*\s+(?:on\s+)?(\d{1,2})\s+([a-z]+)", low)
    if m and m.group(2) in MONTHS:
        req["departure"] = _date(m.group(1), m.group(2), year)
    if not (req["arrival"] and req["departure"]):
        m = re.search(r"(\d{1,2})\s*[–-]\s*(\d{1,2})\s+([a-z]+)", low)
        if m and m.group(3) in MONTHS:
            req["arrival"] = _date(m.group(1), m.group(3), year)
            req["departure"] = _date(m.group(2), m.group(3), year)

    req["breakfast"] = "breakfast" in low and "no breakfast" not in low and "without breakfast" not in low

    if re.search(r"no meeting (room|space)", low):
        req["meeting"] = None
    elif "meeting room" in low or "meeting space" in low:
        mt = {"attendees": None, "date": None, "hours": None, "layout": None, "equipment": []}
        m = re.search(r"meeting (?:room|space) for\s+" + NUM, low)
        if m:
            mt["attendees"] = _n(m.group(1))
        m = re.search(r"meeting (?:room|space)[^.]*?\bon\s+(\d{1,2})\s+([a-z]+)", low)
        if m and m.group(2) in MONTHS:
            mt["date"] = _date(m.group(1), m.group(2), year)
        m = re.search(r"(\d{1,2}[:.]\d{2})\s*[–-]\s*(\d{1,2}[:.]\d{2})", text)
        if m:
            mt["hours"] = f"{m.group(1).replace('.', ':')}-{m.group(2).replace('.', ':')}"
        for lay in ("classroom", "theatre", "theater", "boardroom", "u-shape", "cabaret", "banquet"):
            if lay in low:
                mt["layout"] = "theatre" if lay == "theater" else lay
        mt["equipment"] = [e for e in ("projector", "microphone", "wi-fi", "screen", "flipchart") if e in low]
        req["meeting"] = mt

    m = re.search(r"£\s?([\d,]+)\s+total budget|total budget\s+(?:is\s+|of\s+)?£\s?([\d,]+)|budget\s+(?:is\s+|of\s+)?£\s?([\d,]+)", low)
    if m:
        req["budget_total"] = int((m.group(1) or m.group(2) or m.group(3)).replace(",", "")) * 100
        tail = low[m.end(): m.end() + 60]
        head = low[max(0, m.start() - 40): m.start()]
        if re.search(r"inclusive of tax|incl\.? (?:of )?tax|including tax|tax(es)? included", tail + head):
            req["budget_includes_taxes"] = True
        elif re.search(r"exclusive of tax|excl\.? tax|plus tax|\+ ?vat|ex vat", tail + head):
            req["budget_includes_taxes"] = False
    m = re.search(r"£\s?(\d+)\s+per room[/ ]night", low)
    if m:
        req["target_per_room_night"] = int(m.group(1)) * 100
        req["target_includes_breakfast"] = bool(re.search(r"per room[/ ]night\s+including breakfast", low))

    m = re.search(r"repl\w*\s+by\s+(\d{1,2})\s+([a-z]+)(?:\s+at\s+(noon|midday|\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?", low)
    if m and m.group(2) in MONTHS:
        hour = 12
        t = m.group(3) or "noon"
        if t not in ("noon", "midday"):
            hh = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
            hour = int(hh.group(1)) + (12 if hh.group(3) == "pm" and int(hh.group(1)) < 12 else 0)
        # London is on GMT in January; the demo only uses winter deadlines.
        req["reply_by"] = datetime(year, MONTHS[m.group(2)], int(m.group(1)), hour, tzinfo=timezone.utc).isoformat()

    m = re.search(r"([A-Z][A-Za-z&]+(?:\s+[A-Z][A-Za-z&]+)*\s+(?:Demo\s+)?(?:Ltd|Limited|LLP|plc|Inc|GmbH|LLC))\b", body)
    if m:
        req["payer"] = m.group(1).strip()

    m = re.search(r"(?:thanks|regards|best),?\s+([A-Z][a-z]+)", body, re.I)
    if m:
        req["contact"] = m.group(1)
    elif sender:
        req["contact"] = sender.split("<")[0].strip() or None

    for k, label in UNSUPPORTED.items():
        if re.search(r"\b" + re.escape(k) + r"\b", low) and label not in req["unsupported"]:
            req["unsupported"].append(label)
    return req


def missing_info(req: dict) -> list[str]:
    """Questions the seller must ask before it can quote firmly."""
    out = []
    if not req.get("room_type") or not req.get("rooms"):
        out.append("room type and number of rooms")
    if not (req.get("arrival") and req.get("departure")):
        out.append("arrival and departure dates")
    if req.get("meeting"):
        if not req["meeting"].get("layout"):
            out.append("meeting room layout (classroom, boardroom, theatre...)")
        if not req["meeting"].get("date"):
            out.append("meeting date")
    if not req.get("payer"):
        out.append("legal name of the company paying (the payer on the contract)")
    if req.get("budget_total") and req.get("budget_includes_taxes") is None:
        out.append("whether the budget includes taxes")
    return out


def stay_nights(arrival: str, departure: str) -> list[str]:
    a, b = date.fromisoformat(arrival), date.fromisoformat(departure)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days)]
