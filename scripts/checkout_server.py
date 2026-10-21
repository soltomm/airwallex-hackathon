"""Local checkout for card payments without native API access (kits 4 and 12).

    python scripts/checkout_server.py      # then open http://localhost:8008

The server creates PaymentIntents; the page confirms them with Airwallex.js, so card
details go straight to Airwallex. Every intent is recorded in var/checkout_intents.json
for scripts/payments_followup.py.
"""
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _bootstrap import ROOT, connect

from agentbank.airwallex import AirwallexError
from agentbank.money import Money

PORT = 8008
LOG = ROOT / "var" / "checkout_intents.json"
client, api = connect()


def record(entry: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rows = json.loads(LOG.read_text()) if LOG.exists() else []
    rows.append(entry)
    LOG.write_text(json.dumps(rows, indent=2))


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (ROOT / "web" / "checkout.html").read_bytes(), "text/html; charset=utf-8")
        if self.path.startswith("/api/intent/"):
            intent_id = self.path.rsplit("/", 1)[-1]
            try:
                pi = client.get(f"/pa/payment_intents/{intent_id}")
                return self._send(200, {"id": pi["id"], "status": pi.get("status")})
            except AirwallexError as e:
                return self._send(502, {"error": f"{e.code}: {e.message}"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/intent":
            return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        amount, ref = body.get("amount"), body.get("ref", "TEST")
        try:
            pi = api.create_payment_intent(str(uuid.uuid4()), Money.of(amount, "USD"),
                                           f"{ref}-{uuid.uuid4().hex[:6]}")
        except AirwallexError as e:
            return self._send(502, {"error": f"{e.code}: {e.message}"})
        record({"id": pi["id"], "ref": ref, "amount": amount, "preauth": bool(body.get("preauth"))})
        print(f"created {pi['id']}  {ref}  ${amount}{'  (pre-auth)' if body.get('preauth') else ''}")
        self._send(200, {"id": pi["id"], "client_secret": pi["client_secret"], "currency": pi["currency"]})

    def log_message(self, *args):  # keep the terminal readable
        pass


if __name__ == "__main__":
    print(f"Sandbox checkout on http://localhost:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
