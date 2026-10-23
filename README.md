# agentbank: Airwallex Agentic Banking Hackathon starter

A kit-agnostic foundation for any of the ten starter kits. It implements the guide's
"build around a financial decision" rules so your team only writes the decision logic:

| Guide rule | Where it lives |
|---|---|
| Keep amounts, limits, reserves and validation in code | `policy.py`, `money.py` (Decimal, never floats) |
| Duplicate protection; reuse a `request_id` only to retry the same operation | `ledger.py` |
| Treat non-success on money movement as ambiguous; look up before retrying | `airwallex/client.py` → `AmbiguousResult` |
| Bind each approval to amount, currency, counterparty, terms and evidence | `approvals.py` (fingerprinted, single-use, expiring) |
| Put sandbox-only simulation calls behind one interface | `simulation.py` |
| Give the model typed tools, not credentials | `agent/tools.py` (strict schemas) |
| Show what the agent did and why | `audit.py` → `var/audit.jsonl` |

## Setup

```sh
python3.13 -m venv .venv && source .venv/bin/activate   # Python 3.10+ (macOS system python3 is 3.9)
pip install -e ".[agent]"
cp .env.example .env                                  # bare values: no quotes or spaces
python scripts/check_setup.py                         # login, Global Accounts, balances
```

Other scripts (run from the repo root):

| Script | Does |
|---|---|
| `scripts/fund_sandbox.py 25000 USD` | Simulates a deposit into a Global Account in that currency, creating one only if needed |
| `scripts/seed_beneficiaries.py` | Creates US (LOCAL), GB (LOCAL) and DE (SWIFT) synthetic suppliers from `data/beneficiaries.json` |
| `scripts/run_agent.py <beneficiary_id>` | Example treasury agent: three invoices, scarce cash above a reserve floor, approvals in the console |

Tests (offline, no network): `python -m unittest discover tests`

## Layout

```
src/agentbank/
  config.py           .env loading; rejects quoted or padded credentials (the HTML-403 trap)
  money.py            Money(amount: Decimal, currency) in major units
  airwallex/
    client.py         auth + token cache, retries (GET on 5xx/429; POST only on 429), x-on-behalf-of
    api.py            thin wrappers: balances, Global Accounts, FX, beneficiaries, transfers,
                      issuing, payments/disputes, connected-account transfers, charges
    states.py         normalised transfer and card-transaction outcomes
    errors.py         AirwallexError (definite) vs AmbiguousResult (unknown outcome)
  simulation.py       deposits, transfer transitions, card authorizations, disputes
  ledger.py           one request_id per business operation, at-most-once execution
  approvals.py        ApprovalRequest fingerprinting, ApprovalStore, ConsoleApprover
  policy.py           PaymentPolicy example: reserve floor, autonomous limit, confidence, SWIFT fee
  audit.py            append-only JSONL with secrets redacted
  agent/
    tools.py          tool schemas + handlers (policy -> approval -> ledger -> API)
    loop.py           manual tool-use loop on claude-opus-5-5, refusal fallbacks on
```

## Adapting it to your kit

1. **Write the policy first** (`policy.py`). The decision your demo turns on (reserve
   floor, forecast confidence, dispute economics, tenant isolation) belongs in code that
   returns `EXECUTE`, `NEEDS_APPROVAL` or `REJECT`, with reasons.
2. **Add tools** in `agent/tools.py`, copying `pay_beneficiary`: validate input, then policy,
   then approval if needed, then `ledger.run(key, call)`, then audit. Read-only tools can skip the ledger.
3. **Pick ledger keys from business identity**, not attempts: `transfer:INV-1001`,
   `fx:payroll-2026-w43:EUR`. A deliberate replacement after a bounced transfer is a
   *new* operation, so give it a new key (e.g. `transfer:INV-1001:replacement-1`).
4. **Drive the scenario's "new information" through `simulation.py`** (a late deposit,
   a transfer moved to FAILED, a declined authorization) and let the agent re-plan.

## Sandbox behaviour verified on 2026-10-19

These came from running kits 1–3 against a real sandbox account. Some contradict the guide.

- **Declined card transactions also have `transaction_type: CLEARING`.** Use
  `states.card_outcome()`, which decides on `status`/`failure_reason`.
