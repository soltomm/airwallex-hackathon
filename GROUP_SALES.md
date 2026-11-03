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

Start from **Reset scenario**. It is 09:00, 11 Jan 2027, with two inquiries in the queue. The seller
mode can be Claude or rules; the buyer is the presenter.

1. **Run seller.** Northstar has the earlier deadline but is missing details. The seller asks for the
   meeting layout, the legal payer and whether the budget includes taxes. It notes why it is moving to
   Summit, then quotes Summit: 24 king room-nights at £190 plus 24 breakfasts, **£4,920, deposit
   £1,476**, held for 24 hours. Expand *Records consulted* to show the PMS, meeting space, F&B, RMS,
   CRM and policy checks with timestamps.
2. **Buyer console → Send as buyer** (the answers come pre-filled from Northstar's private brief). The
   seller quotes v1: £220 twin + breakfast = £250 per room-night, **£9,600**, 30% deposit, 10%
   attrition. The hold covers 36 twin room-nights and the Thames Room. The inventory strip updates.
3. **Send the counter** (20% attrition, 25% deposit). The seller keeps the £220 rate and trades 20%
   attrition for a **40% deposit (£3,840)**. v2 pauses for the **hotel manager**. Approve it in the
   Approvals panel; the seller sends v2 on the same hold and expiry.
4. **Send accept.** The deposit is over Northstar's £3,000 and 30% limits, so the buyer side pauses
   for its **organizer**. Approve it. The approvals and the acceptance all carry v2's hash.
5. The seller opens checkout. **Open booking payment page → Pay deposit with Airwallex** redirects to
   the hosted page; pay with `4035 5010 0000 0008`. On return, the backend checks the Payment Intent
   with Airwallex (amount, currency, `merchant_order_id`), records the receipt once and converts the
   hold to a booking. The booking is confirmed.
6. **Clock → 20 Jan 09:00.** Summit's unaccepted quote lapses and its 8 kings go back to inventory.
   Send Northstar's **change request** (two fewer twins on all three nights). The seller checks 6
   room-nights against the allowance of 7 and the 24 Feb deadline, and sends amendment v3: **£8,280,
   revised deposit £3,312, refund £528**. Send accept. The rooms are released once (10 twins per
   night) and £528 is recorded as owed.
7. The refund is over the seller's £500 authority, so the seller asks the manager to authorize **£528
   GBP against that payment and amendment v3**. Approve it. The seller creates the refund in Airwallex
   and saves its id. The completion notification is held back ("hold refund notifications"), so the
   seller sees an unresolved refund. It **checks the existing refund** instead of creating a new one,
   and when Airwallex reports ACCEPTED the ledger is updated once.
8. **Deliver held notifications.** The late notice is recognised as already reconciled and the ledger
   doesn't change. Final state: £3,840 received, £528 refunded, **£3,312 net, £4,968 still due**
   (£828 by 8 Feb, £4,140 by 3 Mar). The operating instructions show 10 twins and 20 guests, the same
   meeting, and breakfast guaranteed for 24.

Other things to show: **Paste RFP…** with other dates or a group dinner (handed to a person with the
reason), or one asking for more twins than are left (the seller offers what can be held and explains
the difference). Also **fail next booking commit** before paying ("payment received – booking
exception", routed to the manager, no second charge), and **Reject** on the refund approval (the
reduction stands, £528 stays owed, and the discrepancy is escalated).

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

`tests/test_group_sales.py` (18 tests, no network) covers the spec's figures, the full demo story,
cumulative attrition (including releasing nine room-nights, releases after the deadline and releases
after the first installment), refund authority, stale approvals and wrong approvers, hard limits,
overselling, duplicate emails and threading, out-of-scope requests, a refund left unresolved, a
rejected refund authorization, a failed refund, a timeout during refund creation, hold expiry during
checkout, a successful payment followed by a booking-commit failure, duplicate and out-of-order
provider events, an old event after a reset, and recovery after a restart.

## Not in this build

Kit 10 (Merchant Onboarding). Payments for extra rooms, other dates, upgrades, custom services, split
billing, forecasting, separate department interfaces. Later installments are shown as obligations
and are not charged automatically.
