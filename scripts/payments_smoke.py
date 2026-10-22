"""End-to-end payment check without the LLMs: commit a Marais offer, book it, wait for payment.

    python scripts/payments_smoke.py card     # then open the printed /pay link and use the test card
    python scripts/payments_smoke.py metal    # then pay the printed URL with the traveller's mandate:
        cd metal && npx metal-agent pay <url> --mandate ../var/metal/traveller-mandate.json --network testnet

Serves the same checkout pages and Metal callbacks as hotel_mcp.py (port 8009), so stop that first.
"""
import sys
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer

import hotel_mcp
from agentbank.money import Money
from agentbank.offers import OFFER_TTL, Cancellation, Offer

method = sys.argv[1] if len(sys.argv) > 1 else "card"
desk = hotel_mcp.desk
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", hotel_mcp.PAY_PORT), hotel_mcp.PayHandler).serve_forever,
                 daemon=True).start()

offer = Offer("htl_paris_marais", "DBL", date(2026, 11, 13), date(2026, 11, 15), 2, ("breakfast", "late_checkout"),
              Cancellation.FREE_48H, Decimal("0.6"), Money.of(500, "EUR"), datetime.now(timezone.utc) + OFFER_TTL)
booked = desk.book(desk.commit(offer), True, method)
ref = booked["booking_ref"]
print(booked["result"], ref, "\n", booked["next_step"], flush=True)

deadline = time.time() + 600
while time.time() < deadline:
    status = desk.booking_status(ref)["status"]
    if status != "awaiting_payment":
        print("status:", status, flush=True)
        break
    time.sleep(3)
else:
    print("timed out waiting for payment", flush=True)
