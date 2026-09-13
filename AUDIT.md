# Review and delivery notes

Reviewed source: the supplied `quran-shorts-bot-main.zip`. The original was retained separately and was not executed. Credentials/cookies from the original archive were not read or copied into this release.

## Problems confirmed in the original source

- Searched broadly for others' Quran videos without checking permissions. Speed and brightness edits did not establish reuse rights.
- Had no original audio-plus-image composition mode.
- GitHub Actions did not persist `uploaded_videos.txt`, so later runs could repeat earlier uploads.
- Upload failures were caught and printed while the program could still end successfully.
- No durable record for uploads whose network response was lost, and no resumable retry policy.
- The 60-second limit was applied only to search metadata; unknown durations could pass through without a render limit.
- Only landscape clips were cropped. Other aspect ratios were not normalized, and cropping could remove existing Quran text.
- Windows launcher assumed a particular Python 3.12 installation path and reinstalled unpinned dependencies on every run.
- The old workflow required working YouTube download access and OAuth credentials; no credentials were available for verification.

## Replacement behavior

- Explicit licensed queue; either compose recitation plus artwork or process an authorized existing video. HTTPS download is supported through yt-dlp without hard-coded YouTube clients or browser cookies.
- FFmpeg replaces MoviePy compatibility branches; output has audio, fixed portrait dimensions, maximum 60 seconds and a checked duration. Whole source frame and original recitation speed are preserved.
- SQLite state, output checksums, operating-system process locks, bounded upload retries and an uncertain-upload recovery command.
- Preview does not authenticate or upload. Upload defaults to private. Public uploads are selected explicitly.
- Windows daily scheduler installer; one pending item per run, persistent local state, rotating logs and nonzero failure exits.
- GitHub workflow runs tests only. It is not a cloud upload deployment. Disable/remove the original scheduled workflow when replacing the repository.
- No automatic Quran transcription, verse recognition, AI recitation, or guarantee of copyright clearance. The operator supplies verified recitation and verse boundaries. The included background is original geometric artwork without Quran text.

## Validation on this delivery

- Windows, Python 3.12, bundled FFmpeg from imageio-ffmpeg 0.6.0.
- 15 automated tests passed. They include real audio-to-video generation, existing-video conversion, landscape padding, too-short source rejection and missing-audio rejection.
- Queue permission checks, duration bounds, duplicate IDs, unsafe IDs, changed jobs, damaged output, concurrent runs and failure status tested.
- Mock YouTube tests cover private metadata, attribution, resumable retries, successful deduplication, interrupted uploads and manual recovery.
- OAuth/channel login, actual internet media downloads, live YouTube uploads and registering the Windows scheduled task were not executed. These require the user's account, licensed media and desired activation time.
- Fresh dependencies were installed successfully in an isolated environment. Direct dependencies are pinned; `requirements-lock.txt` records the tested transitive versions.

## Remaining activation requirements

1. Supply authorized recitation and fill the queue. The delivered active queue is empty on purpose.
2. Configure Google Cloud Desktop OAuth, sign in locally and verify the intended channel.
3. Review a real Quran preview, then upload one private test and check YouTube Studio.
4. Activate the daily task if desired. Keep the computer on and signed in; retain and back up the state directory securely.

YouTube may restrict uploads from unverified API projects to private visibility. OAuth refresh tokens can expire or be revoked. Network access, quotas and media availability are external dependencies. A successful test suite does not mean the channel is connected or the bot is running unattended.

Sources: [YouTube uploads](https://developers.google.com/youtube/v3/docs/videos/insert), [OAuth expiration](https://developers.google.com/identity/protocols/oauth2#expiration), [monetization and reused content](https://support.google.com/youtube/answer/1311392).

