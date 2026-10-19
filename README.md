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

## Not covered yet

- Evidence file uploads for kit 4 (multipart to `files.sandbox.airwallex.com`, JPG/PDF only).
- Connected-account onboarding for kits 5–8 (see the guide's platform setup table).
- Webhooks: verify HMAC-SHA256 on the raw bytes, dedupe on event id, return 200 fast.
