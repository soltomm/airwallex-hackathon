# Review dashboard changes through pull requests

The live dashboard is https://harbour-house-group-sales.onrender.com/.
Render runs the deployed application; the development environment runs a separate
copy for edits and tests. A pull request preview gives reviewers a public URL for
the proposed change before merging it.

## One-time Render settings

Open the existing `harbour-house-group-sales` service in Render:

1. Confirm its connected repository is `soltomm/airwallex-hackathon` and its
   deployment branch is `main`.
2. Enable automatic deployments. When available, choose deployment after CI
   checks pass. The repository's `Tests` workflow checks pull requests and main.
3. Find the service's pull request preview settings. Before enabling previews,
   configure preview-specific environment values as listed below. Verify which
   preview features and plans your Render workspace supports; enabling them may
   require a paid plan. Keep the existing live service settings unchanged.
4. Enable previews for the pull requests you want to review. Render should attach
   deployment information to the pull request; use the preview URL from that
   deployment, rather than guessing its hostname.

### Preview environment

Previews must use simulated payments, the rules seller, and independent local
state. Set these values **for previews only** before starting a preview:

| Variable | Preview value |
| --- | --- |
| `PAYMENTS` | `simulated` |
| `SELLER_MODE` | `rules` |
| `BUYER_MODE` | `presenter` |
| `DATABASE_URL` | Unset or empty; do not inherit the live database connection |
| `GS_VAR` | `var/groupsales` |
| `PUBLIC_BASE_URL` | Unset or empty; the app uses the preview's `RENDER_EXTERNAL_URL` |
| `AWX_CLIENT_ID`, `AWX_API_KEY`, `AWX_WEBHOOK_SECRET` | Unset or empty |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | Unset or empty |

Keep a dashboard access token for each preview if the workspace supports it.
Never put tokens in commits or PR comments. If the preview inherits the live
token, treat it as confidential and open it only through your authorized browser.

Do not start a preview that still inherits the live `DATABASE_URL`: the existing
`STORAGE_NAMESPACE=groupsales` would make it share the live scenario's state.
Local preview state is disposable and may reset on redeploy or idle shutdown.
If the service settings cannot supply preview-specific values, leave previews
disabled until a separate preview configuration is prepared.

## Each change

1. Describe the dashboard change in the development chat and request a draft PR.
2. The agent edits a feature branch, runs the offline tests, and opens a PR.
3. Wait for the GitHub `Tests` check and Render preview deployment. Open the
   preview URL and review the actual proposed dashboard. Request revisions in
   the same chat; further commits update the PR and its preview.
4. Approve and merge the PR when satisfied. Render deploys `main`; confirm the
   deployment succeeds, then refresh the live dashboard.

`/healthz` should return `{"ok": true}`. The main dashboard is protected when
`GS_ACCESS_TOKEN` is set: retrieve it privately from the service's Environment
settings and visit `<service URL>/?k=<token>` once to establish the cookie. A
free Render service may take about a minute to wake up after being idle.

## Development environment access

Allow `api.github.com` for PR operations, `render.com` for deployment documentation,
and `harbour-house-group-sales.onrender.com` to inspect the live dashboard.
After Render creates a preview, allow its exact hostname too. Existing GitHub
Git access and package-registry rules should be retained.

The localhost server in the development environment does not provide a public
Render preview URL. Publication of the development environment and deployment
of the application to Render are separate operations.
