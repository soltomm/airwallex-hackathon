# Harbour House group sales agent

A hotel dashboard where a seller agent handles group inquiries from the first email to negotiation,
booking, deposit, amendment and refund. It works against simulated hotel systems and Airwallex
sandbox payments. This is the build for product spec v2 (Airwallex hackathon).

```
data/groupsales/scenario.json        hotel, PMS/RMS/CRM/policy data, the two RFPs, buyer mandates
src/agentbank/groupsales/
  calc.py       pricing, deposits, attrition allowance, contribution margin, reductions, schedules
  policy.py     hotel authority (seller vs manager vs hard limits) and buyer mandate checks
  engine.py     shared hotel state + every enforced operation (intake, holds, versions, approvals,
                payments, amendments, refunds, webhooks, reset); saved to var/groupsales (or Postgres)
  provider.py   Airwallex sandbox (Payment Intents, refunds) and a simulated twin with the same shape
  seller.py     ClaudeSeller (tools over the engine) and RulesSeller (deterministic, same tools)
  trace.py      record of each seller run and its tool calls, for the dashboard's AI Agent tab
  buyer.py      buyer moves from the private brief (presenter mode) and an optional Claude buyer agent
  extract.py    rule-based RFP extraction
scripts/group_sales.py   server: dashboard, API, agent runner, payment page, webhooks
web/group_sales.html     hotel dashboard          web/group_pay.html   booking payment page
tests/test_group_sales.py
```

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[agent]' pytest
cp .env.example .env        # AWX_CLIENT_ID / AWX_API_KEY (sandbox), ANTHROPIC_API_KEY for the Claude seller
PYTHONPATH=src .venv/bin/python scripts/group_sales.py      # http://localhost:8010
PYTHONPATH=src .venv/bin/python -m pytest tests/test_group_sales.py -q
```

| Variable | Default | Meaning |
|---|---|---|
| `PAYMENTS` | `airwallex` if sandbox keys are set, else `simulated` | where deposits and refunds go |
| `SELLER_MODE` | `claude` if an Anthropic key is set, else `rules` | seller agent; switchable in the dashboard |
| `BUYER_MODE` | `presenter` | `agent` lets the buyer side reply on its own |
| `GS_SELLER_MODEL` / `GS_BUYER_MODEL` | `claude-opus-5-5` | models |
| `PUBLIC_BASE_URL` | `http://localhost:8010` | return URL after hosted checkout |
| `AWX_WEBHOOK_SECRET` | unset | accept signed Airwallex webhooks at `POST /webhooks/airwallex` |
| `GS_PORT`, `GS_VAR` | `8010`, `var/groupsales` | port and state directory |

Airwallex setup: a sandbox account with card payments enabled, and an API key with Payment
Acceptance read/write (payment intents, refunds). Only the backend holds the key. Agents never see it,
and they never see card data either: the buyer enters the card on Airwallex's hosted page. Hosted
checkout redirected back to `http://localhost` in our tests. Webhooks need a public URL, for example a
tunnel. Without one, the backend checks outcomes by asking Airwallex directly: on return from checkout,
through the seller's `check_payment`/`check_refund`, and when a hold expires.

## Demo script (about 6 minutes)

Each deal page has a coloured **next step** card with the buttons for that step:
- amber: your decision, as hotel manager or the buyer's organizer
- purple: your move as the buyer
- grey: the seller agent or Airwallex is working

Presenter tools (clock, edge cases, reset, paste an RFP, seller and buyer modes) are under **Demo controls**.
The **Conversation** tab shows the newest message first; older emails fold under "earlier messages".
The **AI Agent** tab shows each seller run and the actions it took, one line each; click an action for its
inputs, result and what it caused (a refused action is marked in red). The **Activity** tab lists what
each agent and person did, newest first. The **Availability** button under
the inquiries list opens a page with rooms, the Thames Room and breakfast by night, and who is holding them.

1. **Demo controls → Reset scenario → Yes, reset**, then **Run seller** on the card. Northstar gets three questions.
   Summit gets a quote: **£4,920, deposit £1,476**. Summit's **Activity** tab explains why the seller moved
   to Summit first. The **Records checked** tab lists the systems consulted, with timestamps.
2. **Northstar → Send answers.** The seller quotes v1: **£9,600**, 30% deposit, 10% attrition.
3. **Send counteroffer** (20% attrition, 25% deposit). The seller keeps £220 and asks for a **40%
   deposit (£3,840)** in return. Click **Approve** as the hotel manager.
4. **Accept v2.** The deposit is over Northstar's own limits, so click **Approve** as the buyer's
   organizer.
5. **Open payment page ↗** and pay with `4035 5010 0000 0008`. When you return, the booking is
   verified with Airwallex and confirmed.
6. **Jump to 20 Jan**, from the card or Demo controls. Summit's quote lapses. Click **Send change
   request**, then **Accept v3**: **£8,280**, with **£528** owed to the buyer.
7. **Approve** the £528 refund (it's over the seller's £500 limit). The seller creates the refund in
   Airwallex, checks it rather than creating another, and the ledger updates once.
8. **Demo controls → Deliver held notifications.** The late notice is ignored as a duplicate. Final
   state: **£3,312 net received, £4,968 still due**, 10 twins for 20 guests.

Summit can be accepted at any point (**Accept v1**). Its deposit is within Summit's own limits, so
checkout opens straight away.

Other things to show: **Paste a new RFP…** with other dates or a group dinner (it's handed to a
person, with the reason). **Fail the next booking commit** before paying (payment received – booking
exception). **Reject** on the refund (the reduction stands and £528 stays owed).

