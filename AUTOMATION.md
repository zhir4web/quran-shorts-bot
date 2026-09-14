# Cloud automation — current deployment status

The daily workflow is prepared but **publishing is disabled**. There is no verified recording in `catalog.json` yet. A passing test run does not mean a real YouTube upload has succeeded.

Google authorization succeeded on 2026-09-14. The YouTube API confirmed channel `UCFrBgGfylAm18PWF1YgoQTg`, titled `Quran`, and that ID is configured. The token is stored privately on the setup computer; transferring it to GitHub Actions Secrets is still pending. The first cloud implementation passed GitHub Actions run 34852299714.

## What the cloud runner does

On a scheduled run, it selects the next unpublished recording from the approved catalog, downloads it, verifies its SHA-256 checksum, creates an original Arabic title card, renders a vertical video without changing the recitation speed, verifies the authorized channel, and uploads one video. Uploaded IDs are saved in `.bot-state/published.json` on the repository's main branch. The computer can be off when GitHub Actions runs.

The daily schedule is approximately 18:17 Baghdad time. GitHub may delay scheduled jobs. Exhausting the catalog stops new uploads; the bot does not invent recitations or silently reuse previous videos. Scheduled runs remain no-ops while `automation.json` has `enabled: false`.

## Requirements before activation

1. Complete Google authorization for the intended YouTube channel, using `youtube.upload` and `youtube.readonly` scopes. Keep the resulting OAuth token private. Store its JSON only in the repository's encrypted Actions secret named `YOUTUBE_TOKEN`; never commit it.
2. Verify the channel ID through the YouTube API and set it in `automation.json`.
3. Add licensed, verified complete recordings no longer than 60 seconds to `catalog.json`. Each entry needs `id`, `audio_url`, `permission_url`, `sha256`, `duration`, `surah_ar`, `surah_en`, `reciter_ar`, `attribution`, `rights`, `verified: true`, and `whole_recording: true`. Check both the actual recording and permission covering redistribution. A reciter's name or a downloadable file alone is not proof of permission.
4. Run the workflow manually in `preview` mode. Inspect its video artifact and listen to the complete recording. Verify titles, beginning/end, reciter identity, attribution and usage rights.
5. Set `enabled: true` with `privacy: private`, then perform one private upload and inspect it in YouTube Studio. Only after that should public publication be enabled. Some unaudited YouTube API projects restrict uploads to private; OAuth tokens can also expire or be revoked. Resolve those Google-side requirements before expecting unattended public uploads.

The official King Fahd Complex audio page was located, but its server did not respond during setup on 2026-09-14. No file URL, checksum or recording identity could be verified; the catalog intentionally remains empty. This is an outstanding setup requirement.

## Recovering an interrupted upload

The runner writes `uploading` to the remote ledger before sending upload bytes. If the result is uncertain, later runs stop. Check YouTube Studio first. If the video exists, preserve the ledger entry and set its status to `uploaded` with the real `video_id`. Remove a reservation only after confirming that no video was uploaded. Never clear the entire ledger to restart the bot. GitHub's workflow concurrency and conditional ledger writes guard against overlapping runs.

`legacy/uploaded_videos.txt` preserves the old bot's seven source IDs for reference. These are not confirmed destination YouTube upload IDs and cannot be imported as successful uploads in the new ledger.

## Artwork and source permissions

The card artwork is created by this project. Amiri font is bundled under the SIL Open Font License in `assets/Amiri-OFL.txt`; source: https://github.com/google/fonts/tree/main/ofl/amiri. Arabic labels are shaped without synthesizing Quran text. Copyright detection is never bypassed or guaranteed absent by changing speed, color or pitch.

## Checks

`python -m unittest discover -s tests -v` covers the local queue/render pipeline and the cloud publication state machine. Cloud tests use fake services and test fixtures; they do not upload to a real channel. Real authorization, a licensed audio download, and a private upload still require end-to-end verification before deployment is complete.