- **A failed transfer reads back as `CANCELLED`** with `failure: {code, message, details.type}`.
  The simulator recorded `INCORRECT_ROUTING` when asked for `BENEFICIARY_BANK_RETURNED`, so
  key decisions on what Airwallex returns, not on what you simulated.
- **Deposits post within a few seconds, not always instantly.** Re-read balances before re-planning.
- **Kit 4 card payments:** confirming a PaymentIntent with raw card details returns
  *"account is not enabled for native API access"*. Ask devhelp@airwallex.com to enable it,
  or pay through the hosted payment page / a payment link with test card `4035501000000008`.
- Per-transaction card limits are inclusive ($100.00 clears on a $100 limit; $100.01 fails).
- FX conversions settle immediately; quotes are single-use; leave `x-api-version` off FX calls.
- Sandbox rate limits: 10 req/s per endpoint. The client backs off on 429; don't fire parallel bursts in a demo.

## Hotel offer negotiation (our project)

Hotels run their own seller agents (Claude, `seller_agent.py`) that negotiate personalised offers
with the traveller's Claude, within per-hotel rules in `data/hotels.json`. `SellerPolicy`
(`offers.py`) decides in code what a seller may commit to. Deposits are paid by card through
Airwallex (pre-auth hold, released on free cancellation, captured late or at check-out) or by the
traveller's agent on Metal, under an on-chain mandate capped at the budget (`bookings.py`).

```sh
pip install -e ".[agent]"                         # anthropic + mcp
scripts/metal_traveller_setup.sh 0.50             # once: traveller's Metal account + mandate (budget in *USDZ)
claude mcp add hotels -- $PWD/.venv/bin/python $PWD/scripts/hotel_mcp.py    # search, negotiate, book
claude mcp add metal-wallet -- $PWD/scripts/metal_wallet_mcp.sh              # pay within the mandate
(cd metal && npx tsx hotel-pay.ts)                # keep running: x402 endpoint for Metal deposits
```

Then ask Claude for a Paris hotel, 2 guests, 13-15 Nov 2026, breakfast + late checkout + free
cancellation, EUR 500 total. Card checkouts open on `http://127.0.0.1:8009/pay/<ref>` (test card
`4035 5010 0000 0008`). Hotel staff: `python scripts/hotel_ops.py list` and
`python scripts/hotel_ops.py <ref> status|cancel|check-out|no-show`.

**Live negotiation room (MCP App).** In Claude Desktop and claude.ai, `search_hotels` opens a panel in
the chat that follows every round live: both agents' messages, the hotel agent's steps as it works
(never its floors), offers with Book buttons (they post the choice into the chat, so Claude still
confirms terms) and bookings as deposits are held or paid. Source in `ui/`; after editing,
`cd ui && npm install && npm run build` regenerates `web/negotiation_app.html`. `node test/run.mjs`
renders it in a test host with `test/feed.json` (regenerate with `.venv/bin/python ui/test/make_feed.py`)
and screenshots both themes into `ui/test/out/`. Terminal clients get the same rounds as text.

**Sharing with friends (Render).** `render.yaml` deploys the same server over HTTP: Render >
New > Blueprint > this repo, then fill in `ANTHROPIC_API_KEY`, `AWX_CLIENT_ID`, `AWX_API_KEY`.
The MCP URL is `https://<service>.onrender.com/mcp/<HOTELS_ACCESS_TOKEN>` (the token is generated;
find it under the service's Environment). Friends add it in Claude (Settings > Connectors > Add custom
connector) or ChatGPT (developer mode connectors). Each chat session gets its own negotiations, offers,
bookings and panel; hotel inventory is shared. Hosted is card-only, capped per session and per day
(`HOTELS_MAX_*`, `HOTELS_DAILY_USD`), and resets on every deploy or restart (no persistent disk).
Try it locally first: `HOTELS_ACCESS_TOKEN=<16+ chars> PORT=8765 python scripts/hotel_mcp.py --http`.

**Evaluating the seller agent.** `python scripts/eval_seller.py [--runs 3] [--only name,...]` plays
scripted buyers against the real seller agent from fresh inventory and grades each conversation in
code: the expected outcome (closes a feasible deal, declines honestly, holds the line), no leaks of
floors, costs or occupancy, every quoted price backed by a committed offer, and deals within budget.
About $0.15 per conversation on Claude Opus 5.5; transcripts go to `var/evals/`.

