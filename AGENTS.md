# Working on this repo (for AI agents and people)

Harbour House: a hotel group-sales agent for the Airwallex Agentic Banking Hackathon. A seller agent
(Claude or a deterministic rules seller) takes a group RFP from first email to a paid Airwallex
deposit, an amendment and a partial refund. Read [README.md](README.md) and
[GROUP_SALES.md](GROUP_SALES.md) before changing behaviour.

## Where things are

- `src/agentbank/groupsales/`: the product. `engine.py` enforces every rule (authority, versions,
  inventory, money, privacy); `calc.py` prices; `policy.py` holds authority and mandates;
  `seller.py` / `rules.py` are the sellers; `provider.py` talks to Airwallex (or its simulated twin).
- `scripts/group_sales.py`: the server (dashboard, buyer mailbox, payment page, webhooks, API).
- `web/group_sales.html` (hotel dashboard), `web/group_buyer.html` (buyer mailbox), `web/group_pay.html`.
- `data/groupsales/scenario.json`: the hotel data, the two RFPs and the buyers' private mandates.
- `tests/`: unit and server tests; `tests/fuzz_group_sales.py` randomized scenarios;
  `scripts/e2e_demo.js` browser walk-through of the demo script.
- Other files (`hotel_*`, `offer_desk`, `metal/`, `payee_check`, ...) are the earlier Hotel
  Negotiation Desk project. Don't change them unless the task is about them.

## Setup and checks

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[agent,postgres]' pytest
PYTHONPATH=src .venv/bin/python -m pytest tests -q          # must pass before you open a PR
PAYMENTS=simulated SELLER_MODE=rules PYTHONPATH=src .venv/bin/python scripts/group_sales.py   # http://localhost:8010
node scripts/e2e_demo.js                                    # with the server running; needs playwright
```

- Work offline with `PAYMENTS=simulated SELLER_MODE=rules`. No keys are needed for that.
- The Postgres storage tests skip unless `TEST_DATABASE_URL` is set; CI runs them.
- Live Airwallex calls only work from Render: the sandbox key's IP whitelist covers Render's Oregon
  egress IPs, so `PAYMENTS=airwallex` fails from laptops and cloud agent sandboxes. Don't treat that
  failure as a code bug, and don't try to route around it.

## How changes get in

1. Branch off the latest `main`. Never push to `main`: it is protected (PR, the `tests` check and one
   approval from another collaborator; no direct or force pushes).
2. Keep the PR to one change. Add or update a test for any behaviour change; for a bug fix, the test
   should fail on the old code. Say in the PR what you ran and what you could not verify.
3. Open the PR against `main`. GitHub Actions (`.github/workflows/tests.yml`) runs the full suite.
4. A teammate reviews and merges. **Agents never approve or merge PRs.**
5. After the merge, staging redeploys. A person checks it and promotes to the live demo.

Testing before merge: CI on the PR, plus a local run (`gh pr checkout <n>`, the server and
`node scripts/e2e_demo.js`) for anything visible. Changes that touch payments can only be tested
against live Airwallex by deploying the branch to staging; ask a person to do that.

## Environments

| | URL | Deploys | Data |
|---|---|---|---|
| Staging | https://harbour-house-preview.onrender.com | `main`, after CI passes | `STORAGE_NAMESPACE=groupsales_staging` |
| Live demo | https://harbour-house-group-sales.onrender.com | `main`, manual deploy by a person | `groupsales` |

Details: [docs/DEPLOY.md](docs/DEPLOY.md). Agents don't change Render services, environment
variables or GitHub settings, and don't trigger deploys. Ask a person.

## Rules that must not break

- Money, prices, authority, inventory and approvals are enforced in `engine.py`, not in prompts.
  Never move a rule into a prompt, loosen a limit, or let RFP text change behaviour.
- Privacy: the seller never sees a buyer's mandate (budget, deposit limits, organizer notes); the
  buyer never sees hotel floors, costs, margins, flags or unsent versions.
- Money calls: save the operation before calling the provider, look up on timeout instead of
  resubmitting, one unresolved refund at a time, ledger entries keyed so repeats change nothing.
- Hotel payouts (Payments for Platforms): the platform collects for the hotel's connected account
  (`GS_HOTEL_ACCOUNT`), pays its share by funds split after a verified deposit, and reverses its share
  of each completed refund. Hotel net + platform fee must equal the buyer's net payment. No agent tool
  moves payouts, and the buyer never sees them.
- Amounts are `Decimal` / integer pence, never floats.
- The spec figures in GROUP_SALES.md (£4,920 / £1,476, £9,600, £3,840, £8,280, £528, £3,312, £4,968)
  are tested. If a change moves them, say so explicitly in the PR.
- Don't claim something was verified live unless it was. GROUP_SALES.md "Verified live" records what was.

## Secrets

Never commit keys, tokens or `.env`. Never paste them into code, logs, PRs or comments.
`.env.example` lists variable names only.
