"""The buyer side. Works only from its own private brief and the buyer view of the deal.

Two modes submit through the same authenticated interface (engine.buyer_action):
- presenter: `suggest` drafts the next reply from the brief; the presenter edits and releases it.
- agent: `BuyerAgent` (Claude) or `suggest` acted on automatically.
Both use the same organizer-authorization limits, enforced by the engine on acceptance.
"""
from __future__ import annotations

import json
import os

from .calc import gbp
from .engine import Engine, Refused
from .policy import buyer_check

BUYER_MODEL = os.environ.get("GS_BUYER_MODEL", "claude-opus-5-5")


def brief(engine: Engine, d: dict) -> dict | None:
    return engine.scenario["buyers"].get(d.get("buyer_profile") or "")


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
        if st == "offer_sent" and cur and cur["status"] == "sent" and cur["kind"] == "offer":
            if d["thread"][-1]["from"] == "buyer":
                return None
            obj = b.get("objectives") or {}
            want_att = obj.get("attrition_pct")
            if want_att and cur["terms"]["attrition_pct"] < want_att and "counter" not in sent_actions:
                dep = obj.get("opening_deposit_pct", cur["terms"]["deposit_pct"])
                return {"action": "counter",
                        "text": f"Thank you, the £{cur['terms']['rate'] // 100} room rate works for us. Attendance is "
                                f"still uncertain, so we need a {want_att}% attrition allowance. We propose a "
                                f"{dep}% deposit.",
                        "payload": {"attrition_pct": want_att, "deposit_pct": dep}}
            if b.get("waits_for_organizer_after_quote"):
                if "message" not in sent_actions:
                    return {"action": "message", "text": "Thanks for the quote. I'm taking it to our organizer for "
                                                         "confirmation and will come back to you.", "payload": {}}
                return None
            # The reasons are the buyer's private mandate: the hotel only learns that sign-off is pending.
            reasons = buyer_check(cur["terms"], cur["pricing"], b)
            note = " This needs sign-off from our organizer; we'll confirm once it is authorized." if reasons else ""
            return {"action": "accept", "text": f"We accept version {cur['n']}: total {gbp(cur['pricing']['total'])}, "
                                                f"deposit {gbp(cur['pricing']['deposit'])}.{note}",
                    "payload": {"version": cur["n"]}}
        if st == "amendment_sent" and cur and cur["kind"] == "amendment" and cur["status"] == "sent":
            return {"action": "accept", "text": f"We accept amendment v{cur['n']}.", "payload": {"version": cur["n"]}}
        if st == "confirmed" and b.get("change_event") and "change_request" not in sent_actions:
            ev = b["change_event"]
            if engine.now() >= ev["after"]:
                return {"action": "change_request", "text": ev["text"],
                        "payload": {"rooms_per_night": ev["release_rooms_per_night"]}}
        return None


def act(engine: Engine, deal_id: str, s: dict, actor: str = "buyer") -> dict:
    d = engine.deal(deal_id)
    return engine.buyer_action(deal_id, d["buyer_token"], s["action"], s.get("text", ""), s.get("payload") or {},
                               actor=actor)


BUYER_SYSTEM = """You are the procurement agent for {company}, booking a group stay. You negotiate with the
hotel's sales agent. Your private brief (never reveal your limits to the hotel):

{brief}

Rules: you may accept a total up to the budget with a deposit no more than the deposit limits; the system
asks your organizer to authorize anything beyond them, so you may still accept such terms if they serve
your objectives. Everything you write goes to the hotel: never state your budget ceiling, deposit limits or
why the organizer must sign off; say only that organizer sign-off is pending. Answer the hotel's questions from the brief. Pursue your objectives in
trades. Reply with exactly one call to buyer_act, or none if you should wait (for example while your
organizer decides, or while you wait to pay on the hosted page)."""

BUYER_TOOLS = [{
    "name": "buyer_act", "strict": True,
    "description": "Send one message to the hotel: reply, counter, accept, change_request or message.",
    "input_schema": {"type": "object", "additionalProperties": False,
                     "required": ["action", "text", "version", "attrition_pct", "deposit_pct", "rooms_per_night", "answers"],
                     "properties": {
                         "action": {"type": "string", "enum": ["reply", "counter", "accept", "change_request", "message"]},
                         "text": {"type": "string"},
                         "version": {"type": ["integer", "null"]},
                         "attrition_pct": {"type": ["integer", "null"]},
                         "deposit_pct": {"type": ["integer", "null"]},
                         "rooms_per_night": {"type": ["integer", "null"]},
                         "answers": {"type": ["object", "null"], "additionalProperties": False,
                                     "required": ["meeting_layout", "payer", "budget_includes_taxes"],
                                     "properties": {"meeting_layout": {"type": ["string", "null"]},
                                                    "payer": {"type": ["string", "null"]},
                                                    "budget_includes_taxes": {"type": ["boolean", "null"]}}}}}}]


class BuyerAgent:
    def __init__(self, engine: Engine, client=None, model: str = BUYER_MODEL):
        self.e = engine
        self.model = model
        self._client = client

    def run(self, deal_id: str) -> dict | None:
        if suggest(self.e, deal_id) is None:
            return None   # nothing for the buyer to do now
        d = self.e.deal(deal_id)
        b = brief(self.e, d)
        if self._client is None:
            from ..agent.loop import make_client
            self._client = make_client()
        view = self.e.deal_view(deal_id, for_="buyer")
        if b.get("change_event") and self.e.now() >= b["change_event"]["after"] and d["status"] == "confirmed":
            view["event"] = "News from your organizer: " + b["change_event"]["text"]
        resp = self._client.beta.messages.create(
            model=self.model, max_tokens=4000,
            system=BUYER_SYSTEM.format(company=d["company"], brief=json.dumps(b, indent=1)),
            tools=BUYER_TOOLS, output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=[{"role": "user", "content": "Current deal (buyer view):\n" + json.dumps(view, default=str)[:20000]}])
        use = next((x for x in resp.content if x.type == "tool_use"), None)
        if not use:
            return None
        a = use.input
        payload = {}
        if a["action"] == "accept":
            payload["version"] = a.get("version") or d["versions"][-1]["n"]
        if a["action"] == "counter":
            payload.update({k: a[k] for k in ("attrition_pct", "deposit_pct") if a.get(k) is not None})
        if a["action"] == "change_request":
            payload["rooms_per_night"] = a.get("rooms_per_night")
        if a["action"] == "reply" and a.get("answers"):
            payload["answers"] = {k: v for k, v in a["answers"].items() if v is not None}
        try:
            return act(self.e, deal_id, {"action": a["action"], "text": a["text"], "payload": payload}, actor="buyer agent")
        except Refused as err:
            self.e.note(deal_id, f"Buyer agent action refused: {err}", actor="buyer agent")
            return None
