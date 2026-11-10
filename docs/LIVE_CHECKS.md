# Live checks on `main`

PR #1 was tested offline only: the unit tests, 6,000 randomized scenarios and a browser run of the
demo script, all with simulated payments and the rules seller. These checks need real keys. `main`
deploys the live demo on Render. The Airwallex key's IP whitelist covers Render's Oregon egress IPs,
so check 1 may only pass on the Render deploy, not from a laptop.

## Hand-off prompt

Paste this into a Claude Code session that runs on the Mac with the keys (Claude desktop app, or
`claude remote-control` in the repo folder), or into a cloud session whose environment has the keys:

> In soltomm/airwallex-hackathon, check out `main` and pull. Follow
> `docs/LIVE_CHECKS.md` checks 1-4 in order and report each one's result as pass or fail, with the
> evidence: figures, Airwallex ids, the fuzz script's summary lines, and any messages that look wrong.
> Do not push to `main` or `hotel-negotiation`, do not change any Render service, and do not merge
> any PR. If something fails, stop, show me the failing step and wait.

## Setup

```bash
git fetch origin && git checkout main && git pull
python3 -m venv .venv && .venv/bin/pip install -e '.[agent]' pytest
cp -n .env.example .env       # fill in AWX_CLIENT_ID and AWX_API_KEY (sandbox), and ANTHROPIC_API_KEY
set -a; source .env; set +a   # load the keys into this shell
PYTHONPATH=src .venv/bin/python -m pytest tests -q     # expect 163 passed, 7 skipped
```

## 1. Airwallex sandbox, rules seller

```bash
PAYMENTS=airwallex SELLER_MODE=rules GS_VAR=var/check-awx PYTHONPATH=src .venv/bin/python scripts/group_sales.py
```

At http://localhost:8010, follow the demo script in `GROUP_SALES.md`. The seller starts on its own after
the reset; the buyer's moves and the payment are in Northstar's mailbox (the
link on its card). Pay with `4035 5010 0000 0008`, any future expiry and any CVC.

If Airwallex refuses the deposit intent, Northstar's card turns red with the error and a **Retry**
button, and there is exactly one failed deposit attempt in the Payments tab. That is a fail for this
check: note the error, fix the cause (key, IP whitelist), click **Retry** and carry on.

Pass when:
- the deposit Payment Intent is £3,840.00 GBP and SUCCEEDED, and the booking is confirmed on return;
- exactly one £528 refund is created, then checked and found ACCEPTED;
- Deliver held notifications changes nothing;
- the final figures are £3,312 net received and £4,968 still due.

Write down the intent and refund ids. If Airwallex refuses the calls, the key's IP whitelist probably
does not include this machine.

## 2. Claude seller

```bash
PAYMENTS=simulated SELLER_MODE=claude GS_VAR=var/check-claude PYTHONPATH=src .venv/bin/python scripts/group_sales.py
node scripts/e2e_demo.js      # optional: the same browser check, needs `npm i playwright`
```

Run the same story by hand. Claude words things differently from the rules seller, so the browser
script may fail on wording or timing. Judge by the figures and these points.

Pass when:
- the figures match the spec (£4,920 Summit; £9,600 v1; £220 and 40% = £3,840 for v2; £8,280 v3,
  £528 refund);
- after the buyer accepts v2, the seller opens checkout on its own;
- no seller note or email guesses Northstar's budget (£10,000 is in its RFP, so it may appear) or its
  deposit limits (£3,000, 30%);
- the AI Agent tab reads clearly for Claude's runs, with no red refused lines you can't explain.

## 3. Randomized scenarios with the Claude seller (costs API calls)

```bash
PYTHONPATH=src .venv/bin/python tests/fuzz_group_sales.py --cases 3 --steps 30 --claude
PYTHONPATH=src .venv/bin/python tests/fuzz_group_sales.py --cases 10 --steps 40 --claude   # if the first is clean
```

Pass when it prints `N/N cases passed` and the `claude:` line says `0 fell back to rules`. A fallback
means a model call failed, so that case did not test Claude.

## 4. Claude buyer (optional)

Start check 2 again with `BUYER_MODE=agent` added at the front. The buyer side now answers on its own.

Pass when its messages never state its budget ceiling or deposit limits.

## 5. Real phone (a person)

Open the preview link from the testing guide on a phone and do a few steps. Nothing should scroll
sideways, and the buttons should be easy to tap.
