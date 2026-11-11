# Recorded demo: shot list (about 3½ minutes; trim to fit)

A shorter cut of the live demo script in [GROUP_SALES.md](../GROUP_SALES.md#demo-script-about-6-minutes).
Buttons are named as they appear on the dashboard.

## Before you record

- Start the server (`PYTHONPATH=src python scripts/group_sales.py`) and open `http://localhost:8010`
  (or the hosted URL with `?k=<token>`). For Airwallex on screen you need sandbox keys and
  `PAYMENTS=airwallex`. Without them the header says **Payments: simulated**: say so in the voice-over.
- Pick the seller when you start the server (`SELLER_MODE=rules` or `claude`): **rules** is instant and
  repeatable; **Claude** is slower and its wording changes from run to run. The figures are the same, because the backend computes them.
- **↻ Reset scenario → Yes, reset** (in the sidebar's property tools). Keep a browser tab ready for the payment page.
- Browser window about 1280 px wide, so the deal, its next-step card and the approvals column fit.

## Shots

| Time | On screen | Say |
|---|---|---|
| 0:00–0:15 | The dashboard after reset: two inquiries, Northstar and Summit | "Harbour House gets group requests by email. A seller agent works them from the first email to a paid booking, and a person approves anything outside its authority." |
| 0:15–0:40 | **Run seller.** Northstar shows three questions; Summit gets a quote, **£4,920, deposit £1,476**. Point at Summit's **Activity** tab and the **Records checked** tab | "Northstar is missing details, so the agent asks rather than guesses, and moves Summit forward while it waits. Every system it checked is listed with a time." |
| 0:40–1:05 | Northstar → **Send answers**. Offer v1 **£9,600**. **Send counteroffer** (20% attrition, 25% deposit). The card asks the hotel manager | "The buyer wants more flexibility. The agent trades instead of conceding: it keeps £220 and asks for a 40% deposit, £3,840. That is outside its own authority, so the manager decides." |
| 1:05–1:25 | **Approve** as hotel manager. **Accept v2** in the mailbox; the seller opens checkout on its own | "The manager approved the trade, the buyer accepted, and the agent moves straight to collecting the deposit." |
| 1:25–1:50 | **Open payment page ↗**, pay with `4035 5010 0000 0008`, return. Status **Confirmed**, Money card shows £3,840 received | "The buyer pays on the hosted page; the agent never sees card data. On return the backend checks the payment with the provider before it turns the hold into a booking." |
| 1:50–2:20 | **Jump to 20 Jan.** Summit lapses. **Send change request**, then **Accept v3**: **£8,280**, £528 owed | "Two rooms drop out. That is within the agreed allowance, so the deposit is recalculated and £528 is owed back." |
| 2:20–2:40 | **Approve** the £528 refund (over the seller's £500 limit). The seller creates it and checks it; ledger updates once | "Refunds above £500 need the manager, for that exact amount. Once a refund exists the agent checks it rather than creating another." |
| 2:40–2:50 | Money card: **£3,312 net received, £4,968 still due** | "The refund was checked once and recorded once." |
| 2:50–3:05 | **Jump past 8 Feb.** Card turns **Installment overdue**; point at the **Payment schedule** card | "8 February passes and the £828 installment hasn't arrived. The agent sends a payment link for exactly £828, and weighs the risk: 40% paid, a busy week, rooms resellable until 24 February." |
| 3:05–3:15 | Mailbox: **Ask for more time** (15 Feb). The seller's reply: **11 Feb**, "your dates are in high demand" | "The buyer asks for a week. On quiet dates the agent would agree. This week is busy, so it gives the three days it can and says why. No manager needed: a person is only asked for what the agent can't decide." |
| 3:15–3:25 | Mailbox: pay the £828. Money card **£4,140 net received**; payout card: hotel paid its share | "Paid on the same link, verified with Airwallex, recorded once, and the hotel's share paid out by funds split." |
| 3:25–3:30 | Back to the deal header | "The model negotiates and decides; prices, authority, inventory and money are enforced in code, with offline tests for each rule." |

## Notes

- Do not claim more than [GROUP_SALES.md](../GROUP_SALES.md#verified-live-airwallex-sandbox-2026-11-02)
  records as verified. If you record on simulated payments, say so.
- If a step does not appear, check the deal's next-step card: a red card means the seller paused, with
  **Retry**.
