# Harbour House group sales agent

Third-party sourcing, partner capacity and bundled master proposals are documented
in [Vendor workflows](docs/VENDOR_WORKFLOWS.md). They are inquiry-owned persisted
workflows; provider delivery/confirmation is simulated and vendor payouts are
tracked without transferring money.

A hotel dashboard where a seller agent handles group inquiries from the first email to negotiation,
booking, deposit, amendment and refund. It works against simulated hotel systems and Airwallex
sandbox payments. This is the build for product spec v2 (Airwallex hackathon).

```
data/groupsales/scenario.json        hotel, PMS/RMS/CRM/policy data, the two RFPs, buyer mandates
src/agentbank/groupsales/
  calc.py       pricing, deposits, attrition allowance, contribution margin, reductions, schedules
  bookable.py   expands the scenario's bookable window (defaults + per-night overrides) into a calendar
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

The dashboard is the hotel's. You act only as the hotel: the seller agent works on its own, waking on
every new email, buyer reply, decision, payment and clock move, and stops at decisions that need the
hotel manager. There is no button to run it. The buyer answers by email, in its own **mailbox** page
outside the dashboard (`/inbox/<ref>`): open it from the deal's card. Replies, acceptances, the buyer's organizer sign-off and the payment link are all there.

Inquiries are rows in the overview table; clicking one opens its deal page in a side drawer.
Each deal page has a coloured **next step** card:
- amber: your decision, as hotel manager
- purple: waiting for the buyer, with a link to the buyer's mailbox
- grey: the seller agent or Airwallex is working
- red: handed to a person, or the seller paused after a payment-provider error (with **Retry**)

The card also shows the seller's last action. The **AI Agent** tab shows each seller run and the
actions it took, one line each; click an action for its inputs, result and what it caused (a refused
action is marked in red). The **Activity** tab lists what each agent and person did, newest first. The
**Conversation** tab shows the newest message first; older emails fold under "earlier messages".
The sidebar's property tools (on a phone, expand **Property tools** first) hold **Room availability**
(rooms, the Thames Room and breakfast by night), **Seller rules** (the rules the seller works within, see
below), **Hotel payout account** and **↻ Reset scenario**. **＋ New inquiry** sends a generated or
hand-written RFP and sets who answers for the buyer.

1. **↻ Reset scenario → Yes, reset.** The seller starts on its own. Northstar gets three
   questions. Summit gets a quote: **£4,920, deposit £1,476**. Summit's **Activity** tab explains why the
   seller moved to Summit first. The **Records checked** tab lists the systems consulted, with timestamps.
2. Open **Maya's mailbox ↗** from Northstar's card and click **Send answers**. The seller quotes v1:
   **£9,600**, 30% deposit, 10% attrition. It arrives in the mailbox with the offer attached.
3. In the mailbox, **Send counteroffer** (20% attrition, 25% deposit). The seller keeps £220 and asks for a
   **40% deposit (£3,840)** in return. On the dashboard, click **Approve** as the hotel manager.
4. In the mailbox, **Accept v2.** The deposit is over Northstar's own limits, so its organizer signs off:
   **Approve as organizer**, still in the mailbox. The dashboard only shows that the buyer is signing off.
5. The seller opens checkout. In the mailbox, **Open payment page ↗** and pay with `4035 5010 0000 0008`.
   When you return, the booking is verified with Airwallex and confirmed. The **Hotel payout** card
   shows £3,456 paid to the hotel's connected account by funds split and £384 kept as the fee.
6. **Jump to 20 Jan (demo)**, on Northstar's card. Summit's quote lapses. In the mailbox,
   **Send change request**, then **Accept v3**: **£8,280**, with **£528** owed to the buyer.
7. On the dashboard, **Approve** the £528 refund (it's over the seller's £500 limit). The seller creates
   the refund in Airwallex, checks it rather than creating another, and the ledger updates once.
8. Final state: **£3,312 net received, £4,968 still due**, 10 twins for 20 guests. Hotel payout: £475.20 taken
   back for the refund, **£2,980.80 net to the hotel, £331.20 Group Desk fee**. The simulated twin holds
   back the refund's notification; delivering it late (`POST /api/notifications/release`, no dashboard
   button) changes nothing, and `scripts/e2e_demo.js` checks that.

Summit can be accepted at any point (**Accept v1** in Alex's mailbox). Its deposit is within Summit's
own limits, so checkout opens straight away.

Other things to show: **＋ New inquiry → Write your own** with dates the hotel can't sell (for example 11–14 May).
The seller doesn't hand it off: it replies that those dates aren't available and offers dates that
are, checked against live inventory. In the buyer's mailbox, **Take 10 to 13 March 2027** (or type a
reply naming the dates) and the seller quotes. A request for a service the hotel doesn't offer (a
group dinner) is handed to a person: the buyer gets an acknowledgement, and the card offers **Hand
back to seller**, with an optional instruction the seller follows, or **Close inquiry**. Fail the next booking commit before paying (`POST /api/sim {"fail_next_commit": true}`;
payment received – booking exception). **Reject** on the refund (the reduction stands and £528 stays owed). **＋ New inquiry → Buyer replies →
Automatically (buyer agent)**, to let the buyer side answer on its own.

### Seller rules

**Seller rules** lists what the seller may do alone, what needs the hotel manager and what is never
possible, then every rule hotel staff can change, with its default: target rates, the seller's floor
and the absolute minimum for each room type, deposit and attrition limits, the minimum margin, how long
offers last and the refund limit. **Save changes** checks the whole set first (for example, a floor
above the target is refused with the reason) and records the change in Activity. Changes apply to
offers and changes made after saving; versions already made keep their terms. They are stored apart
from the scenario, so they stay after a reset, and **Restore defaults** undoes them. The Claude seller's
prompt is built from the same values, so it never quotes old limits. The fixed rules (services, prices,
payment schedule, cancellation, capacity, costs) are listed underneath.

### Hotel payouts (Airwallex Payments for Platforms)

The hotel doesn't hold the card payments itself. The platform (**Group Desk**, `platform` in
`scenario.json`) has the Airwallex account; the hotel is one of its **connected accounts**
(`GS_HOTEL_ACCOUNT`, `acct_…`), onboarded and verified by Airwallex. The platform collects each deposit
for the hotel ("collect payments directly": the intent is tagged with `connected_account_id`), and once
the backend has verified the payment and confirmed the booking it pays the hotel its share by **funds
split**. The commission (10%, `commission_bps: 1000`) stays with the platform. Airwallex then pays the
hotel out to its own bank account.

A refund to the buyer is paid by the platform, and a **funds split reversal** takes the hotel's share of
it back; the platform returns its commission on the refunded part. In the demo:

| | Buyer paid | To the hotel | Group Desk fee |
|---|---|---|---|
| Deposit | £3,840 | £3,456 (funds split) | £384 |
| Refund | −£528 | −£475.20 (reversal) | −£52.80 |
| **Net** | **£3,312** | **£2,980.80** | **£331.20** |

The **Hotel payout** card on the deal shows these figures, the connected account and the status. The
same money rules apply as for deposits and refunds: the operation is saved before the call, a timeout is
looked up (`GET /pa/funds_splits?source_id=`) and never resent as a second split, a reversal is resent
only with its original `request_id` (Airwallex refuses a reused one), and each payout and reversal is
recorded once in a separate payout ledger. A refused split leaves the booking standing and the deposit
with the platform; **Retry payout** sends it again once the cause is fixed. Payouts are backend
bookkeeping: no agent tool moves them, and the buyer's view never shows them. Without a hotel account
(live Airwallex), deposits are collected untagged and the card shows "No hotel account connected".

**Onboarding the hotel.** Property tools → **Hotel payout account** shows the hotel's connected account
and offers **Onboard Harbour House**. On the server (so it works on Render, inside the key's IP
whitelist) it creates a connected account named Harbour House under your Airwallex account
(`POST /accounts/create`), submits it, activates it in the sandbox and enables card payments (Visa and
Mastercard capabilities), reusing the onboarding the Hotel Negotiation Desk verified live. It takes a
minute or two and runs in the background; progress is saved after each step, and a retry resumes the
same account with the same `request_id` rather than opening another. The account is used only once it
accepts cards, because an intent tagged with an account that doesn't is refused. It is stored apart from
the scenario state, so a reset keeps it; staging and the live demo keep their own (separate storage
namespaces), or share one by setting `GS_HOTEL_ACCOUNT`, which always wins. **Check with Airwallex**
reads the account's status and capabilities. Your Airwallex account must have Payments for Platforms
enabled and an API key allowed to create accounts; otherwise the drawer shows Airwallex's refusal.

### When a payment provider refuses

If Airwallex refuses the deposit intent or a refund, the seller stops on that deal instead of trying
again on every wake, and the card turns red with the error and a **Retry** button. Fix the cause (for
example the key or its IP whitelist), then retry.

## What the backend enforces

These rules are in code; prompts don't enforce them, and nothing in an RFP can change them.

- **Authority.** Twin rate at or above £210 and king at or above £180, deposit 30-40% and attrition up
  to 10% are the seller's own call. A rate below the floor (down to £195/£170), attrition up to 20% or
  a deposit of 20-29% needs the manager. A below-floor rate or attrition above 10% forces a 40%
  deposit. Contribution margin must be at least 40%, both with all rooms used and with the full
  release allowance used. Margin counts delivery costs per room-night used (£70 twin, £60 king) and
  displacement: what a blocked room would have earned from other guests, £80 twin / £60 king at full
  demand times that night's demand: the share of the group allocation other business has already
  taken, read from the same calendar the availability page shows (`hotel.background_demand`), so
  sold-out nights count 100% and quiet ones little. The demo stay's allocation is set by hand, so
  `rms.displacement.demand_by_night_pct` states its demand: a busy week (the nights before it are
  sold out). Rooms released before the cutoff are counted 50% resold. On the demo dates (80% / 80% / 64%)
  the floor bites: £205 passes with 10% attrition but not 20%, and £200 or less is refused; on a
  quiet January weekend the whole manager band down to £195 passes. Only the named approver can decide. Every approval names each rule it
  crosses (what the seller, or the buyer, could agree alone and what is asked) and what approving and
  rejecting each do, and the card shows exactly that. The engine refuses a manager request that names no
  crossed rule, so the manager is only asked when a seller rule is broken. A hand-off to a person is not
  an approval: it says what stops the seller (`why`) and what a person should do (`todo`).
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
- **Reductions.** The contract prices every release, so none is escalated for judgement. Room-nights
  within the allowance (counted cumulatively) and before the release deadline come off the total;
  any beyond the allowance, or released after the deadline, go back to inventory but stay payable at
  the contracted rate as an attrition charge, itemised on the change. Before 8 Feb the deposit is
  recalculated on the new total and the excess refunded; from 8 Feb on, the amendment is recorded and
  the manager reconciles payments if the total changed. The only concession is a waiver of the
  attrition charge: it gives away hotel money, so it needs the hotel manager's approval of that exact
  version and amount, and a declined waiver leaves the change to be offered with the full charge. The
  existing agreement stays in effect until the buyer accepts a change.
- **Other dates.** The seller can only offer dates inside the group calendar (11 January to 31 December
  2027), arriving at least 31 days after the scenario clock, with the rooms free at that moment
  (`offer_dates` refuses anything else), and it can't quote dates that aren't available.
- **Hand-offs.** Only an inquiry handed to staff with no booking can be handed back or closed. A staff
  instruction stays in front of the seller until it next writes to the buyer.
- **Reset.** A reset creates a new run id and restores the hotel data. Airwallex is not reset.
  In-flight operations are written to `the groupsales_followups document`. Events tagged with an earlier
  run are logged and not applied.

## Verified live (Airwallex sandbox)

### 2026-11-09: hotel payouts (Payments for Platforms), on staging

The full demo story ran on the staging deploy (Render) against the Airwallex sandbox, with the hotel
onboarded from the dashboard as a connected account named Harbour House under the platform account:

| Step | Amount | Airwallex id | Status |
|---|---|---|---|
| Deposit Payment Intent (tagged with the hotel's connected account) | £3,840.00 | `int_sgpvtdh4nhnzikulbcb` | SUCCEEDED |
| Funds split to the hotel | £3,456.00 | `spt_sgpvn2l8vhnzimhiz8z` | CREATED |
| Refund to the buyer | £528.00 | `rfd_sgpvzc9hjhnzipknrw4_kulbcb` | ACCEPTED |
| Funds split reversal (hotel's share of the refund) | £475.20 | `rev_sgpvn2l8vhnzipma9ic` | CREATED |

Result on the dashboard: £3,312 net received, £2,980.80 net to the hotel, £331.20 Group Desk fee.
Airwallex accepted the split and the reversal, but both were still CREATED when checked, and the
connected account's balance had not moved: the money follows when the card payment settles, which had
not happened in the sandbox at the time. Not yet observed: the split reaching RELEASED/settled and the
hotel's balance changing.

### 2026-11-02: deposit and refund, before payouts

Changes made after this run (the privacy fix, the two refund fixes and the dashboard polish on
`group-sales-next`) were tested offline only until the 2026-11-09 run above, which exercised them again
on the same story.

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

`tests/test_group_sales.py` (40 tests, no network, no keys; run with
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
- hotel payouts: the intent tagged with the hotel's connected account, £3,456 split to the hotel and
  £384 kept, £475.20 taken back for the £528 refund (hotel £2,980.80 + fee £331.20 = £3,312 net
  received), late and repeated refund events taking nothing back twice, a split timeout recovered by
  lookup with no second split, a refused split leaving the booking standing until **Retry payout**, a
  reversal timeout resent with the same request_id, no payouts without a connected account, and none
  of it in the buyer's view;
- the server: webhook signatures (bad, stale, tampered and missing rejected; valid accepted) and the
  access-token gate (`/pay/`, `/webhooks/`, `/healthz` open, everything else needs the token).

### Randomized scenarios

`tests/fuzz_group_sales.py` checks the rules against requests nobody wrote by hand. Each case (a seed)
starts a fresh hotel and takes a random walk of 60-80 steps:

- **Requests:** new RFPs with random sizes, room types, dates (some outside the stay), budgets,
  meeting rooms, unsupported extras, injected instructions and duplicate emails.
- **Moves:** seller runs; buyer answers, counteroffers, acceptances (sometimes of an old version or
  with a wrong token), change requests and declines; approvals by the right and the wrong person.
- **Money and time:** good and declined cards, return-page and webhook confirmations, provider
  timeouts and failures, clock jumps, held and replayed notifications, events from earlier runs.
- **Restarts and resets.**

Two sellers drive it:

- **rules:** the demo's own seller;
- **chaos:** random, schema-valid calls to the Claude seller's tools, standing in for an agent that
  misreads or is manipulated.

After every step it checks:

- inventory adds up to the holds and bookings on file, and is never oversold;
- every sent offer is within the hard limits and carries the manager approval it needs;
- an acceptance beyond a buyer's mandate has the organizer's approval;
- every booking has a verified payment and a ledger receipt;
- there is one payment intent per checkout, and refunds never exceed receipts or the capture;
- refunds above £500 are authorized, and at most one refund is unresolved at a time;
- neither side sees the other's private data;
- the state survives a restart, and nothing fails except as a refusal.

```bash
PYTHONPATH=src python tests/fuzz_group_sales.py --cases 500             # prints coverage and any failures
PYTHONPATH=src python tests/fuzz_group_sales.py --seed 72 --sellers rules -v   # replay one case step by step
ANTHROPIC_API_KEY=... PYTHONPATH=src python tests/fuzz_group_sales.py --cases 10 --claude   # live Claude seller
```

The normal suite runs 15 cases per seller (`tests/test_group_sales_fuzz.py`; set `FUZZ_CASES` for
more). Planted bugs are caught within the first two seeds: authority checks removed, an inventory leak
on booking, a booking without payment, hotel flags in the buyer view. The first runs found two real
bugs, now fixed with regression tests:

- a change request for more rooms than were booked crashed the seller, and blocked it for every deal;
- the rules seller crashed if questions had gone out before the inquiry was read;
- an organizer approving after the offer had lapsed left the approval marked approved in memory but
  not on disk, with no acceptance recorded; it is now refused as stale before anything changes.

Last full runs: 1,000 of 1,000 cases passed (500 per seller, 80 steps each, about 8 minutes),
ending with 772 confirmed bookings, 91 refunds and 82 changes in manager review. Then 5,000 of
5,000 new cases passed (seeds 1000-3499 per seller, 100 steps each, about 70 minutes), ending with
4,156 confirmed bookings, 470 refunds, 17 refunds failed and handled, and 43 payments received with
a booking exception, plus deals handed to a person or declined.

### Generated inquiries

`src/agentbank/groupsales/generator.py` writes new inquiries for automated runs and demos. Each one is an
RFP email plus a private brief for the buyer agent (its answers, goals, limits and a later change). The
generator only writes the inbound: the seller agent, the buyer agent and the engine handle everything after
it, and the seller never sees the brief.

An inquiry is described by traits: `availability` (`fits`, `short_rooms`, `too_soon`), `price`
(`comfortable`, `tight`, `below_floor`), `deposit` (`standard`, `low`), `attrition`, `meeting`, `missing`
(`payer`, `taxes`, `layout`), `payer_country`, `change_rooms`. Presets are named combinations:

| Preset | Path with the rules seller |
|---|---|
| `straight_booking` | quote, accept, deposit paid |
| `dates_short_haggle` | dates too soon, other dates offered and taken, counter below the floor, floor held, organizer sign-off, paid |
| `full_lifecycle` | attrition traded for a 40% deposit (manager approval), organizer sign-off, paid, fewer rooms, partial refund, hotel payout reversed |
| `payer_abroad` | payer in Germany paying from EUR, paid, fewer rooms, refund |
| `missing_details` | the seller asks who pays and about taxes, the buyer answers from its brief, paid |

The same seed and traits give the same inquiry against the same calendar; the sender's domain includes the
seed, so replaying a seed in the same run is refused rather than merged. From the server:
`GET /api/simulate/presets`, `POST /api/simulate/rfp {"preset": ..., "traits": {...}, "seed": ...}`.
`tests/test_generator.py` runs every preset end to end.

### Browser check of the demo script

`scripts/e2e_demo.js` clicks through the demo script in a real browser against a running server with
simulated payments, and checks 38 points: the seller starting on its own, every spec figure, the
buyer's moves made from the buyer's mailbox (none on the dashboard), the AI Agent tab, Seller rules
(a refused change, a saved one and restoring the defaults), Availability, the late notification, 375 px
width on every tab, the rules page and the mailbox, and no JavaScript errors or browser dialogs. It resets
the scenario first. It needs Playwright for Node (`npm i playwright`).

```bash
PAYMENTS=simulated SELLER_MODE=rules GS_VAR=var/e2e PYTHONPATH=src python scripts/group_sales.py &
node scripts/e2e_demo.js                                   # or BASE=<url> TOKEN=<access token>
```

The checks that need real keys, and a hand-off prompt for running them, are in `docs/LIVE_CHECKS.md`.

## Not in this build

Kit 10 (Merchant Onboarding). Payments for extra rooms, other dates, upgrades, custom services, split
billing, forecasting, separate department interfaces. Later installments are shown as obligations
and are not charged automatically.
