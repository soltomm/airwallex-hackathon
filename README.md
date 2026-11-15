# Groupy: an AI group-sales agent for hotels

Groupy takes a hotel group booking from the first email to a paid Airwallex deposit, on its own. A
seller agent (Claude) reads the request, checks the hotel's systems, quotes, negotiates and collects the
deposit. Later it handles changes and refunds. A person only steps in when a decision is outside the
agent's authority.

Built for the Airwallex Agentic Banking Hackathon. MIT licensed.

## The problem

Group bookings (a team offsite, a wedding party, a conference block) bring hotels a lot of revenue
in one deal. They are also the most manual part of hotel sales:

- **Requests come in as free-text emails (RFPs)**, often missing key details. A salesperson has to read
  each one, check availability, rates and meeting space, and chase the buyer for answers.
- **Speed decides the deal.** Buyers send the same RFP to several hotels. The ones that answer first
  with a clear quote usually win, but small and mid-size hotels rarely have a dedicated group desk.
- **Every quote is a negotiation** over rate, deposit, attrition (how many rooms the group may release)
  and payment terms. Each concession has to stay inside the hotel's margin and authority rules.
- **Collecting money is slow and error-prone.** Deposits by invoice and bank transfer, instalments
  chased by hand, and refunds worked out in spreadsheets when the group shrinks.

## The opportunity

Groups are high-value, repeatable business that most independent and mid-size hotels handle with
email and spreadsheets. An agent that answers every RFP in minutes, negotiates within the hotel's own
rules and gets paid on the spot lets those hotels win more groups without hiring a group sales team.

Groupy runs as a **platform on Airwallex Payments for Platforms**: each hotel is a connected account,
Groupy collects the buyer's payment for the hotel and keeps a commission (10% in the demo) on the money
it brings in. That gives a business model that grows with booking value, and hotels get paid without
building any payment setup of their own.

## What it does

| Step | What the agent does | Airwallex |
|---|---|---|
| **Intake** | Reads the RFP email, pulls out dates, rooms, meeting space and budget, and asks only for what is missing | |
| **Quote** | Checks live availability, rates, costs and margin, then sends a priced offer and holds the rooms for 24 hours | |
| **Negotiate** | Trades instead of conceding (e.g. more attrition flexibility in exchange for a bigger deposit). Anything past its authority goes to the hotel manager for one click | |
| **Book** | Once the buyer accepts, opens checkout. The hold becomes a booking only after the payment is verified with Airwallex | Payment Intent, hosted checkout, webhooks |
| **Pay the hotel** | Pays the hotel its share of each payment and keeps the platform fee | Funds split to the hotel's connected account |
| **Change and refund** | Prices a room reduction from the contract, refunds the excess deposit (manager approval above £500) and takes back the hotel's share | Refund, funds split reversal |
| **Instalments** | Requests the balance on the contract's due dates, through a hosted payment page | Payment Intent |

**Built so the agent can't go wrong with money.** The model negotiates and writes the emails. Prices,
authority limits, inventory, approvals and every money movement are enforced in code
([`engine.py`](src/agentbank/groupsales/engine.py)), so nothing the agent writes and nothing inside an
RFP can change them. In practice:

- The agent acts only through tools. The engine refuses anything outside the hotel's rules, and the
  refusal goes back to the agent as a tool result.
- Offers are immutable, hashed versions. An approval or acceptance names one exact version, so
  changed terms need a new yes.
- Payments are at-most-once: each operation is saved before calling Airwallex, and a timeout is looked
  up rather than resubmitted. Ledger entries are keyed, so a repeated webhook changes nothing.
- Each side keeps its secrets. The agent never sees the buyer's private goals. The buyer never sees
  the hotel's floors, costs or margins. No agent holds the Airwallex key or sees card data.

## How to judge it

### 1. Watch the demo

<!-- Add the video link here once it is recorded, e.g. "[3-minute walkthrough](https://...)" -->
*Video link to be added.*

### 2. Try it yourself (about 5 minutes)

