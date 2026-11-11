# Post-stay reservation → final folio reconciliation

This extends a confirmed group booking beyond its signing deposit. Before this
change, the application reconciled payment notifications, refunds and hotel payouts;
it did not import a final PMS folio, allocate incidentals or collect the final balance.

## What the numbers mean

A reservation price is a charge, not proof of payment. Reconciliation uses:

```
Final master charges − verified Airwallex receipts − staff-attested external receipts
                     + completed refunds = outstanding master balance
```

For a £5,000 reservation, £5,000 actually paid and a £7,000 final master bill,
checkout requests £2,000. If only the £1,500 deposit was paid, it requests £5,500:
the remaining £3,500 reservation balance plus £2,000 of extras. The £5,000 contract
is never counted as a receipt.

Guest charges and their PMS receipts are separate. A guest's minibar does not become
an organizer's debt automatically. Staff must review who is responsible for each
line. The complete hotel folio cannot be finalized while guest balances or credits
remain unresolved. The buyer only sees the master charges allocated to that payer.

## Demo walkthrough

1. Complete a booking normally, including the simulated deposit. Open the confirmed
   inquiry and choose **Final folio** or **Reconcile final folio**.
2. Link a PMS reservation number, or use the generated demo reference. The binding
   cannot be changed once established. Every import must name that same reservation.
3. With simulated payments, choose **Advance simulated demo to checkout**. This
   explicitly moves the shared scenario clock for all inquiries. Final import is
   blocked before the booked stay ends.
4. Choose **Import simulated PMS folio · £2,000 extras**. This imports the full agreed
   package, £500 of minibar spend allocated to a guest, and £1,500 of additional event
   services allocated to the master. The data is labelled simulated.
5. Review/edit the charges and payer allocations, then save the complete bill. If the
   organizer is responsible for all £2,000 of extras, explicitly move the minibar line
   to **master** before saving. Adding a new incidental defaults to **guest**.
6. Record any reservation balance previously settled in the PMS. For the existing
   Summit demo: reservation £4,920, deposit £1,476, remaining reservation settlement
   £3,444. Use a distinct PMS receipt ID/reference and attest that external receipt
   evidence was checked. An Airwallex receipt can instead be matched by its existing
   verified intent ID; it is not credited a second time.
7. Approve the exact final bill and allocations, then create the final balance link.
   When the £4,920 reservation is fully paid and all extras belong to the master,
   the final bill is £6,920 and this new link requests exactly £2,000.
8. Open the buyer mailbox and **Pay final balance**. The customer consents on the
   existing hosted checkout. Payment is re-read from the provider on return or a
   verified webhook; the amount and references must match before a receipt is posted.
9. Once the master and guest balances are reconciled, the immutable final statement
   is issued. The buyer can print/save the master folio as PDF. Hotel staff also have
   a complete reconciled PMS folio copy, including guest allocations.

If personal charges stay allocated to a guest, record that guest's actual PMS receipt
before finalizing. No new guest card-payment portal or automatic organizer charge is
implied. A credit/overpayment requires finance reconciliation; this version does not
silently issue a refund or create another charge.

## PMS integration boundary

There is **no live PMS connection** in this PR. The demo supplies simulated data,
and hotel staff can enter a complete snapshot. The import API is the adapter boundary
for a future trusted PMS connector. It must map one reservation number and import
full, tax-inclusive GBP charges, stable posting IDs and receipt IDs. It must not send
only the incremental incidentals as though they were the complete bill.

Hotel API (existing dashboard access control):

- `GET /api/deals/<id>/folio`: reconciliation, sources of receipts and blocking issues.
- `POST /api/deals/<id>/folio`: `command` plus `payload`; supports `link`, `import`,
  `simulate`, `receipt`, `approve`, `checkout`, `check`, `cancel_checkout`, `finalize`
  and explicit simulated `demo_checkout`.
- `GET /api/deals/<id>/folio/report`: printable hotel copy, available after issuance.

Example import payload, with amounts in integer GBP pence:

```json
{
  "command": "import",
  "payload": {
    "reservation_ref": "PMS-EXISTING-RESERVATION",
    "source_id": "PMS-FOLIO-100",
    "source_revision": 1,
    "currency": "GBP",
    "lines": [
      {"posting_id":"stay","description":"Agreed accommodation package","category":"accommodation","payer":"master","amount":500000},
      {"posting_id":"extra","description":"Approved additional services","category":"incidentals","payer":"master","amount":200000}
    ],
    "payments": [
      {"payment_id":"bank-balance","amount":350000,"method":"external","reference":"BANK-RECEIPT-001","payer":"master"}
    ],
    "external_payments_attested": true
  }
}
```

This example assumes the application's verified deposit receipt is £1,500. The external
receipt supplies the other £3,500 previously paid. It is explicitly staff-attested,
not represented as a bank API or Airwallex verification. `airwallex` receipts must
match an existing verified payment on this booking and are never credited twice.

Buyer routes `/folio/<ref>/pay`, `/return` and `/document` require the booking's own
buyer token. They cannot approve bills or create payment obligations. Unapproved
PMS bills and other guests' allocations are not exposed to the customer.

## Money and approval behavior

- Folio state persists with the existing inquiry storage (file/Postgres). PMS snapshot
  revisions and receipt journals are retained; repeated identical imports are harmless.
  Changing a snapshot requires a newer revision and invalidates its approval.
- An unresolved checkout blocks bill/receipt changes. Staff can check it or cancel an
  unpaid intent before revising the bill. Uncertain creation/cancellation is recovered
  by existing references; a repeated click never blindly creates another intent.
- Collection is manager-initiated, for the approved outstanding master balance only,
  and uses customer-consented hosted checkout. There is no off-session card charge.
- Provider status, amount, GBP currency, merchant order and operation/run metadata are
  validated before recording a receipt. Webhooks are triggers to re-read the provider,
  not trusted proof of payment themselves. Receipt keys make repeats idempotent.
- Online final payments use the existing connected-account funds-split/commission
  path. Payouts are tracked per payment intent; deposit refunds still reverse the
  original deposit payout. External PMS settlements are already outside the platform
  and are not split again.
- The master bill cannot use this workflow to silently discount the accepted contract.
  A PMS master total below that contract blocks collection/finalization pending a
  separately approved adjustment; existing rate/authority rules are not bypassed.
- Existing reservation amendment automation is blocked after a final PMS snapshot is
  started. Finalized documents are immutable; subsequent changes need a separate
  approved financial adjustment. Credit handling remains a finance task.
- PMS descriptions are escaped in the dashboard, buyer view, checkout and printable
  output. Public copies omit individual guest allocations; the hotel copy includes them.

## Validation commands

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_folio.py tests/test_group_sales.py -q
FOLIO_TEST_URL=http://127.0.0.1:8034 node ui/test/folio.cjs
```

Use an isolated simulated server for the browser test; it resets its scenario. The
browser follows deposit payment, reservation binding, PMS import, explicit payer
allocation, prior-payment reconciliation, incremental checkout and final PDF view.
Live PMS integration, automatic guest collection, non-GBP folios, VAT invoice
compliance and automated post-stay refunds are outside this first version. A person
must verify the new Airwallex collection/payout path on staging before promotion.
