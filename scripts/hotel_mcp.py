"""MCP server that lets a traveller's Claude negotiate with hotels' seller agents and book.

    claude mcp add hotels -- <repo>/.venv/bin/python <repo>/scripts/hotel_mcp.py

Each hotel answers through its own seller agent (Claude, hotel side), which needs
ANTHROPIC_API_KEY (or `ant auth login`). Deposits are paid by card through Airwallex (the
checkout pages are served on http://127.0.0.1:8009 by this process) or by the traveller's
agent on Metal (metal/hotel-pay.ts). Inventory is simulated from data/hotels.json; bookings
live in var/bookings.json.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp.server.mcpserver import MCPServer

from _bootstrap import ROOT, connect

from agentbank.bookings import BookingError
from agentbank.config import ConfigError, load_dotenv
from agentbank.offer_desk import OfferDesk

PAY_PORT = 8009
load_dotenv(ROOT / ".env")
try:
    _, api = connect()
except ConfigError:
    api = None   # negotiation still works; card payments report that Airwallex is not configured
desk = OfferDesk.from_files(ROOT, api)

server = MCPServer("hotels", instructions="""Hotel marketplace for AI travel agents (simulated inventory).
1. search_hotels shows published prices; they are a starting point, not final.
2. negotiate sends your message to a hotel's own sales agent. State the traveller's dates, guests,
   must-haves and budget. Keep the negotiation_id to continue. Hotels can trade price against
   extras, cancellation terms and the deposit hold.
3. Before book_offer, show the traveller the exact terms of one offer_id, ask how they want to pay
   the deposit (card, or metal if they gave you a Metal wallet), and get an explicit yes.
4. book_offer returns the next payment step: a checkout link for the traveller (card), or a URL to
   pay with the Metal wallet's pay tool (metal). Check booking_status after payment.""")


@server.tool()
def search_hotels(check_in: str, check_out: str, guests: int, extras: list[str], cancellation: str) -> list[dict]:
    """Published prices for a stay. Dates are ISO (2026-11-13). extras: any of "breakfast",
    "late_checkout". cancellation: "non_refundable", "free_until_48h" or "free_until_24h"."""
    return desk.search(check_in, check_out, guests, extras, cancellation)


@server.tool()
def negotiate(hotel_id: str, message: str, negotiation_id: str | None = None) -> dict:
    """Send a message to the hotel's sales agent and get its reply plus any offers it committed to.
    Omit negotiation_id to start; pass it back to continue the same negotiation."""
    return desk.negotiate(hotel_id, message, negotiation_id)


@server.tool()
def book_offer(offer_id: str, traveller_confirmed: bool, payment_method: str) -> dict:
    """Book a committed offer exactly as offered. Set traveller_confirmed only after the traveller
    explicitly accepted these terms. payment_method: "card" (Airwallex hold or charge, paid by the
    traveller on a checkout page) or "metal" (paid by the agent from the traveller's Metal wallet).
    Offers expire 15 minutes after they are made."""
    return desk.book(offer_id, traveller_confirmed, payment_method)


@server.tool()
def booking_status(booking_ref: str) -> dict:
    """Current state of a booking and its deposit."""
    return desk.booking_status(booking_ref)


@server.tool()
def cancel_booking(booking_ref: str) -> dict:
    """Cancel a booking. Inside the free-cancellation window the card hold is released (or a Metal
    refund is recorded as due); after it, or for non-refundable bookings, the deposit is kept."""
    return desk.cancel_booking(booking_ref)


class PayHandler(BaseHTTPRequestHandler):
    """Card checkout pages for travellers, and the booking lookups metal/hotel-pay.ts relies on.
    Listens on 127.0.0.1 only: the Metal callbacks trust local callers."""

    def _send(self, status: int, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            if m := re.fullmatch(r"/pay/([\w-]+)", self.path):
                b = desk.payments.refresh(m.group(1))
                if b["method"] != "card":
                    return self._send(409, {"error": "this booking is paid on Metal"})
                page = (ROOT / "web" / "hotel_pay.html").read_text().replace("__BOOKING__", json.dumps(b).replace("</", "<\\/"))
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if m := re.fullmatch(r"/api/bookings/([\w-]+)", self.path):
                return self._send(200, desk.booking_status(m.group(1)))
            if m := re.fullmatch(r"/api/metal/([\w-]+)", self.path):
                b = desk.bookings.get(m.group(1))
                if b["method"] != "metal":
                    return self._send(409, {"error": "not a Metal booking"})
                return self._send(200, {"status": b["status"], **b["metal"]})
        except BookingError as e:
            return self._send(404, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        m = re.fullmatch(r"/api/metal/([\w-]+)/paid", self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            b = desk.payments.record_metal_payment(m.group(1), body["transaction"], body.get("payer", ""), body["usdz"])
        except (BookingError, KeyError) as e:
            return self._send(409, {"error": str(e)})
        self._send(200, {"status": b["status"]})

    def log_message(self, *args):   # stdout carries the MCP protocol
        pass


if __name__ == "__main__":
    pay = ThreadingHTTPServer(("127.0.0.1", PAY_PORT), PayHandler)
    threading.Thread(target=pay.serve_forever, daemon=True).start()
    server.run()
