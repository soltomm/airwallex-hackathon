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
  AWX_WEBHOOK_SECRET                     verifies Airwallex webhooks at <PUBLIC_BASE_URL>/webhooks/airwallex
  HOTELS_ADMIN_TOKEN                     hosted revenue dashboard at <PUBLIC_BASE_URL>/dashboard/<token>
                                         (locally: http://127.0.0.1:8009/dashboard)
  HOTELS_JOIN_TOKEN                      hosted hotel sign-up at <PUBLIC_BASE_URL>/join/<token>
                                         (locally: http://127.0.0.1:8009/join)
  HOTELS_MAX_NEW_HOTELS                  cap on hotels that sign up themselves
  AC_CHECKOUT_SECRET                     Kit 10: verifies Airwallex Agentic Commerce calls to
                                         <PUBLIC_BASE_URL>/agentic-commerce/checkout (scripts/agentic_commerce.py)
  SUPABASE_URL, SUPABASE_ANON_KEY        Kit 10: shoppers sign in at <PUBLIC_BASE_URL>/oauth/consent, the
                                         Authorization Path of the Supabase OAuth server Claude connects through
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

from agentbank.agentic_commerce import CheckoutError, MerchantCheckout, Night, image, product
from agentbank.bookings import BookingError
from agentbank.config import ConfigError, load_dotenv
from agentbank.hotel_onboarding import STYLES, SignupError
from agentbank.offer_desk import LOCAL, PAYMENT_METHODS, Limits, OfferDesk
from agentbank.webhooks import WebhookInbox

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
                  daily_usd=Decimal(env("HOTELS_DAILY_USD")) if env("HOTELS_DAILY_USD") else None),
    max_new_hotels=int(env("HOTELS_MAX_NEW_HOTELS")) if env("HOTELS_MAX_NEW_HOTELS") else None)


merchant = MerchantCheckout(desk, env("AC_CHECKOUT_SECRET", ""))   # Kit 10 create_checkout; 404 without a secret
inbox = WebhookInbox(env("AWX_WEBHOOK_SECRET", ""), lambda e: merchant.on_payment(e) or desk.on_airwallex_event(e),
                     ROOT / "var/webhooks.jsonl") if env("AWX_WEBHOOK_SECRET") else None


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
                  ctx: Context, city: str = "") -> dict:
    """Published prices for a stay. Dates are ISO (2026-11-13). extras: any of "breakfast",
    "late_checkout". cancellation: "non_refundable", "free_until_48h" or "free_until_24h".
    city: e.g. "Paris" or "Barcelona"; empty searches every city. New hotels can join at any time.
    Opens the live negotiation room in clients that show apps."""
    tenant = tenant_of(ctx)
    results = desk.search(check_in, check_out, guests, extras, cancellation, tenant, city=city)
    out = {"room": tenant, "results": results}
    if city and not results:
        out["cities"] = sorted({h.city for h in list(desk.hotels.values()) if h.city})
    return out


@apps.tool(resource_uri=PANEL, visibility=["app"])
def negotiation_feed(ctx: Context, room: str = "", since: int = 0) -> dict:
    """Events after `since` for the negotiation room: searches, each round's messages, the hotel
    agent's steps as they happen, offers and bookings. Called by the panel, not the model. `room`
    comes from search_hotels, so the panel follows its own traveller's session."""
    return desk.feed_since(since, room or tenant_of(ctx))


