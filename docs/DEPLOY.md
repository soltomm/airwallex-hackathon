# Testing and deploying

```
branch ──► PR ──► CI "tests" + 1 approval ──► merge to main
                                                  │
                                CI "tests" on main passes
                                                  │
                        staging (harbour-house-preview) deploys automatically
                                                  │
                            check it, then promote by hand
                                                  │
                      live demo (harbour-house-group-sales) deploys
```

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

- Deploys `main` with **Auto-Deploy: Off**. After a change looks right on staging, open the service in
  Render and use **Manual Deploy → Deploy latest commit** (or **Deploy a specific commit** with the
  commit you checked on staging).
- Before judging, stop merging to `main`, or at least stop promoting.

## Checking a change on staging

1. Wait for the PR's merge to go live on staging (Render → harbour-house-preview → Events).
2. Run the demo script in [GROUP_SALES.md](../GROUP_SALES.md#demo-script-about-6-minutes) there, or
   `BASE=https://harbour-house-preview.onrender.com TOKEN=<staging token> node scripts/e2e_demo.js`.
3. Promote to the live demo as above.
