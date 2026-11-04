#!/usr/bin/env python3
"""Harbour House group-sales demo server: hotel dashboard, agents, hosted payment page, webhooks.

  PYTHONPATH=src python scripts/group_sales.py            # http://localhost:8010
Env: PAYMENTS=airwallex|simulated  SELLER_MODE=claude|rules  BUYER_MODE=presenter|agent
     PUBLIC_BASE_URL (for Airwallex return URLs)  AWX_WEBHOOK_SECRET (to accept Airwallex webhooks)
     GS_PORT / PORT (default 8010)  GS_VAR (state dir, default var/groupsales)
     GS_ACCESS_TOKEN: when set, the dashboard and API need it once (<url>/?k=<token>, then a cookie).
     GS_MAX_CLAUDE_RUNS_PER_DAY (default 150): beyond this the seller falls back to rules.
     DATABASE_URL: keep state in Postgres (storage.py) so a hosted instance survives spin-downs.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbank.config import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agentbank.groupsales.buyer import BuyerAgent, act, suggest  # noqa: E402
from agentbank.groupsales.calc import gbp  # noqa: E402
from agentbank.groupsales.engine import Engine, Refused  # noqa: E402
from agentbank.groupsales.provider import make_provider  # noqa: E402
from agentbank.groupsales.seller import ClaudeSeller, RulesSeller  # noqa: E402
from agentbank.webhooks import signature_ok  # noqa: E402

PORT = int(os.environ.get("GS_PORT") or os.environ.get("PORT") or "8010")
VAR = Path(os.environ.get("GS_VAR", ROOT / "var" / "groupsales"))
PUBLIC = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or f"http://localhost:{PORT}").rstrip("/")
ACCESS = os.environ.get("GS_ACCESS_TOKEN", "")
MAX_CLAUDE_RUNS = int(os.environ.get("GS_MAX_CLAUDE_RUNS_PER_DAY", "150"))
OPEN_PATHS = ("/pay/", "/sim-hpp/", "/webhooks/", "/healthz")
WEB = ROOT / "web"
HAS_CLAUDE = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

provider = make_provider(None, VAR)
engine = Engine(VAR, provider, public_base=PUBLIC)


class Runner:
    """Runs the seller (and, in agent mode, the buyer) in the background when something changes."""

    def __init__(self):
        self.settings = {"auto_seller": True,
                         "seller_mode": os.environ.get("SELLER_MODE") or ("claude" if HAS_CLAUDE else "rules"),
                         "buyer_mode": os.environ.get("BUYER_MODE", "presenter")}
        self.busy = None
        self.last = []
        self.claude_runs = {"day": time.strftime("%Y-%m-%d"), "n": 0}
        self.pending = threading.Event()
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def wake(self, reason: str):
        if self.settings["auto_seller"] or self.settings["buyer_mode"] == "agent":
            self.pending.set()

    def _loop(self):
        while True:
            self.pending.wait()
            time.sleep(0.4)          # let a burst of changes settle
            self.pending.clear()
            try:
                self.run_once("activity")
            except Exception as e:
                traceback.print_exc()
                engine.note(None, f"Agent runner error: {e}", actor="system")

    def run_once(self, trigger: str, force_seller: bool = False):
        with self.lock:
            if self.settings["auto_seller"] or force_seller:
                mode = self.settings["seller_mode"]
                if mode == "claude":
                    today = time.strftime("%Y-%m-%d")
                    if self.claude_runs["day"] != today:
                        self.claude_runs = {"day": today, "n": 0}
                    self.claude_runs["n"] += 1
                    if self.claude_runs["n"] > MAX_CLAUDE_RUNS:
                        mode = "rules"
                        engine.note(None, f"Daily cap of {MAX_CLAUDE_RUNS} Claude seller runs reached; using the rules "
                                    "seller until tomorrow.", actor="system")
                self.busy = f"seller ({mode})"
                seller = ClaudeSeller(engine) if mode == "claude" else RulesSeller(engine)
                self.last = seller.run(trigger) if isinstance(seller, ClaudeSeller) else seller.run(trigger=trigger)
            if self.settings["buyer_mode"] == "agent":
                self.busy = "buyer agent"
                for d in list(engine.s["deals"].values()):
                    if suggest(engine, d["id"]) is None:
                        continue
                    if HAS_CLAUDE and self.settings["seller_mode"] == "claude":
                        BuyerAgent(engine).run(d["id"])
                    else:
                        try:
                            act(engine, d["id"], suggest(engine, d["id"]), actor="buyer agent")
                        except Refused as e:
                            engine.note(d["id"], f"Buyer agent refused: {e}", actor="buyer agent")
            self.busy = None


runner = Runner()
engine.listeners.append(runner.wake)


def state():
    s = engine.snapshot()
    s["runner"] = {"busy": runner.busy, "settings": runner.settings, "last": runner.last[-6:],
                   "claude_available": HAS_CLAUDE}
    for d in s["deals"]:
        d["buyer_suggestion"] = suggest(engine, d["id"])
    return s


PAY_PAGE = (WEB / "group_pay.html").read_text() if (WEB / "group_pay.html").exists() else ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else (json.dumps(body, default=str).encode() if ctype == "application/json"
                                                     else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return raw

    def _json(self, raw):
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _authorized(self, path: str, q: dict) -> bool:
        if not ACCESS or path.startswith(OPEN_PATHS):
            return True
        cookies = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
        import hmac
        return hmac.compare_digest(cookies.get("gs_k", ""), ACCESS) or hmac.compare_digest(q.get("k", ""), ACCESS)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path.rstrip("/") or "/"
        if p == "/healthz":
            return self._send(200, {"ok": True})
        if not self._authorized(p, q):
            return self._send(401, "<h1>Harbour House group desk</h1><p>Open the link with its access token.</p>", "text/html")
        if q.get("k") and ACCESS:
            self.send_response(303)
            self.send_header("Set-Cookie", f"gs_k={ACCESS}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                             + ("; Secure" if PUBLIC.startswith("https") else ""))
            self.send_header("Location", p)
            self.end_headers()
            return
        try:
            if p == "/":
                return self._send(200, (WEB / "group_sales.html").read_text(), "text/html")
            if p == "/api/state":
                return self._send(200, state())
            if p.startswith("/api/buyer/") and p.endswith("/suggest"):
                return self._send(200, {"suggestion": suggest(engine, p.split("/")[3])})
            if p.startswith("/pay/"):
                parts = p.split("/")
                return self._pay(parts[2], q.get("t", ""), returned=len(parts) > 3 and parts[3] == "return")
            if p.startswith("/sim-hpp/"):
                return self._sim_hpp(p.split("/")[2], q)
            return self._send(404, {"error": "not found"})
        except Refused as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path.rstrip("/")
        raw = self._body()
        b = self._json(raw)
        if not self._authorized(p, {}):
            return self._send(401, {"error": "access token required"})
        try:
            if p == "/webhooks/airwallex":
                return self._webhook(raw)
            if p == "/api/reset":
                return self._send(200, engine.reset())
            if p == "/api/seller/run":
                threading.Thread(target=runner.run_once, args=("presenter asked the seller to run", True),
                                 daemon=True).start()
                return self._send(200, {"started": True})
            if p == "/api/settings":
                for k in ("auto_seller", "seller_mode", "buyer_mode"):
                    if k in b:
                        if k == "seller_mode" and b[k] == "claude" and not HAS_CLAUDE:
                            return self._send(400, {"error": "no Anthropic credentials configured"})
                        runner.settings[k] = b[k]
                runner.wake("settings")
                return self._send(200, runner.settings)
            if p == "/api/clock":
                return self._send(200, {"clock": engine.advance_clock(hours=b.get("hours"), to=b.get("to"))})
            if p == "/api/sim":
                engine.set_sim(**b)
                return self._send(200, {"ok": True})
            if p == "/api/notifications/release":
                return self._send(200, engine.release_notifications())
            if p == "/api/inbox":
                if not (b.get("from") and b.get("body")):
                    return self._send(400, {"error": "from and body are required"})
                return self._send(200, engine.ingest_email(b["from"], b.get("subject") or "(no subject)", b["body"],
                                                           message_id=b.get("message_id") or None))
            if p.startswith("/api/approvals/"):
                return self._send(200, engine.decide_approval(p.split("/")[3], b.get("decision"), b.get("actor"),
                                                              b.get("note", "")))
            if p.startswith("/api/buyer/") and p.endswith("/agent"):
                did = p.split("/")[3]
                if HAS_CLAUDE:
                    r = BuyerAgent(engine).run(did)
                else:
                    s = suggest(engine, did)
                    r = act(engine, did, s, actor="buyer agent") if s else None
                return self._send(200, {"result": r})
            if p.startswith("/api/buyer/"):
                did = p.split("/")[3]
                return self._send(200, engine.buyer_action(did, b.get("token", ""), b.get("action", ""),
                                                           b.get("text", ""), b.get("payload") or {},
                                                           actor=b.get("actor", "buyer (presenter)")))
            if p.startswith("/api/deals/") and p.endswith("/check_payment"):
                return self._send(200, engine.check_payment(p.split("/")[3], source="dashboard"))
            if p.startswith("/api/deals/") and p.endswith("/check_refund"):
                return self._send(200, engine.check_refund(p.split("/")[3], actor="hotel staff"))
            if p.startswith("/sim-hpp/"):
                return self._sim_pay(p.split("/")[2], {k: v[0] for k, v in parse_qs(raw.decode()).items()})
            return self._send(404, {"error": "not found"})
        except Refused as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})

    # ---- payment page ---------------------------------------------------------------------------
    def _pay(self, ref: str, token: str, returned: bool):
        d = engine.deal(ref)
        if token != d["buyer_token"]:
            return self._send(403, "<h1>Not authorized</h1>", "text/html")
        op = engine._deposit_op(d)
        message = ""
        if returned and op:
            try:
                engine.check_payment(d["id"], source="return from hosted checkout")
            except Exception as e:
                message = f"Could not verify yet: {e}"
        hold = engine.s["holds"].get(d.get("hold_id") or "")
        hold_ok = bool(hold and (hold["status"] in ("active", "converted")))
        v = engine._version(d, op["version"]) if op else None
        info = {
            "hotel": engine.hotel["name"], "ref": d["ref"], "company": d["company"], "status": d["status"],
            "op_status": op and op["status"], "provider": engine.provider.name,
            "intent_id": op and op.get("payment_intent_id"),
            "client_secret": op and op.get("client_secret") if (op and op["status"] == "created" and hold_ok) else None,
            "currency": "GBP", "amount": op and gbp(op["amount"]), "version": op and op["version"],
            "total": v and gbp(v["pricing"]["total"]), "lines": v and [
                {"item": l["item"], "amount": gbp(l["amount"])} for l in v["pricing"]["lines"]],
            "expires_at": hold and hold["expires_at"], "hold_ok": hold_ok, "message": message,
            "success_url": engine.pay_url(d, "return"),
            "sim_url": f"/sim-hpp/{op.get('payment_intent_id')}?ret=" + engine.pay_url(d, "return").replace("&", "%26")
            if op and op.get("payment_intent_id") else None,
            "booking": d.get("booking") and d["booking"]["status"],
        }
        return self._send(200, PAY_PAGE.replace("__PAY__", json.dumps(info)), "text/html")

    def _sim_hpp(self, intent_id: str, q):
        if engine.provider.name != "simulated":
            return self._send(404, {"error": "simulated checkout is off"})
        i = engine.provider.get_intent(intent_id)
        ret = q.get("ret", "/")
        html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Simulated Checkout</title><style>body{{font:15px system-ui;margin:0;padding:40px 16px;background:#f4f4f8;color:#14171a}}
main{{max-width:420px;margin:auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 12px #0001}}
input{{width:100%;box-sizing:border-box;padding:10px;margin:6px 0 14px;border:1px solid #ccd;border-radius:8px;font:inherit}}
button{{width:100%;padding:12px;border:0;border-radius:8px;background:#612fff;color:#fff;font:600 15px system-ui}}
.tag{{display:inline-block;font-size:12px;background:#ffe9b3;padding:2px 8px;border-radius:99px}}</style></head>
<body><main><span class="tag">Simulated hosted page (PAYMENTS=simulated)</span><h2>Pay {gbp(i['amount'])} {i['currency']}</h2>
<p>Order {i['merchant_order_id']}</p><form method="post"><input type="hidden" name="ret" value="{ret}">
<label>Card number<input name="card" value="4035 5010 0000 0008"></label><label>Expiry<input value="12/30"></label>
<label>CVC<input value="123"></label><button>Pay</button></form><p style="font-size:13px;color:#666">4000 0000 0000 0002 declines.</p></main></body></html>"""
        return self._send(200, html, "text/html")

    def _sim_pay(self, intent_id: str, form):
        engine.provider.pay(intent_id, form.get("card", ""))
        self.send_response(303)
        self.send_header("Location", form.get("ret", "/"))
        self.end_headers()

    # ---- webhooks -------------------------------------------------------------------------------
    def _webhook(self, raw: bytes):
        secret = os.environ.get("AWX_WEBHOOK_SECRET", "")
        h = {k.lower(): v for k, v in self.headers.items()}
        if not signature_ok(secret, h.get("x-timestamp", ""), raw, h.get("x-signature", "")):
            return self._send(401, {"error": "bad signature"})
        try:
            event = json.loads(raw)
        except ValueError:
            return self._send(400, {"error": "not json"})
        threading.Thread(target=self._handle_event, args=(event,), daemon=True).start()
        return self._send(200, {"received": True})

    @staticmethod
    def _handle_event(event):
        try:
            engine.handle_event(event, verified=True)
        except Exception as e:
            print(f"webhook {event.get('id')} failed: {e}", file=sys.stderr)


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Harbour House group sales on {PUBLIC}  (payments: {provider.name}, seller: {runner.settings['seller_mode']}, "
          f"buyer: {runner.settings['buyer_mode']}, run {engine.s['run_id']})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
