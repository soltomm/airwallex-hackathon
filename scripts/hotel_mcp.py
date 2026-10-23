"""MCP server that lets a traveller's Claude negotiate with hotels' seller agents and book.

Local (stdio, one traveller; card checkout pages on http://127.0.0.1:8009):
    claude mcp add hotels -- <repo>/.venv/bin/python <repo>/scripts/hotel_mcp.py

Hosted (streamable HTTP, many travellers; see render.yaml):
    HOTELS_ACCESS_TOKEN=<secret> python scripts/hotel_mcp.py --http
    -> MCP at <PUBLIC_BASE_URL>/mcp/<secret>, checkout pages at <PUBLIC_BASE_URL>/pay/<ref>

Each hotel answers through its own seller agent (Claude, hotel side), which needs
ANTHROPIC_API_KEY. Deposits are paid by card through Airwallex or, locally, by the traveller's
agent on Metal (metal/hotel-pay.ts). Inventory is simulated from data/hotels.json.

Environment (all optional locally):
  PUBLIC_BASE_URL / RENDER_EXTERNAL_URL  where travellers' browsers reach the checkout pages
  HOTELS_PAYMENT_METHODS                 "card,metal" (default) or "card"
  HOTELS_MAX_NEGOTIATIONS, HOTELS_MAX_TURNS, HOTELS_DAILY_USD   caps for a shared deployment
  HOTELS_ADMIN_TOKEN                     hosted revenue dashboard at <PUBLIC_BASE_URL>/dashboard/<token>
                                         (locally: http://127.0.0.1:8009/dashboard)
"""
import hashlib
import hmac
import json
import os
import re
import sys
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, MCPServer

from _bootstrap import ROOT, connect

from agentbank.bookings import BookingError
from agentbank.config import ConfigError, load_dotenv
from agentbank.offer_desk import LOCAL, PAYMENT_METHODS, Limits, OfferDesk

