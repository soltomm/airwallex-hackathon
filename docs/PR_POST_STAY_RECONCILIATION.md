## Problem and resulting behavior

The existing flow stops at a paid booking, amendments and deposit refunds. Its payment
“reconciliation” rechecks provider notifications; it does not reconcile the agreed
reservation with a post-stay PMS folio containing minibar, incidentals or other charges.
There is no final-bill balance collection or settled final folio.

This PR adds a post-stay reconciliation workflow inside each confirmed inquiry. Staff
link the PMS reservation, import/review the complete final charges, assign each charge
to the master payer or a guest, reconcile payments already received, approve the bill,
and create a customer-consented checkout for the outstanding master balance. A settled,
immutable folio is issued once master and guest balances are reconciled.

## Accounting example

- Accepted reservation: **£5,000**.
- Payments actually received: **£5,000**, which may comprise a verified £1,500 Airwallex
  deposit and a separately recorded £3,500 PMS settlement.
- Approved final master bill: **£7,000**.
- New Airwallex checkout: **£2,000**, not £7,000 and not another deposit.

If only the £1,500 deposit has been paid, the amount due is **£5,500**. A reservation
price is never treated as a receipt. Personal guest charges are excluded from the
organizer's collection unless management explicitly allocates them to the master.

## What changed

- Added a **Final folio** tab and a post-stay Next move with reservation price, final
  bill, variance, received payments by source, outstanding balance and blocking issues.
- Added an immutable PMS reservation binding, complete tax-inclusive GBP snapshot
  imports, stable posting/receipt IDs, revision history and payer allocation controls.
  Staff can import a simulated £2,000-extras example or edit the complete bill.
- Added receipt reconciliation. Existing verified Airwallex receipts are included
  automatically; matching a PMS copy does not credit them twice. Other PMS receipts
  require explicit hotel-manager attestation and are labelled as such. Individual
  guest receipts reconcile the guest's own balance, not the master account.
- Added exact-snapshot approval and a separate final-balance payment operation, using
  the existing provider adapter and hosted checkout. Final payment status is verified
  from the provider before a receipt is recorded. Verified GBP receipts also enter
  the existing FX receipt journal, without a conversion being needed.
- Extended funds-split handling per payment intent so the online final receipt follows
  the existing hotel/platform commission model. Existing deposit refunds still reverse
  the original deposit payout. External receipts are not split again.
- Added final statements: a buyer-visible master copy and a hotel copy including guest
  allocations, with print/save-PDF controls. The buyer mailbox shows the approved final
  bill, outstanding payment link and issued master statement.

## Authority, consistency and privacy

Hotel-manager authority is required for import, external-receipt attestation, approval
and checkout creation. Collection is blocked before the booked stay ends, during an
unresolved reservation refund, without current approval, or for credit balances. This
workflow cannot silently discount the accepted contract: a lower PMS master total
requires separate adjustment reconciliation before collection/finalization.

Identical imports are idempotent. Changed data requires a newer PMS revision and
invalidates approval. An unresolved checkout blocks bill changes; staff can verify
or cancel an unpaid intent before revising the bill. Operations are persisted before
provider calls, uncertain outcomes are recovered by existing references, and duplicate
clicks/webhooks cannot create a second receipt or blindly resubmit a payment.

Provider success, amount, GBP currency, merchant order and operation/run metadata must
match. Finalized statements cannot be edited. Booking amendments cannot rewrite the
reservation once final reconciliation has begun. The buyer does not see other guests'
charges, internal receipt journals or supplier costs. PMS descriptions are escaped in
browser pages and printable documents.

## Reviewer walkthrough

1. Run an isolated instance with `PAYMENTS=simulated SELLER_MODE=rules` and complete the
   existing Summit booking/deposit. Open the confirmed inquiry's **Final folio** tab.
2. Link a reservation reference, then choose **Advance simulated demo to checkout**.
   This explicitly advances the shared demo clock for all inquiries.
3. Import the simulated £2,000-extras folio. Review the minibar/incidentals and payer
   allocations. To demonstrate all extras billed to the organizer, move minibar to
   **master** and save the complete charges.
4. Record the reservation balance already paid through the PMS. In Summit, this is
   £3,444 after its £1,476 deposit against the £4,920 reservation. Use a distinct receipt
   reference and attest the external evidence.
5. Approve the final £6,920 bill and create the payment link. The remaining master
   balance must be exactly £2,000.
6. Open the buyer mailbox, follow **Pay final balance**, and complete simulated hosted
   checkout. Verify the receipt appears once, the balance becomes zero, and the final
   master folio and complete hotel copy open with the correct totals.
7. Also try leaving minibar allocated to a guest, replaying an import, changing a bill
   while checkout is open, cancelling an unpaid checkout, or importing a credit. These
   must not silently charge the organizer or mark an unreconciled bill settled.

## Validation

- **19 new backend tests** cover the £5,000/£7,000 example, deposit-only balances,
  guest allocation/settlement, receipt deduplication, revision/approval invalidation,
  timeout recovery, duplicate webhooks, incorrect provider facts, card decline,
  unpaid-checkout cancellation, credits, contract-price protection, checkout timing,
  buyer/hotel access controls, HTML escaping and statement immutability.
- The dedicated browser test passes the complete reservation/deposit → PMS binding →
  charge allocation → prior-payment reconciliation → exactly £2,000 collection → final
  folio/PDF-view path, plus privacy, mobile width and payment-return replay.
- Existing group-sales tests pass, including the original proposal, deposit, amendment,
  refund and payout figures. JavaScript syntax and `git diff --check` pass.
- Full-suite run: **324 passed, 7 skipped, 4 subtests passed; one existing failure** in
  `tests/test_understand.py::test_schema_is_in_the_form_the_api_accepts` with the installed
  Anthropic SDK. This failure was previously reproduced on unchanged main and is not
  changed by this PR. Postgres tests skip without `TEST_DATABASE_URL`.
- Existing browser walkthrough: **45/46 checks passed**. It completes booking, deposit,
  amendment/refund, rules and availability; the final mobile inquiry-row click times
  out while the row is re-rendering, as observed before this change. No JS errors/dialogs.

## Integration limits and staging requirement

This is a working **demo plus PMS adapter boundary**, not a live PMS connector. No PMS
credentials/provider were supplied and no external PMS fetch or write-back is claimed.
The hotel import API accepts reservation-bound snapshots for a future adapter. External
PMS receipts are staff-attested, not bank-verified. Amounts are GBP and tax-inclusive;
VAT invoice compliance, off-session charging, a new guest-payment portal, non-GBP final
folios and automatic post-stay refunds are outside this first version. Credit/adjustment
exceptions remain finance tasks. Data persists in normal inquiry storage; scenario reset
uses the existing demo archive/retention policy.

No Render configuration, deployment or live Airwallex calls were made. A person must
verify the new final-balance checkout, callback and connected-account funds split in
the Airwallex sandbox on staging before promotion.

Implementation/adapter contract and commands are included in
`docs/POST_STAY_RECONCILIATION.md`. The screenshot is included in this branch as
`docs/screenshots/post-stay-folio.png`; this description contains the essential context
without requiring either file or an external link.
