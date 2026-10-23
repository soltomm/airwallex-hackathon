"""Writes ui/test/feed.json: the panel's events from the real desk, with scripted sellers instead of
the LLM (two hotels, a counter-offer round, a booking). Run from the repo root:
    .venv/bin/python ui/test/make_feed.py"""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path[:0] = ["src", "tests"]
from test_offer_desk import TERMS, FakeAirwallex, FakeClaude, text_turn, tool_turn  # noqa: E402

from agentbank.audit import AuditLog  # noqa: E402
from agentbank.bookings import BookingStore  # noqa: E402
from agentbank.ledger import OperationLedger  # noqa: E402
from agentbank.offer_desk import OfferDesk  # noqa: E402
from agentbank.offers import load_hotels  # noqa: E402

now = datetime.now(timezone.utc)
tmp = Path(tempfile.mkdtemp())
claude = FakeClaude()
desk = OfferDesk(load_hotels("data/hotels.json"), AuditLog(tmp / "a.jsonl"), OperationLedger(":memory:"),
                 BookingStore(tmp / "b.json"), api=FakeAirwallex(), client=claude, now=lambda: now)
inv = ("get_inventory", {"check_in": "2026-11-13", "check_out": "2026-11-15"})
pc = lambda **t: ("price_check", {**TERMS, **t})  # noqa: E731
mo = lambda **t: ("make_offer", {**TERMS, **t})  # noqa: E731

desk.search("2026-11-13", "2026-11-15", 2, ["breakfast", "late_checkout"], "free_until_48h")
claude.turns = [tool_turn(inv), tool_turn(pc(deposit_pct="0.2", total="500"), pc(deposit_pct="1", total="539")),
                tool_turn(mo(deposit_pct="1", total="539"), mo(deposit_pct="0.5", total="560")),
                text_turn("I can't reach **€500** with all three must-haves, but here are two options:\n"
                          "- **€539** with the full stay held on your card\n- **€560** with only half held\n"
                          "Both keep breakfast, late checkout and free cancellation until 48h before arrival.")]
r1 = desk.negotiate("htl_paris_marais", "Hello! I'm booking for 2 adults, 13–15 Nov 2026. Must-haves: breakfast for "
                    "both guests, late checkout, and free cancellation. Our budget is €500 total. What can you offer?")
claude.turns = [tool_turn(inv), tool_turn(mo(extras=["breakfast"], deposit_pct="1", total="546")),
                text_turn("We're nearly full on 15 Nov, so **late checkout isn't possible**. With breakfast and free "
                          "cancellation, the best I can do is **€546**. We're happy to keep your luggage after checkout.")]
desk.negotiate("htl_paris_opera", "Hello! 2 adults, 13–15 Nov 2026, breakfast, late checkout and free cancellation, "
               "€500 total budget.")
claude.turns = [tool_turn(pc(deposit_pct="1", total="520")), tool_turn(mo(deposit_pct="1", total="525")),
                text_turn("Since you're flexible on the deposit, I can meet you at **€525** with the full stay held "
                          "on your card. That's my final offer.")]
r3 = desk.negotiate("htl_paris_marais", "Thanks. We're fine with a full card hold, and we have a competing quote at "
                    "€546 without late checkout. Can you do €520?", r1["negotiation_id"])
desk.book(r3["offers"][0]["offer_id"], True, "card")
events = desk.feed_since(0)["events"]
Path("ui/test/feed.json").write_text(json.dumps(events, indent=1, ensure_ascii=False))
print(len(events), "events")