PAY_PORT = 8009
load_dotenv(ROOT / ".env")
env = os.environ.get
PUBLIC = (env("PUBLIC_BASE_URL") or env("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{PAY_PORT}").rstrip("/")
try:
    _, api = connect()
except ConfigError as e:
    api = None   # negotiation still works; card payments report that Airwallex is not configured
    print(f"Airwallex not configured: {e}", file=sys.stderr)   # shows in the host's logs
desk = OfferDesk.from_files(
    ROOT, api, pay_base_url=PUBLIC,
    payment_methods=tuple(m for m in env("HOTELS_PAYMENT_METHODS", ",".join(PAYMENT_METHODS)).split(",") if m),
    limits=Limits(negotiations_per_tenant=int(env("HOTELS_MAX_NEGOTIATIONS")) if env("HOTELS_MAX_NEGOTIATIONS") else None,
                  turns_per_tenant=int(env("HOTELS_MAX_TURNS")) if env("HOTELS_MAX_TURNS") else None,
                  daily_usd=Decimal(env("HOTELS_DAILY_USD")) if env("HOTELS_DAILY_USD") else None))


def tenant_of(ctx: Context) -> str:
    """One tenant per MCP session when hosted (the session id, hashed); 'local' over stdio."""
    headers = ctx.headers
    sid = headers.get("mcp-session-id") if headers else None
    return hashlib.sha256(sid.encode()).hexdigest()[:16] if sid else LOCAL


# In-chat negotiation panel (MCP App; Claude Desktop and claude.ai render it). Built from ui/ by
# `cd ui && npm run build`. Clients without Apps support get the same tools as plain text.
PANEL = "ui://hotels/negotiation.html"
apps = Apps()
apps.add_html_resource(PANEL, (ROOT / "web" / "negotiation_app.html").read_text(), title="Negotiation room",
                       prefers_border=True)


@apps.tool(resource_uri=PANEL)
def search_hotels(check_in: str, check_out: str, guests: int, extras: list[str], cancellation: str,
                  ctx: Context) -> dict:
    """Published prices for a stay. Dates are ISO (2026-11-13). extras: any of "breakfast",
    "late_checkout". cancellation: "non_refundable", "free_until_48h" or "free_until_24h".
    Opens the live negotiation room in clients that show apps."""
    tenant = tenant_of(ctx)
    return {"room": tenant, "results": desk.search(check_in, check_out, guests, extras, cancellation, tenant)}


@apps.tool(resource_uri=PANEL, visibility=["app"])
def negotiation_feed(ctx: Context, room: str = "", since: int = 0) -> dict:
    """Events after `since` for the negotiation room: searches, each round's messages, the hotel
    agent's steps as they happen, offers and bookings. Called by the panel, not the model. `room`
    comes from search_hotels, so the panel follows its own traveller's session."""
    return desk.feed_since(since, room or tenant_of(ctx))


server = MCPServer("hotels", extensions=[apps], instructions="""Hotel marketplace for AI travel agents (simulated inventory).
1. search_hotels shows published prices; they are a starting point, not final.
2. negotiate sends your message to a hotel's own sales agent. State the traveller's dates, guests,
   must-haves and budget. Keep the negotiation_id to continue. Hotels can trade price against
   extras, cancellation terms and the deposit hold.
3. Before book_offer, show the traveller the exact terms of one offer_id, ask how they want to pay
   the deposit (card, or metal if they gave you a Metal wallet), and get an explicit yes.
   Make the negotiation visible as it happens: after every negotiate call, before your next move,
   show the traveller that round as "Round N - <hotel>", list the seller_activity lines, quote the
   hotel's message (blockquote), then say in one line what you will counter with and why.
4. book_offer returns the next payment step: a checkout link for the traveller (card), or a URL to
   pay with the Metal wallet's pay tool (metal). Check booking_status after payment.
5. In clients that show apps, search_hotels opens a live negotiation room for the traveller. When
   they click Book there, a message arrives in the chat: confirm those exact terms as in step 3.""")


@server.tool()
def negotiate(hotel_id: str, message: str, ctx: Context, negotiation_id: str | None = None) -> dict:
    """Send a message to the hotel's sales agent and get its reply plus any offers it committed to.
    Omit negotiation_id to start; pass it back to continue the same negotiation."""
    return desk.negotiate(hotel_id, message, negotiation_id, tenant=tenant_of(ctx))


@server.tool()
def book_offer(offer_id: str, traveller_confirmed: bool, payment_method: str, ctx: Context) -> dict:
    """Book a committed offer exactly as offered. Set traveller_confirmed only after the traveller
    explicitly accepted these terms. payment_method: "card" (Airwallex hold or charge, paid by the
    traveller on a checkout page) or "metal" (paid by the agent from the traveller's Metal wallet).
    Offers expire 15 minutes after they are made."""
    return desk.book(offer_id, traveller_confirmed, payment_method, tenant=tenant_of(ctx))


@server.tool()
def booking_status(booking_ref: str, ctx: Context) -> dict:
    """Current state of a booking and its deposit."""
    return desk.booking_status(booking_ref, tenant=tenant_of(ctx))


@server.tool()
def cancel_booking(booking_ref: str, ctx: Context) -> dict:
    """Cancel a booking. Inside the free-cancellation window the card hold is released (or a Metal
    refund is recorded as due); after it, or for non-refundable bookings, the deposit is kept."""
    return desk.cancel_booking(booking_ref, tenant=tenant_of(ctx))


# ---- hotel web pages (shared by the local and hosted servers) ---------------
PAGE_FIELDS = ("booking_ref", "hotel", "room", "guests", "check_in", "check_out", "extras", "cancellation",
               "total", "deposit", "currency", "status", "payment_intent_id", "client_secret")


def dashboard_page(data_url: str) -> bytes:
    return (ROOT / "web" / "hotel_dashboard.html").read_text().replace("__DATA_URL__", json.dumps(data_url)).encode()


def web_get(path: str) -> tuple[int, object, str]:
    """(status, body, content type) for the checkout page and booking lookups."""
    try:
        if m := re.fullmatch(r"/pay/([\w-]+)", path):
            b = desk.payments.refresh(m.group(1))
            if b["method"] != "card":
                return 409, {"error": "this booking is paid on Metal"}, "application/json"
            if b["status"] == "awaiting_payment" and not b.get("payment_intent_id"):
                b = desk.payments.open_card_payment(m.group(1))   # retry after an Airwallex failure
            data = json.dumps({k: b.get(k) for k in PAGE_FIELDS}).replace("</", "<\\/")
            page = (ROOT / "web" / "hotel_pay.html").read_text().replace("__BOOKING__", data)
            return 200, page.encode(), "text/html; charset=utf-8"
        if m := re.fullmatch(r"/api/bookings/([\w-]+)", path):
            b = desk.booking_status(m.group(1), tenant=None)
            return 200, {"booking_ref": b.get("booking_ref"), "status": b.get("status"), "error": b.get("error")}, \
                "application/json"
        if "metal" in desk.payment_methods and (m := re.fullmatch(r"/api/metal/([\w-]+)", path)):
            b = desk.bookings.get(m.group(1))
            if b["method"] != "metal":
                return 409, {"error": "not a Metal booking"}, "application/json"
            return 200, {"status": b["status"], **b["metal"]}, "application/json"
        if path == "/healthz":
            return 200, {"ok": True}, "application/json"
    except BookingError as e:
        return 404, {"error": str(e)}, "application/json"
    return 404, {"error": "not found"}, "application/json"


def web_metal_paid(path: str, body: dict) -> tuple[int, object]:
    """metal/hotel-pay.ts reports a settled deposit. Local only: it trusts its caller."""
    m = re.fullmatch(r"/api/metal/([\w-]+)/paid", path)
    if not m or "metal" not in desk.payment_methods:
        return 404, {"error": "not found"}
    try:
        b = desk.payments.record_metal_payment(m.group(1), body["transaction"], body.get("payer", ""), body["usdz"])
    except (BookingError, KeyError) as e:
        return 409, {"error": str(e)}
    desk.note_booking(m.group(1))
    return 200, {"status": b["status"]}


class PayHandler(BaseHTTPRequestHandler):
    """The local server's pages on 127.0.0.1 (the Metal callback and the dashboard trust local callers)."""

    def _send(self, status: int, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/dashboard":
            return self._send(200, dashboard_page("/dashboard/data.json"), "text/html; charset=utf-8")
        if self.path == "/dashboard/data.json":
            return self._send(200, desk.insights())
        self._send(*web_get(self.path))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self._send(*web_metal_paid(self.path, body))

    def log_message(self, *args):   # stdout carries the MCP protocol
        pass


def serve_http() -> None:
    """Hosted: streamable HTTP MCP at /mcp/<token>, plus the checkout pages, on $PORT."""
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    token = env("HOTELS_ACCESS_TOKEN", "")
    if len(token) < 16:
        sys.exit("set HOTELS_ACCESS_TOKEN (16+ characters): it is the secret part of the MCP URL")

    async def page(request: Request) -> Response:
        status, body, ctype = await run_in_threadpool(web_get, request.url.path)
        return Response(body, status, media_type=ctype) if isinstance(body, bytes) else JSONResponse(body, status)

    for route in ("/pay/{ref}", "/api/bookings/{ref}", "/healthz"):
        server.custom_route(route, methods=["GET"])(page)

    admin = env("HOTELS_ADMIN_TOKEN", "")

    async def dashboard(request: Request) -> Response:
        """Hotel staff only: floors and lost demand. Off unless HOTELS_ADMIN_TOKEN is set."""
        if len(admin) < 16 or not hmac.compare_digest(request.path_params["token"], admin):
            return JSONResponse({"error": "not found"}, 404)
        if request.url.path.endswith("/data.json"):
            return JSONResponse(await run_in_threadpool(desk.insights))
        return Response(dashboard_page(f"/dashboard/{admin}/data.json"), media_type="text/html; charset=utf-8")

    server.custom_route("/dashboard/{token}", methods=["GET"])(dashboard)
    server.custom_route("/dashboard/{token}/data.json", methods=["GET"])(dashboard)
    # The secret path is the access control. DNS-rebinding protection guards servers on a user's own
    # machine; this one is public by design, behind TLS at the host's proxy.
    app = server.streamable_http_app(streamable_http_path=f"/mcp/{token}", host="0.0.0.0",
                                     transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    print(f"hotels MCP on {PUBLIC}/mcp/<token>; checkout pages on {PUBLIC}/pay/<ref>", file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=int(env("PORT", "8000")), log_level="warning")


if __name__ == "__main__":
    if "--http" in sys.argv:
        serve_http()
    else:
        try:
            pay = ThreadingHTTPServer(("127.0.0.1", PAY_PORT), PayHandler)
            threading.Thread(target=pay.serve_forever, daemon=True).start()
        except OSError as e:   # another copy (e.g. Claude Desktop and Claude Code both running) serves the pages
            print(f"checkout pages not started on :{PAY_PORT}: {e}", file=sys.stderr)
        server.run()