server = MCPServer("hotels", extensions=[apps], instructions="""Hotel marketplace for AI travel agents (simulated inventory).
1. search_hotels shows published prices (pass the city); they are a starting point, not final.
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
def book_offer(offer_id: str, traveller_confirmed: bool, payment_method: str, ctx: Context,
               currency: str | None = None) -> dict:
    """Book a committed offer exactly as offered. Set traveller_confirmed only after the traveller
    explicitly accepted these terms. payment_method: "card" (Airwallex hold or charge, paid by the
    traveller on a checkout page) or "metal" (paid by the agent from the traveller's Metal wallet).
    currency (card only, optional): "USD" or "GBP" to hold the deposit in the traveller's currency;
    the price stays in EUR and the rate is locked now (shown in the booking's `fx`).
    Offers expire 15 minutes after they are made. If the hotel can no longer sell these terms
    (rooms sold elsewhere, offer expired) the result is "terms_changed" with the closest new_offer the
    hotel commits to: show the traveller exactly what changed and book new_offer.offer_id only after
    they approve it. A hotel may also cancel a stay it cannot honour; booking_status then shows the
    refund or released hold."""
    return desk.book(offer_id, traveller_confirmed, payment_method, tenant=tenant_of(ctx), currency=currency)


@server.tool()
def booking_status(booking_ref: str, ctx: Context) -> dict:
    """Current state of a booking and its deposit (in EUR, and in the guest's currency under `fx`)."""
    return desk.booking_status(booking_ref, tenant=tenant_of(ctx))


@server.tool()
def cancel_booking(booking_ref: str, ctx: Context) -> dict:
    """Cancel a booking. Inside the free-cancellation window the card hold is released (or a Metal
    refund is recorded as due); after it, or for non-refundable bookings, the deposit is kept."""
    return desk.cancel_booking(booking_ref, tenant=tenant_of(ctx))


# ---- hotel web pages (shared by the local and hosted servers) ---------------
PAGE_FIELDS = ("booking_ref", "hotel", "room", "guests", "check_in", "check_out", "extras", "cancellation",
               "total", "deposit", "currency", "status", "payment_intent_id", "client_secret", "fx")


def dashboard_page(data_url: str, join_url: str | None) -> bytes:
    return (ROOT / "web" / "hotel_dashboard.html").read_text().replace("__DATA_URL__", json.dumps(data_url)) \
        .replace("__JOIN_URL__", json.dumps(join_url)).encode()


def join_page(post_url: str) -> bytes:
    """Hotel sign-up form; it posts JSON to post_url and polls post_url/status/<hotel_id>."""
    styles = {k: v["label"] for k, v in STYLES.items()}
    return (ROOT / "web" / "hotel_join.html").read_text().replace("__POST_URL__", json.dumps(post_url)) \
        .replace("__STYLES__", json.dumps(styles)).encode()


def join_post(form: dict) -> tuple[int, dict]:
    if not isinstance(form, dict):
        return 400, {"error": "send the sign-up form as a JSON object"}
    try:
        return 200, desk.onboarding.join(form)
    except SignupError as e:
        return 422, {"error": str(e)}


def dashboard_post(rest: str) -> tuple[int, dict]:
    """Revenue staff's actions, `rest` being the path after /dashboard[/<token>]."""
    if m := re.fullmatch(r"/suggestions/(sug_\w+)/apply", rest):
        out = desk.apply_suggestion(m.group(1))
    elif m := re.fullmatch(r"/changes/(sug_\w+)/revert", rest):
        out = desk.revert_change(m.group(1))
    elif m := re.fullmatch(r"/bookings/([\w-]+)/([a-z-]+)", rest):
        out = desk.hotel_action(m.group(1), m.group(2))
    elif m := re.fullmatch(r"/onboarding/(htl_\w+)/retry", rest):
        out = desk.onboarding.retry(m.group(1))
    elif rest == "/payout-cycle":
        out = desk.run_payout_cycle()
    elif m := re.fullmatch(r"/simulate/overbook/(\w+)/(\d{4}-\d{2}-\d{2})", rest):
        out = desk.sim.overbook(m.group(1), m.group(2))
    elif m := re.fullmatch(r"/simulate/sell/(\w+)/(\w+)/(\d{4}-\d{2}-\d{2})/(\d{1,2})", rest):
        try:
            out = desk.sim.sell_rooms(m.group(1), m.group(2), m.group(3), int(m.group(4)))
        except KeyError as e:
            out = {"error": f"unknown hotel or room {e}"}
    else:
        return 404, {"error": "not found"}
    return (409 if "error" in out else 200), out


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
        if path.split("?")[0] == "/oauth/consent":   # the local server passes the query string too
            data = json.dumps({"url": env("SUPABASE_URL", ""), "key": env("SUPABASE_ANON_KEY", "")}).replace("</", "<\\/")
            page = (ROOT / "web" / "oauth_consent.html").read_text().replace("__CONFIG__", data)
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
        if m := re.fullmatch(r"/catalogue/(htl_\w+)\.svg", path):   # catalogue product images (Kit 10)
            if m.group(1) in desk.hotels:
                return 200, image(desk.hotels[m.group(1)]), "image/svg+xml"
        elif m := re.fullmatch(r"/catalogue/([\w.]+)", path):          # catalogue product links (Kit 10)
            n = Night.parse(m.group(1))
            h = desk.hotels.get(n.plan.hotel_id)
            if h and n.plan.room_code in h.rooms and n.plan.breakfast_guests <= h.rooms[n.plan.room_code].max_guests:
                return 200, product(h, n, PUBLIC), "application/json"
        if path == "/healthz":
            return 200, {"ok": True}, "application/json"
    except CheckoutError:
        pass
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
            return self._send(200, dashboard_page("/dashboard/data.json", "/join"), "text/html; charset=utf-8")
        if self.path == "/join":
            return self._send(200, join_page("/join"), "text/html; charset=utf-8")
        if m := re.fullmatch(r"/join/status/(htl_\w+)", self.path):
            st = desk.onboarding.status(m.group(1))
            return self._send(404 if "error" in st else 200, st)
        if self.path == "/dashboard/data.json":
            return self._send(200, desk.insights())
        self._send(*web_get(self.path))

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/webhooks/airwallex":
            return self._send(*(inbox.receive(dict(self.headers), raw) if inbox else (404, {"error": "not found"})))
        if self.path == "/agentic-commerce/checkout":
            return self._send(*merchant.receive(dict(self.headers), raw))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})
        if self.path == "/join":
            return self._send(*join_post(body))
        if self.path.startswith("/dashboard/"):
            return self._send(*dashboard_post(self.path.removeprefix("/dashboard")))
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

    for route in ("/pay/{ref}", "/api/bookings/{ref}", "/catalogue/{item}", "/oauth/consent", "/healthz"):
        server.custom_route(route, methods=["GET"])(page)

    @server.custom_route("/webhooks/airwallex", methods=["POST"])
    async def webhook(request: Request) -> Response:
        if inbox is None:
            return JSONResponse({"error": "not found"}, 404)
        status, body = inbox.receive(dict(request.headers), await request.body())   # raw bytes, as signed
        return JSONResponse(body, status)

    @server.custom_route("/agentic-commerce/rejections", methods=["GET"])
    async def agentic_rejections(request: Request) -> Response:
        """Recent create_checkout calls we refused, for debugging; behind the checkout secret."""
        if not merchant.secret or not hmac.compare_digest(request.query_params.get("token", ""), merchant.secret):
            return JSONResponse({"error": "not found"}, 404)
        return JSONResponse(merchant.rejected)

    @server.custom_route("/agentic-commerce/checkout", methods=["POST"])
    async def agentic_checkout(request: Request) -> Response:
        """Airwallex's merchant MCP pricing a cart (Kit 10); signed like the webhooks."""
        raw = await request.body()
        status, body = await run_in_threadpool(merchant.receive, dict(request.headers), raw)
        return JSONResponse(body, status)

    admin = env("HOTELS_ADMIN_TOKEN", "")
    join = env("HOTELS_JOIN_TOKEN", "")

    def join_allowed(request: Request) -> bool:
        return len(join) >= 16 and hmac.compare_digest(request.path_params["token"], join)

    async def join_form(request: Request) -> Response:
        """Hotels sign themselves up: listed at once, Airwallex account opened in the background."""
        if not join_allowed(request):
            return JSONResponse({"error": "not found"}, 404)
        if request.method == "GET":
            return Response(join_page(f"/join/{join}"), media_type="text/html; charset=utf-8")
        try:
            form = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, 400)
        status, body = await run_in_threadpool(join_post, form)
        return JSONResponse(body, status)

    async def join_status(request: Request) -> Response:
        if not join_allowed(request) or not re.fullmatch(r"htl_\w+", request.path_params["hotel"]):
            return JSONResponse({"error": "not found"}, 404)
        st = desk.onboarding.status(request.path_params["hotel"])
        return JSONResponse(st, 404 if "error" in st else 200)

    server.custom_route("/join/{token}", methods=["GET", "POST"])(join_form)
    server.custom_route("/join/{token}/status/{hotel}", methods=["GET"])(join_status)

    async def dashboard(request: Request) -> Response:
        """Hotel staff only: floors and lost demand. Off unless HOTELS_ADMIN_TOKEN is set."""
        if len(admin) < 16 or not hmac.compare_digest(request.path_params["token"], admin):
            return JSONResponse({"error": "not found"}, 404)
        if request.url.path.endswith("/data.json"):
            return JSONResponse(await run_in_threadpool(desk.insights))
        return Response(dashboard_page(f"/dashboard/{admin}/data.json", f"/join/{join}" if len(join) >= 16 else None),
                        media_type="text/html; charset=utf-8")

    server.custom_route("/dashboard/{token}", methods=["GET"])(dashboard)
    server.custom_route("/dashboard/{token}/data.json", methods=["GET"])(dashboard)

    async def dashboard_action(request: Request) -> Response:
        if len(admin) < 16 or not hmac.compare_digest(request.path_params["token"], admin):
            return JSONResponse({"error": "not found"}, 404)
        rest = request.url.path.removeprefix(f"/dashboard/{admin}")
        status, body = await run_in_threadpool(dashboard_post, rest)
        return JSONResponse(body, status)

    for route in ("/dashboard/{token}/suggestions/{sid}/apply", "/dashboard/{token}/changes/{sid}/revert",
                  "/dashboard/{token}/bookings/{ref}/{action}", "/dashboard/{token}/payout-cycle",
                  "/dashboard/{token}/onboarding/{hotel}/retry",
                  "/dashboard/{token}/simulate/overbook/{hotel}/{night}",
                  "/dashboard/{token}/simulate/sell/{hotel}/{room}/{night}/{rooms}"):
        server.custom_route(route, methods=["POST"])(dashboard_action)
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
