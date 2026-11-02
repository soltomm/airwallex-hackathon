"""Kit 10: list the hotels with Airwallex Agentic Commerce (Merchant Onboarding API, sandbox).

    python scripts/agentic_commerce.py status
    python scripts/agentic_commerce.py catalogue [--base URL]           # write the feed to var/, no API calls
    python scripts/agentic_commerce.py onboard [--base URL] --yes        # once: keys, checkout URL, secret, OAuth
    python scripts/agentic_commerce.py upsert [--base URL] --yes         # upload the catalogue, 300 per call

Airwallex then hosts an MCP for shopping agents (product search, checkout) at the path `onboard`
prints, calls <base>/agentic-commerce/checkout (scripts/hotel_mcp.py) to price a cart, signed with
AC_CHECKOUT_SECRET, and takes the payment; the payment_intent.succeeded webhook books the stay.
Claude signs shoppers in through the merchant's OAuth server: AC_OAUTH_BASE (OIDC issuer) and
AC_OAUTH_JWKS. The catalogue is every room-night of the next 60 days for data/hotels.json plus
hotels that signed up on /join; run upsert daily to keep the window moving. See
agentbank/agentic_commerce.py.
"""
import argparse
import json
import os
import sys
from datetime import date
from urllib.parse import urlsplit

from _bootstrap import ROOT, connect

from agentbank.agentic_commerce import API, batches, catalogue, tsv
from agentbank.airwallex import AirwallexError
from agentbank.config import load_dotenv
from agentbank.hotel_onboarding import HotelOnboarding
from agentbank.offers import load_hotels

load_dotenv(ROOT / ".env")


def hotels() -> dict:
    live = load_hotels(ROOT / "data/hotels.json")
    HotelOnboarding(live, ROOT / "var/hotels_onboarded.json")   # adds hotels that signed up themselves
    return live


def base_url(arg: str | None) -> str:
    url = arg or os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        sys.exit("pass --base https://<your host> (where /catalogue/<id> and /agentic-commerce/checkout are served)")
    return url.rstrip("/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("status", "catalogue", "onboard", "upsert"))
    ap.add_argument("--base", help="public base URL of scripts/hotel_mcp.py (default: PUBLIC_BASE_URL)")
    ap.add_argument("--yes", action="store_true", help="onboard/upsert: really call Airwallex")
    args = ap.parse_args()

    if args.command == "catalogue":
        rows = catalogue(hotels(), base_url(args.base), date.today())
        (ROOT / "var").mkdir(exist_ok=True)
        (ROOT / "var/agentic_catalogue.json").write_text(json.dumps(rows, indent=1))
        (ROOT / "var/agentic_catalogue.tsv").write_text(tsv(rows))
        per_hotel = {}
        for r in rows:
            per_hotel[r["brand"]] = per_hotel.get(r["brand"], 0) + 1
        for name, n in per_hotel.items():
            print(f"{n:4}  {name}")
        print(f"{len(rows):4}  products -> var/agentic_catalogue.json, var/agentic_catalogue.tsv")
        return 0

    client, _ = connect()
    if args.command == "status":
        try:
            cfg = client.get(f"{API}/config")
        except AirwallexError as e:
            if e.status == 404 and e.code == "MERCHANT_CONFIG_NOT_FOUND":
                print(f"{API} is live but we are not onboarded yet: run `onboard`")
                return 1
            if e.status == 404:   # the gateway's HTML 404: the route does not exist on this sandbox
                print(f"{API}/config: 404 ({e.code}). Merchant Onboarding API not deployed on this sandbox yet")
                return 1
            raise
        print(json.dumps({k: v for k, v in cfg.items() if k != "product_upload"}, indent=1))
        return 0

    if not args.yes:
        sys.exit(f"{args.command} calls Airwallex; add --yes")
    if args.command == "onboard":
        base = base_url(args.base)
        secret = os.environ.get("AC_CHECKOUT_SECRET", "")
        oauth_base, jwks = os.environ.get("AC_OAUTH_BASE", ""), os.environ.get("AC_OAUTH_JWKS", "")
        if len(secret) < 16 or not base.startswith("https://") or not (oauth_base and jwks):
            sys.exit("needs an https --base, AC_CHECKOUT_SECRET (16+ characters, also set on the server), "
                     "AC_OAUTH_BASE and AC_OAUTH_JWKS")
        host = urlsplit(base)
        cfg = client.post(f"{API}/onboard", {
            "psp_config": {"api_key_data": {"client_id": client.config.client_id, "api_key": client.config.api_key}},
            "apis": {"create_checkout": f"{base}/agentic-commerce/checkout", "secret_key": secret,
                     "oauth": {"acp": {"oauth_base": oauth_base, "oauth_jwks": jwks}}},
            "whitelisted_image_domains": [f"{host.scheme}://{host.netloc}"]})
        print(json.dumps({k: v for k, v in cfg.items() if k != "product_upload"}, indent=1))
        if acp := (cfg.get("mcp_endpoints") or {}).get("acp"):
            print(f"\nShopping agents connect to: https://mcp.sandbox.airwallex.com{acp}")
        return 0

    rows = catalogue(hotels(), base_url(args.base), date.today())
    for i, body in enumerate(batches(rows), 1):
        out = client.post(f"{API}/products", body)
        print(f"batch {i}: added {out.get('records_added')}, updated {out.get('records_updated')}, "
              f"failed {out.get('records_failed')}" + (f" (errors: {out['error_gcs_file_loc']})"
                                                       if out.get("error_gcs_file_loc") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
