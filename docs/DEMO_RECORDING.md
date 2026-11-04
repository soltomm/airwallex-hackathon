# Recorded demo: shot list (about 3 minutes)

A shorter cut of the live demo script in [GROUP_SALES.md](../GROUP_SALES.md#demo-script-about-6-minutes).
Buttons are named as they appear on the dashboard.

## Before you record

- Start the server (`PYTHONPATH=src python scripts/group_sales.py`) and open `http://localhost:8010`
  (or the hosted URL with `?k=<token>`). For Airwallex on screen you need sandbox keys and
  `PAYMENTS=airwallex`. Without them the header says **Payments: simulated**: say so in the voice-over.
- Pick the seller under **Demo controls**: **Rules** is instant and repeatable; **Claude** is slower
  and its wording changes from run to run. The figures are the same, because the backend computes them.
- **Demo controls → Reset scenario → Yes, reset.** Keep a browser tab ready for the payment page.
- Browser window about 1280 px wide, so the deal, its next-step card and the approvals column fit.

## Shots

| Time | On screen | Say |
|---|---|---|
| 0:00–0:15 | The dashboard after reset: two inquiries, Northstar and Summit, and the room strip | "Harbour House gets group requests by email. A seller agent works them from the first email to a paid booking, and a person approves anything outside its authority." |
| 0:15–0:40 | **Run seller.** Northstar shows three questions; Summit gets a quote, **£4,920, deposit £1,476**. Point at the activity note and the **Records checked** tab | "Northstar is missing details, so the agent asks rather than guesses, and moves Summit forward while it waits. Every system it checked is listed with a time." |
| 0:40–1:05 | Northstar → **Send answers**. Offer v1 **£9,600**. **Send counteroffer** (20% attrition, 25% deposit). The card asks the hotel manager | "The buyer wants more flexibility. The agent trades instead of conceding: it keeps £220 and asks for a 40% deposit, £3,840. That is outside its own authority, so the manager decides." |
| 1:05–1:25 | **Approve** as hotel manager. **Accept v2**; the card turns to the buyer's organizer. **Approve** | "The deposit is over Northstar's own limit, so its organizer has to sign off. The hotel's agent only learns that sign-off is pending, never the buyer's limits." |
| 1:25–1:50 | **Open payment page ↗**, pay with `4035 5010 0000 0008`, return. Status **Confirmed**, Money card shows £3,840 received | "The buyer pays on the hosted page; the agent never sees card data. On return the backend checks the payment with the provider before it turns the hold into a booking." |
| 1:50–2:20 | **Jump to 20 Jan.** Summit lapses. **Send change request**, then **Accept v3**: **£8,280**, £528 owed | "Two rooms drop out. That is within the agreed allowance, so the deposit is recalculated and £528 is owed back." |
| 2:20–2:40 | **Approve** the £528 refund (over the seller's £500 limit). The seller creates it and checks it; ledger updates once | "Refunds above £500 need the manager, for that exact amount. Once a refund exists the agent checks it rather than creating another." |
| 2:40–2:55 | **Demo controls → Deliver held notifications**: the late notice is ignored as a duplicate. Final Money card: **£3,312 net received, £4,968 still due** | "A late or repeated notification changes nothing. Final position: £3,312 received net, £4,968 still due." |
| 2:55–3:00 | Back to the deal header | "The model negotiates; prices, authority, inventory and money are enforced in code, with offline tests for each rule." |

## Notes

- Do not claim more than [GROUP_SALES.md](../GROUP_SALES.md#verified-live-airwallex-sandbox-2026-11-02)
  records as verified. If you record on simulated payments, say so.
- If a step does not appear, the seller may be off: **Run seller** on the card, or switch on
  **Seller acts automatically** under Demo controls.
