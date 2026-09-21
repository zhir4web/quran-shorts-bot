# Quran Shorts dashboard

The dashboard is a small server-rendered static UI with a Python API. It keeps
GitHub and YouTube credentials on the server and calls the existing workflow;
the browser never receives a token.

## Local run

Set a GitHub token with repository **Actions: write** and **Contents: read**
permissions, then run:

```powershell
$env:GITHUB_TOKEN = "your-token"
$env:DASHBOARD_KEY = "a-long-random-local-key"  # optional on localhost
python dashboard_server.py
```

Open `http://127.0.0.1:8787`. The dashboard reads `automation.json`,
`catalog.json`, and `.bot-state/published.json` from the configured repository
and dispatches `.github/workflows/daily.yml` in `preview`, `publish`, or
`scheduled` mode. The manual count is capped at five and dispatches serially;
the existing remote ledger and workflow concurrency group remain the source of
truth for duplicate protection.

To expose it beyond localhost, set `DASHBOARD_HOST` and **always** set
`DASHBOARD_KEY`. Put the dashboard behind HTTPS and an authenticated reverse
proxy in production.

## TikTok module

The TikTok page is intentionally shown as a separate integration boundary. It
is not marked connected until TikTok Content Posting API credentials and OAuth
approval are configured. No TikTok token is accepted by the browser or written
to the repository.

