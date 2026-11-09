"""The hotel's bookable calendar, expanded from the compact form in the scenario.

The scenario says which nights are on sale and the defaults, and lists only the exceptions:

    "bookable": {"first_night": "2027-01-11", "last_night": "2027-12-31"},
    "availability": {"twin": 20, "king": 20},                      # group rooms per night by default
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
from datetime import date, timedelta


def _days(first: str, last: str) -> list[str]:
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


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
    h["capacity"] = {n: {rt: int(by_night.get(n, {}).get(rt, cap)) for rt, cap in h["availability"].items()}
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
