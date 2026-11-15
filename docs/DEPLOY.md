# Testing and deploying

```
branch ──► PR ──► CI "tests" + 1 approval ──► merge to main
                                                  │
                          ┌───────────────────────┴───────────────────────┐
              at once, on the commit                       after CI on main passes
                          │                                               │
   live demo (harbour-house-group-sales) deploys      staging (harbour-house-preview) deploys
```

Both services deploy `main` automatically, and the live demo does not wait for staging or for CI on
`main`. The checks that protect it are the ones on the pull request.

## CI

`.github/workflows/tests.yml` runs the whole suite on every pull request and every push to `main`:
Python 3.13, simulated payments, the rules seller and a throwaway Postgres, so the storage tests run
too. No keys are used. The `tests` check is required by the **Protect main** ruleset, so a red PR
cannot be merged.

## Staging: `harbour-house-preview`

- https://harbour-house-preview.onrender.com, deploys `main` with **Auto-Deploy: After CI checks pass**.
- Same build and start commands as the live demo.
- Its own data: `STORAGE_NAMESPACE=groupsales_staging`, so a reset on staging never touches the live
  demo's rows in the shared Postgres.
- Its own `GS_ACCESS_TOKEN`. Airwallex sandbox keys work there too (same Render region, so the key's IP
  whitelist allows it).

## Live demo: `harbour-house-group-sales`

- Deploys `main` with **Auto-Deploy: On Commit**: every merge to `main` goes live about a minute later,
  before staging has it. To check changes on staging first, set Auto-Deploy to **Off** in Render
  (service → Settings) and use **Manual Deploy → Deploy a specific commit** with the commit you checked.
- Before judging, stop merging to `main`, or turn Auto-Deploy off.

## Checking a change on staging

1. Wait for the PR's merge to go live on staging (Render → harbour-house-preview → Events).
2. Follow the walkthrough in [README.md](../README.md#2-try-it-yourself) there, or
   `BASE=https://harbour-house-preview.onrender.com TOKEN=<staging token> node scripts/e2e_demo.js`.
3. The live demo already has the change (see above). If something is wrong, use **Rollback** on the
   live service in Render, then fix it with a new PR.
