# Quran Shorts dashboard

Static Kurdish UI with a Python HTTP API. GitHub and YouTube tokens are never
sent to the browser. The dashboard access key is entered in the browser and
kept in sessionStorage for that tab session.

## Run

Install requirements-dashboard.txt and set GITHUB_TOKEN, DASHBOARD_KEY,
GITHUB_REPOSITORY (owner/repository) and GITHUB_BRANCH (default main), then run
`python dashboard_server.py`. Open http://127.0.0.1:8787.

The GitHub token needs repository **Contents: read** and **Actions: write**.
Deploy dashboard_server.py, schedule_policy.py, dashboard/ and
requirements-dashboard.txt together. PORT takes precedence over DASHBOARD_PORT.
A key is required beyond localhost; expose through HTTPS and an authenticated
reverse proxy. Missing remote configuration is an error, not a local fallback.

## Behavior

- One publish request dispatches daily.yml once with mode and count. Counts
  1–5 execute sequentially inside that workflow. Other modes require 1.
- The YouTube health page has a read-only status sync action. It runs the
  existing video statistics/privacy check through GitHub Actions without
  rendering or uploading; updated results appear after the report completes.
- Recent videos show their last checked visibility and any active health flags.
  The check timestamp distinguishes current, stale, unavailable and never-checked data.
- The workflow uses queue: max to retain pending runs. GitHub queue capacity
  and availability remain external limits.
- Dispatch acceptance means requested, not uploaded. A timeout is an unknown
  outcome: check GitHub Actions before retrying.
- Repeated clicks are suppressed during an active request. This is not
  cross-device deduplication; two intentional requests remain two requests.
- Uncertain uploads block publication controls until resolved in the ledger.
- Connection verification requires a matching successful channel check within
  24 hours. A configured channel ID alone is not verification.
- Failed workflows, disabled automation, invalid schedule data and uncertain
  uploads are surfaced. Unavailable checks display unknown instead of healthy.
- Schedule rows use actual slot IDs and shared manual-post accounting.
- The library shows the latest 12 uploads.
- Refresh retries authentication after canceling the key dialog.
- The service worker caches static assets only, never API responses.

TikTok posting is not implemented. Environment credentials do not enable it.

## Tests

```text
python -m unittest discover -s tests -v
node --test tests/dashboard.test.js
```

Python tests use local HTTP servers and mocked external services. JavaScript
tests execute the actual script in a DOM harness, covering authentication,
stale data, blocked uploads, slot display and repeated clicks.

