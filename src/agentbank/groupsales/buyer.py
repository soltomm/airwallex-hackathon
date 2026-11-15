"""A scripted buyer for tests and randomized runs. Works only from its own private brief and the buyer
view of the deal.

`suggest` drafts the buyer's next move from the brief and `act` submits it through the same authenticated
interface a person uses (engine.buyer_action). The website doesn't use this module: there, a person types
every reply in the buyer's mailbox. The engine enforces no buyer-side approvals.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .calc import gbp
from .engine import Engine


def brief(engine: Engine, d: dict) -> dict | None:
    return engine.buyer_brief(d)


def change_due(engine: Engine, d: dict, b: dict) -> bool:
    """Whether the brief's later change (fewer rooms) is due: at a fixed scenario time, or a set number of
    scenario hours after the booking was confirmed (generated buyers, whose booking time isn't known upfront)."""
    ev = b.get("change_event")
    bk = d.get("booking")
    if not ev or not bk or bk.get("status") != "confirmed":
        return False
    if ev.get("after"):
        return engine.now() >= ev["after"]
    if ev.get("after_booked_hours") is not None and bk.get("confirmed_at"):
        due = datetime.fromisoformat(bk["confirmed_at"]) + timedelta(hours=float(ev["after_booked_hours"]))
        return datetime.fromisoformat(engine.now()) >= due
    return False


def suggest(engine: Engine, deal_id: str) -> dict | None:
    """The buyer's next move under its brief, or None when it should wait."""
    with engine.lock:
        d = engine.deal(deal_id)
        b = brief(engine, d)
        if not b:
            return None
        st = d["status"]
        cur = d["versions"][-1] if d["versions"] else None
        sent_actions = [m.get("action") for m in d["thread"] if m["from"] == "buyer"]
        if st == "needs_info":
            last_q = next((m for m in reversed(d["thread"]) if m["from"] == "seller" and m.get("questions")), None)
            # The brief only answers the intake questions (layout, payer, taxes): never for a booked stay, and
            # never the same answer twice. Anything else is for the person playing the buyer to answer.
            if last_q and d["thread"][-1] is last_q and not d.get("booking") and not any(
                    m["from"] == "buyer" and (m.get("payload") or {}).get("answers") for m in d["thread"]):
                k = b["known_answers"]
                parts = []
                if "meeting_layout" in k:
                    parts.append(f"{k['meeting_layout'].capitalize()} seating for the meeting room.")
                if "payer" in k:
                    parts.append(f"The legal payer is {k['payer']}.")
                if "budget_includes_taxes" in k:
                    parts.append("Our total budget is inclusive of taxes." if k["budget_includes_taxes"]
                                 else "Our budget excludes taxes.")
                if "Twin" in b.get("requirements", ""):
                    parts.append("Please confirm twin beds in the shared rooms and breakfast for 24 on all three mornings.")
                return {"action": "reply", "text": " ".join(parts), "payload": {"answers": k}}
            return None
        if st == "dates_proposed":
            last = d["thread"][-1] if d["thread"] else None
            opts = (last or {}).get("date_options") if (last or {}).get("from") == "seller" else None
            if opts:
                o = opts[0]   # the hotel lists its best match first, as the mailbox's primary button does
                return {"action": "reply", "text": f"{_span(o['arrival'], o['departure'])} works for us. Please send "
                                                   "the offer for those dates.",
                        "payload": {"dates": {"arrival": o["arrival"], "departure": o["departure"]}}}
            return None
        clar = _clarify_change(d)
        if clar:
            return clar
        if st == "offer_sent" and cur and cur["status"] == "sent" and cur["kind"] == "offer":
            if d["thread"][-1]["from"] == "buyer":
                return None
            obj = b.get("objectives") or {}
            want_att, want_rate = obj.get("attrition_pct"), obj.get("target_rate")
            need_att = bool(want_att and cur["terms"]["attrition_pct"] < want_att)
            need_rate = bool(want_rate and cur["terms"]["rate"] > want_rate)
            if (need_att or need_rate) and "counter" not in sent_actions:
                dep = obj.get("opening_deposit_pct", cur["terms"]["deposit_pct"])
                payload: dict = {}
                if need_rate:
                    parts = [f"Thank you for the offer. Our target is {gbp(want_rate)} per room-night, so we'd ask you "
                             "to meet that rate."]
                    payload["rate"] = want_rate
                else:
                    parts = [f"Thank you, the £{cur['terms']['rate'] // 100} room rate works for us."]
                if need_att:
                    parts.append(f"Attendance is still uncertain, so we need a {want_att}% attrition allowance.")
                    payload["attrition_pct"] = want_att
                parts.append(f"We propose a {dep}% deposit.")
                payload["deposit_pct"] = dep
                return {"action": "counter", "text": " ".join(parts), "payload": payload}
            if b.get("waits_for_organizer_after_quote"):
                if "message" not in sent_actions:
                    return {"action": "message", "text": "Thanks for the quote. I'm taking it to our organizer for "
                                                         "confirmation and will come back to you.", "payload": {}}
                return None
            return {"action": "accept", "text": f"We accept version {cur['n']}: total {gbp(cur['pricing']['total'])}, "
                                                f"deposit {gbp(cur['pricing']['deposit'])}.",
                    "payload": {"version": cur["n"]}}
        if st == "amendment_sent" and cur and cur["kind"] == "amendment" and cur["status"] == "sent":
            return {"action": "accept", "text": f"We accept amendment v{cur['n']}.", "payload": {"version": cur["n"]}}
        if st == "confirmed" and b.get("change_event") and "change_request" not in sent_actions:
            ev = b["change_event"]
            if change_due(engine, d, b):
                return {"action": "change_request", "text": ev["text"],
                        "payload": {"rooms_per_night": ev["release_rooms_per_night"]}}
        return None


def _span(a: str, b: str) -> str:
    x, y = date.fromisoformat(a), date.fromisoformat(b)
    return (f"{x.day}-{y.day} {y:%B %Y}" if (x.year, x.month) == (y.year, y.month)
            else f"{x.day} {x:%B} - {y.day} {y:%B %Y}")


def _clarify_change(d: dict) -> dict | None:
    """The hotel asked about the buyer's own open change request on a booked stay: restate it precisely, once.

    Built only from what the buyer already sent and the booking it can see, never from the private mandate. It is
    sent as a change_request so either seller can act on it."""
    b, th = d.get("booking"), d["thread"]
    if not b or b.get("status") != "confirmed" or not th:
        return None
    q = th[-1]
    if q["from"] != "seller" or not q.get("questions"):
        return None
    i = next((j for j in range(len(th) - 1, -1, -1) if th[j]["from"] == "buyer" and th[j].get("action") == "change_request"
              and not (th[j].get("payload") or {}).get("clarifies")), None)
    if i is None:
        return None
    req = th[i]
    if any(m["from"] == "seller" and m.get("version") for m in th[i + 1:]):
        return None   # an amendment already answered it
    if any((m.get("payload") or {}).get("clarifies") == req["id"] for m in th[i + 1:]):
        return None   # restated once already: anything further is for a person
    n = (req.get("payload") or {}).get("rooms_per_night")
    if not n:
        return None
    rooms = b.get("rooms") or {}
    per_night = sorted(set(rooms.values()))
    kind = (b.get("room_type") or "").strip()
    unit = f"{kind} room" if kind else "room"
    nights = sorted(rooms)
    if len(per_night) == 1 and per_night[0] > n:
        cur = per_night[0]
        ds = [date.fromisoformat(x) for x in nights]
        last = f"{ds[-1].day} {ds[-1]:%B %Y}"
        when = ", ".join(str(x.day) for x in ds[:-1]) + " and " + last if len(ds) > 1 else last
        text = (f"To confirm: please release {n} {unit}{'s' if n != 1 else ''} on each night, taking our block from "
                f"{cur} to {cur - n} {unit}s per night on {when}. We understand any room-nights beyond our remaining "
                "allowance are charged at the contracted rate.")
    else:
        text = (f"To confirm: please release {n} {unit}{'s' if n != 1 else ''} on each night of our stay. We understand "
                "any room-nights beyond our remaining allowance are charged at the contracted rate.")
    return {"action": "change_request", "text": text, "payload": {"rooms_per_night": n, "clarifies": req["id"]}}


def wants_reply(engine: Engine, deal_id: str) -> bool:
    """Whether it is the buyer's turn: a drafted move, or a hotel question the draft can't answer."""
    if suggest(engine, deal_id) is not None:
        return True
    with engine.lock:
        d = engine.deal(deal_id)
        if not brief(engine, d) or d["status"] in ("lost", "declined", "handoff"):
            return False
        last = d["thread"][-1] if d["thread"] else None
        return bool(last and last["from"] == "seller" and last.get("questions"))


def act(engine: Engine, deal_id: str, s: dict, actor: str = "buyer") -> dict:
    d = engine.deal(deal_id)
    return engine.buyer_action(deal_id, d["buyer_token"], s["action"], s.get("text", ""), s.get("payload") or {},
                               actor=actor)