Open the live demo: **https://harbour-house-group-sales.onrender.com** (the access link with its token
is in our submission). It sleeps when idle, so the first load can take a minute.
Or [run it locally](#run-it-locally) with no keys.

You play the **hotel** on the dashboard and the **buyer** in their mailbox. The agent works on its
own between your moves. Each deal has a coloured next-step card: amber means your decision as hotel
manager, purple means waiting for the buyer.

1. **↻ Reset scenario → Yes, reset** (sidebar). The agent starts at once. It asks Northstar Analytics
   three questions (its RFP is incomplete) and quotes Summit Robotics: **£4,920, deposit £1,476**.
2. Open Northstar's row, then **Maya's mailbox ↗**. Type *"Classroom seating for the meeting room. The
   legal payer is Northstar Analytics Demo Ltd. Our total budget is inclusive of taxes."* and **Send**. The agent quotes
   **£9,600**.
3. Push back: *"Thank you, the £220 room rate works for us. Attendance is still uncertain, so we need a
   20% attrition allowance. We propose a 25% deposit."* The agent
   keeps the rate and asks for a **40% deposit (£3,840)** in return. That is outside its authority, so
   on the dashboard **Approve** it as the hotel manager.
4. In the mailbox, **Accept offer v2**, then **Open payment page ↗** and pay with the test card
   `4035 5010 0000 0008`. The booking is confirmed only after the payment is verified. The **Hotel
   payout** card shows **£3,456** split to the hotel and **£384** kept as the Groupy fee.
5. **Demo clock → Pick a day: 20 Jan 2027 → Jump** (it warns that Summit's quote
   will lapse). In the mailbox: *"Please reduce our block by two
   twin rooms for all three nights."*, then **Accept change v3**: the new total is **£8,280** and
   **£528** is owed back.
6. **Approve** the £528 refund on the dashboard (it is over the agent's £500 limit). The agent issues
   it and the hotel's share is reversed. Final position: **£3,312 received, £4,968 still due**.

Things to look at along the way: the **AI Agent** tab (every tool call the agent made, including
refused ones), **Records checked** (which hotel systems it consulted), and **＋ New inquiry** to send
your own RFP. Try dates the hotel can't sell, or slip an instruction into the RFP text: the agent
offers dates it can sell, and the instruction changes nothing.

### 3. Check the engineering

```bash
PYTHONPATH=src python -m pytest tests -q
```

No keys or network needed. The suite covers the demo figures above, the full deal story, privacy in
both directions, every authority limit (including instructions hidden in an RFP), inventory and hold
expiry, payment and refund recovery after timeouts, webhooks, randomized deals
(`tests/fuzz_group_sales.py`) and the Claude seller's tool calls. `scripts/e2e_demo.js` clicks through
the demo in a real browser.

## Verified live on the Airwallex sandbox

On 2026-11-09 the full story ran on our staging deploy against the Airwallex sandbox, with the hotel
onboarded from the dashboard as a connected account:

| Step | Amount | Status |
|---|---|---|
| Deposit Payment Intent, collected for the hotel's connected account | £3,840.00 | SUCCEEDED |
| Funds split to the hotel | £3,456.00 | CREATED |
| Refund to the buyer | £528.00 | ACCEPTED |
| Funds split reversal (hotel's share of the refund) | £475.20 | CREATED |

Airwallex accepted the split and the reversal. Both were still `CREATED` when we checked, because the
sandbox card payment had not settled yet, so we have not seen the hotel's balance move. An earlier run
(2026-11-02) verified the deposit and partial refund with the rules seller. The Claude seller has run
the same story on simulated payments.

## Run it locally

Offline, with no keys (a simulated Airwallex twin and the deterministic rules seller, which uses the
same tools as Claude):

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[agent]' pytest
PAYMENTS=simulated SELLER_MODE=rules PYTHONPATH=src .venv/bin/python scripts/group_sales.py
```

Open http://localhost:8010 and follow the steps above.

To use Claude and the Airwallex sandbox, copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`,
`AWX_CLIENT_ID` and `AWX_API_KEY` (sandbox, Payment Acceptance read/write), then start the server
without `PAYMENTS` and `SELLER_MODE`. Our sandbox key is IP-restricted to our Render deploy, so live
Airwallex calls work from there, not from a laptop.

## How it is built

One Python process ([`scripts/group_sales.py`](scripts/group_sales.py)) serves the hotel dashboard,
the buyer's mailbox and the payment page, runs the agent in the background and receives Airwallex
webhooks. Every state change goes through the engine.

```mermaid
flowchart LR
  H["Hotel dashboard"] --> S["Server and agent runner"]
  B["Buyer mailbox"] --> S
  P["Payment page"] --> S
  S -- wakes on each event --> A["Seller agent<br/>Claude or rules"]
  A -- tools only --> E["Engine<br/>rules, versions, inventory,<br/>approvals, money ledger"]
  S -- buyer and manager actions --> E
  E --> PR["provider.py"]
  PR <--> AWX[("Airwallex<br/>sandbox or simulated")]
  AWX -- signed webhooks --> S
  P -. hosted checkout .-> AWX
```

| Part | Where |
|---|---|
| Engine: every rule and every state change | [`src/agentbank/groupsales/engine.py`](src/agentbank/groupsales/engine.py) |
| Pricing, margin and hotel authority, in integer pence | `calc.py`, `policy.py` |
| Claude seller and rules seller, same tools | `seller.py`, `rules.py` |
| Airwallex: Payment Intents, refunds, funds splits, connected accounts | `provider.py` |
| Hotel data, the two demo RFPs and the buyers' private briefs | [`data/groupsales/scenario.json`](data/groupsales/scenario.json) |
| Pages | `web/group_sales.html`, `web/group_buyer.html`, `web/group_pay.html` |

The demo hotel is Harbour House, London, with simulated property, revenue and CRM systems.

Optional features, off by default: third-party vendors (`GS_VENDORS=1`,
[docs](docs/VENDOR_WORKFLOWS.md)), post-stay folio reconciliation (`GS_FOLIO=1`,
[docs](docs/POST_STAY_RECONCILIATION.md)), payer origin and FX (`GS_FX=1`, [docs](docs/FX_POLICY.md)),
and revenue-manager pricing review ([docs](docs/REVENUE_REVIEW.md)). Deployment:
[docs/DEPLOY.md](docs/DEPLOY.md). Contributing: [AGENTS.md](AGENTS.md).