Offline: `python scripts/negotiation_demo.py` (scripted negotiation, no LLM) and
`python scripts/payments_smoke.py card|metal` (booking and payment, no LLM).

**Guest currency (card).** `book_offer(..., currency="USD"|"GBP")` holds the deposit in the guest's
currency while prices, floors and offers stay in EUR (`guest_currency.py`). It uses Airwallex
Multi-Currency Pricing: a 24-hour conversion quote is applied to the fresh PaymentIntent, which then reads
in GBP/USD (`original_amount` keeps the EUR). The booking's `fx` stores the rate and both amounts;
late-cancellation captures and the check-out increment use that locked rate, rounded half-to-even. If
Airwallex can't quote, the deposit stays in EUR and `fx.error` says why. `payments_smoke.py card GBP` tries it.
Verified in the sandbox on 2026-10-23 (EUR 300 -> GBP 260.31 / USD 348.09). Caveats: Airwallex guarantees
the settled amount only if captured within 12 hours, so a capture weeks later may settle at a newer rate.
The sandbox refuses a conversion quote on an intent tagged with a connected account ("not allowed ... for
platform payments"), so guest-currency intents are left untagged and still split to the hotel after capture.

**Hotel payouts (Airwallex connected accounts).** The platform owns each card payment ("collect payments
directly") and tags it with the hotel's connected account; after every capture (late cancellation,
no-show, check-out, non-refundable charge) a FundsSplit sends the hotel its share and the commission
(`platform.commission_pct` in `data/hotels.json`, 12%) stays with the platform. The booking's
`settlement` records both, in EUR and, for guest-currency bookings, in the card's currency. A failed split
leaves the capture standing; `hotel_ops.py <ref> settle` retries it. `scripts/onboard_hotels.py` onboards
each hotel. Sandbox lessons (2026-10-23): an ACTIVE connected account still fails PaymentIntents with
"not enabled for payment acceptance" until it has `store_details` (store names in unaccented Latin
letters) and the `payments_visa`/`payments_mastercard` capabilities are enabled, a few seconds after
activation; a FundsSplit is only accepted once the intent has SUCCEEDED. Verified live: EUR 520 tagged
intent split EUR 457.60 to the hotel, and an untagged GBP 451.20 intent split GBP 397.06 (both RELEASED).

**Revenue desk (hotel dashboard).** The offer desk logs every search, negotiation, policy check, committed
offer and booking to `var/demand.jsonl` (hotel side: it holds floors). The dashboard shows the funnel,
how much of the floor-to-list band deals kept, unmet demand and why it was lost (below the floor with the
gap, sold out, offer not taken), demand by night and by extra, and payouts. Locally:
`http://127.0.0.1:8009/dashboard` (or `python scripts/hotel_ops.py insights`); hosted:
`<service URL>/dashboard/<HOTELS_ADMIN_TOKEN>` (generated by render.yaml; off when unset).

The dashboard also acts. **Suggested rule changes** (`rule_tuning.py`) turn its evidence into one concrete
change each: a lower floor on the nights where at least two travellers walked away below it (capped at 15%
a night, with how many of those deals it would have closed), a 15% higher rate on busy nights that keep
being asked for, or a wider autonomy band when deals close near the floor. **Apply** edits the hotel's live
rules (per-night overrides in `Hotel.night_floors` / `night_rates`), so seller agents price with them from
their next step; applied changes persist in `var/rule_changes.json`, can be reverted, and show conversion
before and after. **Booking actions** (refresh, check-out, no-show, cancel) run the same payment flows as
`hotel_ops.py`, so a check-out from the dashboard captures and pays the hotel out. Buttons that move money
or change rules ask for a second click.

- Metal amounts use a demo rate of 0.001 *USDZ per EUR (faucet amounts are small).
- Metal testnet needs `@metalntwx/*` 0.3.2; 0.3.0's account factory call reverts.
- A mandate's budget never resets: after test payments, bind a fresh one with `metal-agent mandate renew`.
- Metal refunds for free cancellations are recorded as due, not sent.

## Not covered yet

- Evidence file uploads for kit 4 (multipart to `files.sandbox.airwallex.com`, JPG/PDF only).
- Connected-account onboarding for kits 5–8 (see the guide's platform setup table).
- Webhooks: verify HMAC-SHA256 on the raw bytes, dedupe on event id, return 200 fast.
