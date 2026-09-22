# Mobile dashboard deployment

The repository can remain private. The Cloud Run image contains only the
dashboard server and its UI; it does not contain the YouTube OAuth token or the
GitHub token. Store the GitHub token and dashboard key in Secret Manager and
inject them at runtime. The YouTube token belongs only in GitHub Actions.

## One-time setup

In Google Cloud Shell, from a checkout of this private repository:

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com

# Create these secrets without putting their values in Git or Docker.
printf '%s' 'YOUR_GITHUB_FINE_GRAINED_TOKEN' | gcloud secrets create github-dashboard-token --data-file=-
printf '%s' 'A_LONG_RANDOM_DASHBOARD_PASSWORD' | gcloud secrets create dashboard-key --data-file=-

gcloud run deploy quran-shorts-dashboard \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars GITHUB_REPOSITORY=zhir4web/quran-shorts-bot,GITHUB_BRANCH=main \
  --set-secrets GITHUB_TOKEN=github-dashboard-token:latest,DASHBOARD_KEY=dashboard-key:latest
```

The GitHub token needs only **Actions: write** and **Contents: read** for this
repository. Never paste it into the browser or commit it. Cloud Run returns an
HTTPS URL; open it on the phone and choose **Add to Home Screen**. The manifest
and service worker make it behave like a small app.

For a production deployment, restrict access further with Cloud Run/IAP or an
authenticated reverse proxy. The dashboard key is a second application-level
guard, not a replacement for HTTPS.

## What remains separate

The YouTube OAuth token stays in the existing GitHub Actions secret. TikTok
requires its own approved Content Posting API app and OAuth secret; it should be
implemented as a separate integration before adding any credentials. Setting a
TikTok environment variable does not enable posting in this version.

