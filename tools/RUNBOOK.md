# Hourly refresh runbook — Amazing Animal Minds KPI dashboard

The dashboard refreshes itself via `.github/workflows/refresh.yml`, a GitHub
Actions workflow that runs on GitHub's own infrastructure. It does not depend
on Claude, Cowork, or any other external session being open.

## How it works

1. **`tools/fetch_data.py`** — calls the Kit, Meta Marketing, and Google Drive
   APIs directly (using secrets stored in the repo, see below) and writes raw
   JSON/CSV files into `tools/work/`. Any source whose secret is missing or
   whose call fails is skipped; `refresh.py` keeps the previous data for that
   section rather than wiping it.
2. **`tools/refresh.py`** — decrypts the current `index.html` payload with
   `DASH_PASSWORD`, merges in the fresh data from `tools/work/`, re-encrypts,
   and rewrites `index.html`.
3. The workflow commits and pushes `index.html` only if it actually changed.
   GitHub Pages redeploys automatically within about a minute of the push.

## Required repository secrets

Set these under Settings → Secrets and variables → Actions → New repository
secret. Nothing here is ever committed to the repo itself.

| Secret name                   | What it is                                                                 |
|--------------------------------|-----------------------------------------------------------------------------|
| `DASH_PASSWORD`                | The dashboard's unlock password (also used to encrypt the data payload)     |
| `KIT_API_KEY`                  | Kit v4 API key — Kit → Settings → Advanced → API                            |
| `META_ACCESS_TOKEN`            | Meta Marketing API token, `ads_read` only, for ad account `263286035984952` |
| `GOOGLE_SERVICE_ACCOUNT_JSON`  | Full JSON key of a Google service account with Viewer access to the ThriveCart sales Google Sheet |

`META_AD_ACCOUNT_ID` and `SALES_SHEET_ID` are not secret (they're plain
identifiers, not credentials) and are hardcoded directly in the workflow.

## Turning on the hourly schedule

The workflow currently only has a manual trigger (`workflow_dispatch`) so it
can be test-run first. Once a manual run succeeds with all four secrets set:

1. Edit `.github/workflows/refresh.yml`
2. Uncomment the `schedule: - cron: '0 * * * *'` block
3. Commit — it now fires automatically, once an hour, forever, independent of
   any Claude session.

## Notes / known limitations

- `results` / `cost_per_result` in the Meta ad-tree breakdown are not
  populated by the direct-API fetch yet — spend, impressions, clicks, CTR and
  CPC are all reliable, but "Results" depends on mapping each ad's specific
  conversion event, which needs calibrating against real account data.
- If a single source fails after being retried by re-running the workflow,
  it's safe — `refresh.py` never wipes a section it didn't get fresh data for.
  Only a sales CSV with under 50 parsed orders is rejected outright (guards
  against a broken/empty export wiping sales history).
- Nothing outside `index.html` should ever be modified by a refresh run.
