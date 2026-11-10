"""The hotel's bookable calendar, expanded from the compact form in the scenario.

The scenario says which nights are on sale and the defaults, and lists only the exceptions:

    "bookable": {"first_night": "2027-01-11", "last_night": "2027-12-31"},
    "availability": {"twin": 20, "king": 20},                      # group rooms per night by default
    "background_demand": {"seed": 2027, ...},                      # optional: rooms other business has taken
    "availability_by_night": {"2027-03-10": {"twin": 14, ...}},    # nights that differ from the default
    "meeting_rooms": {"thames": {"hours": "08:00-18:00", "open_weekdays": [0, 1, 2, 3, 4],
                                 "available": {"2027-12-24": null}}}  # date overrides; null = closed

`expand()` adds the derived fields the engine and the checks read:

    hotel["nights"]              every bookable night, in order
    hotel["mornings"]            the breakfast morning after each of those nights
    hotel["capacity"]            {night: {room_type: rooms}}
    meeting_room["available"]    {date: hours} for every open day inside the window
"""
from __future__ import annotations

import copy
import random
from datetime import date, timedelta


def _days(first: str, last: str) -> list[str]:
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def background_demand(nights: list[str], allocation: dict, spec: dict) -> dict:
    """Group rooms already taken by other business on each night, from a seeded pattern, so the calendar
    looks like a working hotel's. The seed makes it the same on every run (tests and the demo rely on that).

    spec: {"seed": int, "by_month": {"1": share, ...}, "by_weekday": [Mon..Sun adjustments],
           "noise": max +/- share, "sold_out_spans": [count, min_nights, max_nights]}
    Returns {night: {room_type: rooms still on sale}}.
    """
    rng = random.Random(spec["seed"])
    sold_out: set[str] = set()
    count, lo, hi = spec.get("sold_out_spans", [0, 0, 0])
    for _ in range(count):                    # conferences and events that fill the hotel
        start = rng.randrange(len(nights))
        sold_out.update(nights[start:start + rng.randint(lo, hi)])
    out = {}
    for n in nights:
        dt = date.fromisoformat(n)
        base = spec["by_month"][str(dt.month)] + spec["by_weekday"][dt.weekday()]
        out[n] = {}
        for rt, cap in allocation.items():
            taken = 1.0 if n in sold_out else min(1.0, max(0.0, base + rng.uniform(-spec["noise"], spec["noise"])))
            out[n][rt] = cap - round(cap * taken)
    return out


def expand(scenario: dict) -> dict:
    sc = copy.deepcopy(scenario)
    h = sc["hotel"]
    win = h["bookable"]
    nights = _days(win["first_night"], win["last_night"])
    if not nights:
        raise ValueError("bookable window is empty: last_night is before first_night")
    by_night = h.get("availability_by_night") or {}
    unknown = sorted(n for n in by_night if n not in set(nights))
    if unknown:
        raise ValueError(f"availability_by_night lists nights outside the bookable window: {', '.join(unknown)}")
    h["nights"] = nights
    h["mornings"] = [(date.fromisoformat(n) + timedelta(days=1)).isoformat() for n in nights]   # breakfast days
    # Default allocation, less what other business has taken (if the scenario models it), then the
    # nights the scenario lists explicitly (the demo stay) exactly as given.
    on_sale = (background_demand(nights, h["availability"], h["background_demand"]) if h.get("background_demand")
               else {n: dict(h["availability"]) for n in nights})
    h["capacity"] = {n: {rt: int(by_night.get(n, {}).get(rt, on_sale[n][rt])) for rt in h["availability"]}
                     for n in nights}
    # A meeting can be on any day of the stay, including the departure day.
    days = nights + [(date.fromisoformat(nights[-1]) + timedelta(days=1)).isoformat()]
    for mr in h.get("meeting_rooms", {}).values():
        overrides = mr.get("available") or {}
        weekdays = set(mr.get("open_weekdays", []))
        avail = {d: mr["hours"] for d in days if date.fromisoformat(d).weekday() in weekdays}
        for d, hours in overrides.items():
            if hours is None:
                avail.pop(d, None)
            elif d in days:
                avail[d] = hours
        mr["available"] = avail
    return sc


def window_text(hotel: dict) -> str:
    """'11 January 2027 to 31 December 2027' style text for prompts and the rules page."""
    def f(s: str) -> str:
        x = date.fromisoformat(s)
        return f"{x.day} {x.strftime('%B %Y')}"
    return f"nights of {f(hotel['bookable']['first_night'])} to {f(hotel['bookable']['last_night'])}"
