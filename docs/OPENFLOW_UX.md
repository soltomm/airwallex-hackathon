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

**＋ New inquiry** sends a real inquiry to Harbour House, so the seller agent works it with the real rules: it
checks rooms and rates, asks for anything missing, quotes, asks the hotel manager only when a seller rule would
be exceeded, and takes the deposit through Airwallex like any other deal. The drawer offers:

- **Generated inquiries** (`POST /api/simulate/rfp`, see `src/agentbank/groupsales/generator.py`): one card
  per preset, or traits chosen by hand (dates, price pressure, the buyer's deposit limit, attrition, room
  type, payer country, a later reduction, a meeting room, details left out of the RFP). Each inquiry comes
  with a private brief for the buyer agent; the seller never sees it. An optional seed replays the same
  inquiry against the same calendar, for example after a reset.
- **Buyer replies**: automatically (the buyer agent answers for every buyer with a brief) or by you in the
  buyer's mailbox. This is the server's buyer mode. Approvals and
  paying the deposit always stay with a person.
- **Write your own**: a real, unscripted RFP (`POST /api/inbox`, the same intake as an email). It has no
  brief, so nobody answers for the buyer automatically.

Generated rows show their preset and seed under the company name. The earlier browser-only simulator
(its fixtures, fake approvals and deposits) has been removed: everything on the workbench comes from the
engine.

## Summary definitions

- Open pipeline: latest proposal totals of active inquiries. Unquoted inquiries
  contribute zero value.
- Group room nights: active and confirmed blocks, excluding declined/expired live
  inquiries. Each occupied room-night counts once.
- New inquiries: inquiries in intake/needs-info, or handed to staff. The adjacent
  review badge counts items needing hotel
  action; an inquiry waiting for buyer details can be new without needing action.
- Deposits collected: net receipts (after refunds). The Payments view shows whether
  they came through Harbour's simulated provider or the Airwallex sandbox.

## Validation

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[agent]' pytest
PYTHONPATH=src .venv/bin/python -m pytest tests -q
node --check web/openflow.js
node --check web/fx.js
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

The browser test exercises connected detail/rules, the New inquiry drawer (presets loaded from the
server, a seeded preset, a replayed seed being refused, traits chosen by hand, the buyer-mode switch and
writing your own), search through polling, filters, payment-source labels, drawer keyboard focus and mobile
layout. It checks that the workbench only writes through `/api/simulate/rfp`, `/api/inbox` and
`/api/settings`. Screenshots are saved below; the before image is rendered from `origin/main` on the same
isolated backend.

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

![New inquiry drawer](screenshots/openflow/simulator.png)

Mobile: [overview](screenshots/openflow/mobile.png) ·
[new inquiry](screenshots/openflow/mobile-simulator.png).
