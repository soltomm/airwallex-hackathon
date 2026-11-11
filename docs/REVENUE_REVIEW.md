# Revenue pricing review

Pricing review adds an internal decision to the existing inquiry drawer and offer
engine. It does not replace the dashboard, change rate bands, or waive margin rules.

When the rules seller receives a buyer counteroffer in the manager's discretionary
rate band (at/above the hard minimum, below the seller's floor), it pauses and routes
an inquiry-owned task to the Revenue team. A package below the hard margin floor
also routes to revenue for a higher counteroffer or an alternative scope. Ordinary
bargaining at the seller's floor continues for requests below the absolute minimum;
the agent cannot promise that such a request can be approved. The Claude seller
has a request-only tool and the same engine enforcement. Existing generic pricing
handoffs also offer **Request revenue pricing review**.

The pipeline and Next move show **Awaiting revenue review**. The inquiry card shows
why review is needed, the owner/time, current dates/rooms, buyer-requested rate,
latest quoted rate and total, stated budget, agent limit and hard limits. No private
buyer brief is consulted. Pending review withdraws prior offers/holds and invalidates
pending approvals; the buyer gets a short acknowledgement. The seller cannot send
another proposal until review is resolved.

**Review as revenue manager** opens the review in the existing drawer. This is a
demo role using existing hotel-manager authority and the dashboard's access gate,
not a new authentication system. Staff can add internal notes, decline with a reason,
or enter a counteroffer/minimum acceptable rate and preview the complete package.
Preview includes billing lines, deposit, attrition, both contribution margins and
any hard/inventory/vendor-readiness problems. It prices the current inquiry scope;
changes to dates or quantities still go through the existing inquiry requirements.

**Approve and requote this package** binds the manager decision to the current
preview and request. A changed buyer message, requirements, vendor lines or proposal
makes the request stale. Policy/availability changes are checked again at approval.
Invalid prices cannot be approved. The existing offer approval mechanism records
the decision where necessary; the seller then sends the revised version, using the
normal inventory hold, acceptance and checkout path. Approval is for one package,
not permanent authority to discount future offers. Duplicate requests/decisions are
idempotent or refused; a rejection stays with sales, and closing/declining withdraws
a pending review. Accepted/booked inquiries cannot enter this workflow.

**Copy internal workflow link** copies a deep link with the request identifier.
Recipients need hotel-dashboard access. No access token is copied and no email or
Slack message is sent. Notes, floors and margins stay internal; buyer messages contain
only the proposed commercial terms. Reviews persist with normal inquiry storage.

Validation:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_pricing_review.py tests/test_generator.py -q
REVENUE_TEST_URL=http://127.0.0.1:8028 node ui/test/revenue.cjs
```

The browser test resets an isolated simulated server and follows a buyer counter
through review, internal discussion, hard-limit refusal, approval, revised quote,
buyer acceptance and simulated hosted checkout. It also checks the direct link,
buyer privacy and mobile width. No Render deployment or Airwallex call is needed.
