# OpenFlow group sales workbench

The group-sales server now presents an OpenFlow-style overview and payments
view. It uses the existing Python HTTP server and plain HTML/CSS/JS;
there is no new production dependency or frontend build step.

## Connected Harbour workflows

The existing `/api/state` supplies live inquiries, finance, inventory, approvals,
and activity. Connected rows open Harbour's existing inquiry controls in a right
side drawer, including conversation, versions, manager approvals, payment checks,
refund recovery and operations handoff. Buyer mailboxes still open separately.
Availability and seller rules are under the property tools. On mobile, expand **Property tools** in the sidebar.
The dashboard cookie gate and backend authority, inventory, immutable offer
versions, payment verification and webhook contracts are unchanged. The three new
static assets use explicit authenticated routes, not a directory-serving route.

## New inquiries

**＋ New inquiry** sends a real inquiry to Harbour House (`POST /api/inbox`, the same intake as an
email), so the seller agent works it with the real rules: it checks rooms and rates, asks for anything
missing, quotes, asks the hotel manager only when a seller rule would be exceeded, and takes the deposit
through Airwallex like any other deal. The drawer offers two samples (a corporate offsite by email, a
wedding block from a phone transcript), each with a choice of asking for rooms that are free or for more
rooms than are free (the agent then offers what it has), and a form to write your own: from, subject and
the inquiry text. Harbour House sells twin and king rooms, breakfast and the Thames Room for stays
10–13 March 2027; anything else is handed to hotel staff.

Inquiries created by the earlier browser-only simulator (stored under `openflow-harbour-demo-v1` in
localStorage) still display, labelled as simulated, and **Reset simulator** removes them. No new ones
are created.

## Summary definitions

- Open pipeline: latest proposal totals of active connected inquiries plus active
  simulator drafts/proposals. Unquoted inquiries contribute zero value.
- Group room nights: active and confirmed blocks, excluding declined/expired live
  inquiries. Each occupied room-night counts once.
- New inquiries: connected inquiries in intake/needs-info and simulator drafts or
  alternative-date review. The adjacent review badge counts items needing hotel
  action; an inquiry waiting for buyer details can be new without needing action.
- Deposits collected: connected net receipts plus simulated deposits. The Payments
  view identifies each source and distinguishes browser fixtures from Harbour's
  simulated provider and Airwallex sandbox.

## Validation

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[agent]' pytest
PYTHONPATH=src .venv/bin/python -m pytest tests -q
node --test tests/test_openflow_demo.cjs
node --check web/openflow.js
node --check web/openflow-demo.js
```

The GitHub **Group sales** workflow runs these offline checks on pull requests
and pushes to main. Postgres tests require `TEST_DATABASE_URL`; otherwise they
are explicitly skipped. The unrelated legacy MCP UI bundle is not changed.

For browser checks, install Playwright in a temporary directory rather than
changing the legacy UI dependencies, and install its Chromium if needed:

```bash
npm install --prefix /tmp/openflow-browser playwright
/tmp/openflow-browser/node_modules/.bin/playwright install chromium
PAYMENTS=simulated SELLER_MODE=rules BUYER_MODE=presenter \
  GS_PORT=8011 GS_VAR=/tmp/openflow-harbour-test \
  .venv/bin/python scripts/group_sales.py
# In another terminal, from the same repository root:
NODE_PATH=/tmp/openflow-browser/node_modules node ui/test/openflow.cjs
```

Use an isolated server with no `DATABASE_URL` and no access token for this test;
never point it at a deployed dashboard. The default target is port 8011;
`OPENFLOW_TEST_URL` overrides it. `CHROMIUM_PATH` can select a system browser;
otherwise the test uses `/usr/bin/chromium` if present, then Playwright's browser.

The browser test exercises connected detail/rules, both deterministic simulator
paths, changed-terms reapproval, decline reasons, customer acceptance, deposits,
handoff, filters, search through polling, payment-source labels, local persistence,
manager links, drawer keyboard focus and mobile layout. It also verifies that
simulator actions make no API writes. Screenshots are saved below; the before
image is rendered from `origin/main` on the same isolated backend.

## Screenshots and reference limits

The reference URL was blocked by the development environment's network policy,
and no reference images were available in this chat. This implementation follows
the supplied brief's colors, dimensions, copy and interaction requirements; it has
not been visually compared against the hosted reference. It deliberately keeps
Harbour's richer connected inquiry controls rather than replacing them with demo
controls. Outbound Opportunities and the hosting site's Edit site control are
omitted.

| Before | OpenFlow overview |
| --- | --- |
| ![Original Harbour dashboard](screenshots/openflow/before.png) | ![OpenFlow overview](screenshots/openflow/overview.png) |

![Inbound simulator](screenshots/openflow/simulator.png)
![Unavailable dates and alternative calendar](screenshots/openflow/unavailable.png)
![Editable proposal](screenshots/openflow/proposal.png)

Mobile: [overview](screenshots/openflow/mobile.png) ·
[simulator](screenshots/openflow/mobile-simulator.png).