## What the backend enforces

These rules are in code; prompts don't enforce them, and nothing in an RFP can change them.

- **Authority.** Twin rate at or above £210 and king at or above £180, deposit 30-40% and attrition up
  to 10% are the seller's own call. A rate below the floor (down to £195/£170), attrition up to 20% or
  a deposit of 20-29% needs the manager. A below-floor rate or attrition above 10% forces a 40%
  deposit. Contribution margin must be at least 40%, both with all rooms used and with the full
  release allowance used. Only the named approver can decide.
- **Versions.** Every offer or amendment is an immutable version with a hash of its terms. Approvals
  and acceptance name the version and hash; drafting a changed version makes earlier approvals stale.
- **Buyer mandate.** Accepting above the budget or the deposit limits creates an organizer approval
  bound to that version. Buyer actions need the deal's buyer token.
- **Inventory.** available = capacity − held − booked, per night and room type, plus the Thames Room
  and breakfast covers (60 a day). The first sent offer creates a 24-hour hold. Negotiated versions
  reuse that hold and its expiry. A hold becomes a booking only after a verified payment, in one
  operation. An accepted reduction releases rooms exactly once.
- **Expiry.** When a hold expires, the backend first checks any Payment Intent. A paid intent is
  booked. An unknown or processing one keeps the inventory held and is flagged for the manager. An
  unpaid one is cancelled and the hold released. Checkout then closes; renewing needs a fresh
  availability check, a renewed offer and new acceptance.
- **Money.** Each provider call is preceded by a saved operation (`run-op` request_id, booking,
  version, amount, GBP, original payment). If a creation times out, the backend looks the object up by
  `merchant_order_id` / request_id and never resubmits. A new refund is refused while one is
  unresolved. Pending refunds count against the refundable balance and the £500 limit. A refund
  authorization covers one exact amount, payment and amendment. Ledger entries have unique keys, and
  refund statuses only move forward (RECEIVED → ACCEPTED → SETTLED), so repeated or out-of-order
  events change nothing.
- **Reductions.** Within the allowance (counted cumulatively), before the release deadline and
  before 8 Feb, the deposit is recalculated and the excess refunded. From 8 Feb on, the amendment is
  recorded and the manager reconciles payments. Going over the allowance or past the deadline goes to
  manager review, and the existing agreement stays in effect until a change is accepted.
- **Reset.** A reset creates a new run id and restores the hotel data. Airwallex is not reset.
  In-flight operations are written to `the groupsales_followups document`. Events tagged with an earlier
  run are logged and not applied.

## Verified live (Airwallex sandbox, 2026-11-02)

Changes made after this run (the privacy fix, the two refund fixes and the dashboard polish on
`group-sales-next`) were tested offline only.

The full story ran against the sandbox with the rules seller, paying through hosted checkout in Chrome:
- Deposit Payment Intent `int_sgpvzhfh9hnsz4b1uwl`, £3,840.00 GBP, SUCCEEDED; checked on return,
  booking confirmed.
- Partial refund `rfd_sgpvzhfh9hnsz5eara3_4b1uwl`, £528.00, created as RECEIVED, then checked by the
  seller and found ACCEPTED; ledger updated once. Delivering the held notification later changed
  nothing.

The Claude seller (`claude-opus-5-5`) ran the same story on simulated payments. It asked the three
questions, quoted Summit while waiting, and traded 20% attrition for a 40% deposit at £220. It also
opened checkout after the organizer authorized, prepared the reduction, and executed and checked the
approved refund.

## Checks

`tests/test_group_sales.py` (37 tests, no network, no keys; run with
`PYTHONPATH=src python -m pytest tests/test_group_sales.py -q`) covers:

- the spec's figures and the full demo story;
- cumulative attrition (including releasing nine room-nights, releases after the deadline and after
  the first installment);
- privacy in both directions: the seller's tools and the shared thread never show the buyer's
  mandate, deposit limits or organizer reasons (only that buyer-side authorization is pending), and
  the buyer view never shows floors, costs, margins, hotel checks, records, flags, unsent versions or
  manager approval summaries;
- authority: stale approvals and wrong approvers, hard limits, a 20-29% deposit needing the manager
  and refused alongside attrition above 10% or a below-floor rate, a below-floor rate needing
  approval and a 40% deposit, a package below the 40% margin floor refused, and instructions inside
  a pasted RFP changing nothing;
- inventory and holds: no overselling, negotiated versions reusing the same hold and expiry, hold
  expiry during checkout (unpaid: cancelled and released; processing or unknown: kept and flagged
  for the manager), and a renewed offer after expiry needing a fresh hold and new acceptance;
- payments: a timeout creating the deposit intent recovered by lookup with no second intent, a
  successful payment followed by a booking-commit failure, recovery after a restart;
- refunds: the £500 authority, an unresolved refund blocking a new one (including one stored but
  never status-applied), pending refunds counted when a second reduction is priced, an approval
  binding one exact amount, a rejected authorization, a failed refund, a timeout during refund
  creation, duplicate and out-of-order provider events, an old event after a reset;
- the AI Agent record: every seller run with its tool calls, inputs, results and effects; refused calls
  recorded without changing anything; empty runs dropped; cleared on reset;
- the server: webhook signatures (bad, stale, tampered and missing rejected; valid accepted) and the
  access-token gate (`/pay/`, `/webhooks/`, `/healthz` open, everything else needs the token).

## Not in this build

Kit 10 (Merchant Onboarding). Payments for extra rooms, other dates, upgrades, custom services, split
billing, forecasting, separate department interfaces. Later installments are shown as obligations
and are not charged automatically.
