# Cloud automation — current deployment status

The GitHub workflow is active and publishes public Shorts three times per day. The first end-to-end upload succeeded on 2026-09-16: https://www.youtube.com/watch?v=9i_Ix-efT2I.

Google authorization succeeded on 2026-09-14. The YouTube API confirmed channel `UCFrBgGfylAm18PWF1YgoQTg`, titled `Quran`, and that ID is configured. The OAuth token is stored in the encrypted GitHub Actions secret `YOUTUBE_TOKEN` and is never committed to the repository.

## What the cloud runner does

On a scheduled run, it obtains the current reciter list and short-Surah audio from the Quran Foundation API, selects the next unpublished combination, creates an original Arabic title card, renders a vertical video without changing the recitation speed, verifies the authorized channel, and uploads one video. Uploaded IDs and media hashes are saved in `.bot-state/published.json` on the repository's main branch. The computer can be off when GitHub Actions runs.

The schedule is 09:00, 15:00 and 21:00 Baghdad time. GitHub may delay scheduled jobs by several minutes. The bot never silently reuses a published combination.

## Active deployment

`automation.json` is enabled with `privacy: public`. `catalog.json` uses all reciters currently returned by the Quran Foundation recitations endpoint and Surahs 112–114, each of which is checked against the 60-second Shorts limit before rendering. Descriptions credit Quran Foundation and link to its developer terms. Run `35093667675` completed the first real public upload successfully.

## Recovering an interrupted upload

The runner writes `uploading` to the remote ledger before sending upload bytes. If the result is uncertain, later runs stop. Check YouTube Studio first. If the video exists, preserve the ledger entry and set its status to `uploaded` with the real `video_id`. Remove a reservation only after confirming that no video was uploaded. Never clear the entire ledger to restart the bot. GitHub's workflow concurrency and conditional ledger writes guard against overlapping runs.

`legacy/uploaded_videos.txt` preserves the old bot's seven source IDs for reference. These are not confirmed destination YouTube upload IDs and cannot be imported as successful uploads in the new ledger.

## Artwork and source permissions

The card artwork is created by this project. Amiri font is bundled under the SIL Open Font License in `assets/Amiri-OFL.txt`; source: https://github.com/google/fonts/tree/main/ofl/amiri. Arabic labels are shaped without synthesizing Quran text. Copyright detection is never bypassed or guaranteed absent by changing speed, color or pitch.

## Checks

`python -m unittest discover -s tests -v` covers the local queue/render pipeline and the cloud publication state machine. All 26 tests passed before activation, followed by a successful real public upload through GitHub Actions.

