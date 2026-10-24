# Demo script (3 minutes)

The guide asks every demo to show the user's goal, the agent's plan, new information, the revised
decision and the resulting payment. This one does it twice: once for the traveller (Kit 10, terms
change before booking) and once for the platform (Kit 8, a hotel fails to deliver).

Two windows side by side: **claude.ai** with the hotels connector, and the **revenue desk**
(`/dashboard/<admin token>`). Card checkout opens in a third tab.

## Before you record

1. Redeploy or restart the server so the data is fresh, then open the dashboard.
2. Run two or three quick negotiations that end without a booking (ask for EUR 350 for the stay below
   and walk away), so the revenue desk has lost demand and suggestions to show.
3. Book one non-refundable stay at **Opéra Lumière** on 14–15 Nov and pay it (test card
   `4035 5010 0000 0008`); it is the stay the hotel will fail to deliver in step 5.
4. Have the GBP test card checkout ready to type quickly.

## Script

**0:00 The pitch (15 s).** "Hotels can't hand-sell every room to every AI agent. We give each hotel its
own seller agent that negotiates inside rules the hotel controls, and Airwallex moves the money:
deposit holds in the guest's currency, payouts to the hotel, and a risk reserve. Every price and money
decision is in code; the models negotiate and explain."

**0:15 Goal and plan (40 s).** In claude.ai:

> Find me a hotel in Paris for 2 people, 13–15 Nov 2026, with breakfast, late checkout and free
> cancellation, around EUR 500. Negotiate for me, then book by card with the deposit in GBP.

The live panel opens. Point at the hotel agents working (checking inventory, testing prices against the
hotel's rules, never showing floors) and at the committed offers with their terms. Claude compares them
and asks you to approve one.

**0:55 New information: rooms sell elsewhere (25 s).** Before saying yes, on the dashboard's
*Sandbox simulations*, sell 2 rooms of that hotel's room type on 14 Nov ("another channel"). Now
approve in claude.ai. Booking re-checks the policy and returns **terms_changed**: the night became
busy, so the hotel can only confirm a higher total. Claude shows exactly what changed and asks again;
nothing has been charged. Approve the new terms.

**1:20 Resulting payment (25 s).** Open the checkout link: the deposit is shown in GBP with the locked
rate and the EUR price. Pay with the test card. The panel flips to *deposit held* (by webhook, or within
seconds). In the Airwallex web app the PaymentIntent is `REQUIRES_CAPTURE` in GBP.

**1:45 The hotel gets paid (25 s).** On the dashboard, *Bookings* → **Check out** (click twice). The hold
is raised to the full stay and captured; *Payouts* shows the hotel's share split to its connected
account: part released now, part held as a **reserve** (20%: a new hotel). *Settlement* reads
owed = paid + held, **✓ balanced**.

**2:10 New information: the hotel overbooks (30 s).** *Sandbox simulations* → overbook **Opéra Lumière**
on 14 Nov. Its affected guest is refunded in full; the hotel's share is clawed back by funds split
reversal, reserve first. Its reserve rate jumps (20% → 50%, with the reason) and its reserves are
frozen. **Run payout cycle**: the delivered stay's reserve is released; the Opéra's stays held, citing
the incident.

**2:40 The hotel learns (15 s).** *Suggested rule changes*: "Lower the Classic Double floor by EUR 35 a
night on the nights of 13–15 Nov: 3 travellers walked away below it". **Apply**. The seller agents use
it from their next step; *Rule changes applied* tracks conversion before and after.

**2:55 Close (5 s).** "Airwallex Payments, Multi-Currency Pricing, Payments for Platforms and
webhooks; Metal for agent-paid deposits; Claude on both sides. Evals and 97 tests in the repo."

## If something goes wrong live

- A Render free instance sleeps after 15 idle minutes: open the dashboard a minute before recording.
- No panel in the chat: the same rounds come back as text; carry on.
- Checkout says the payment is not open yet: refresh the page (it retries with Airwallex).
