# Review and repairs — 2026-09-22

چاکسازییەکان لە کۆپیی ناوخۆیی جێبەجێ کران. 94 تاقیکردنەوەی Python و 4 تاقیکردنەوەی JavaScript سەرکەوتوو بوون. کۆدەکە بۆ GitHub push کراوە؛ ئەم پەڕەیە دۆخی ڕاستەقینەی asset و تاقیکردنەوەی ناوخۆیی جیا دەکاتەوە لە بڵاوکردنەوەی ڕاستەوخۆی YouTube.

## Scope and status

Reviewed and repaired the supplied local source tree. The working directory is
an exported copy without a `.git` directory, but the repaired files were
synced to the GitHub `main` branch. The production automation configuration,
channel identity and existing publication ledger were preserved. External
account connectivity was not inferred from local files.

## Repairs

### Publication and scheduling

- Corrected the workflow dispatch endpoint to use daily.yml rather than its
  repository path.
- Changed multi-post requests to one workflow with a validated count of 1–5.
  The runner processes posts sequentially and stops on failure or exhaustion.
- Added GitHub's documented queue: max setting to retain pending runs.
- Saved the completed video ID before optional comment operations. Comment
  failure cannot turn a known completed upload into an uncertain upload.
- Made preview state read-only, including skipped audio candidates and
  workflow reporting. A GitHub preview can read the ledger without changing it.
- Used the workflow branch for the remote ledger instead of hard-coded main.
- Shared Baghdad slot accounting between the runner and dashboard.
- Removed double counting when timing history and uploaded jobs describe the
  same publication. Backfilled uploads no longer inflate heartbeat counts.
- Stopped treating deleted videos or encoding failures as reasons to blacklist
  every recording by that reciter. Copyright rejection and regional
  restrictions can still trigger reciter blocking.
- Checked the authorized channel before pre-publication restriction updates.
  An unavailable restriction check now prevents a new publication.

### Dashboard and request handling

- A channel ID means configured, not connected. A matching successful channel
  check less than 24 hours old is required for recent verification.
- Displayed uncertain uploads, failed workflows, disabled automation, invalid
  schedule data, missing videos and unknown checks.
- Used actual completed slot IDs; total daily uploads no longer mark the wrong
  schedule rows complete. Legacy timestamps are included where available.
- Blocked publication controls for uncertain uploads and disabled automation.
- Added strict JSON object, content type, length, mode and count validation.
- Rejected cross-origin publication requests and invalid hosts on keyless local
  access. Used safe diagnostics for remote failures rather than raw errors.
- Corrected cancel behavior in the access-key dialog, prevented overlapping
  refreshes and duplicate in-flight publication clicks, and cleared stale
  success indicators after failed refreshes.
- Moved service worker registration out of inline HTML to comply with CSP.
  API responses are never cached; unrelated origin caches are not deleted.
- Fixed the mobile menu backdrop selector and added a visible refresh control.
- Marked TikTok as unimplemented regardless of environment variables.
- Included the shared schedule module in Docker and honored Cloud Run PORT.

### Media and content

- Replaced the reshaper path that removed Quran diacritics with HarfBuzz and
  FreeType glyph layout. The original Unicode text is shaped without stripping
  marks; unsupported glyphs or text outside the safe card bounds stop rendering.
- Sized line spacing from rendered text height to avoid overlapping marks.
- Rejected malformed source paths that could redirect a Quran audio URL to a
  different host. Compared returned verse IDs when present.
- Rejected failed media decodes even when stderr contains a duration header.
- Validated minimum duration and incompatible background/filter combinations.
  Relative filmed-background paths now resolve against the queue directory.
- Measured legacy concatenated recordings instead of trusting API duration alone.
- Bounded candidate scans and preserved the continuation cursor for publication.
  An unsuitable preferred reciter no longer prevents trying other reciters.
- Added one checked-in filmed clip for each of the five catalog themes and
  recorded every source page in `assets/backgrounds/video/LICENSES.md`.
- Removed silent cross-theme background fallback. A production entry now fails
  closed when its reviewed theme asset is missing.
- Restored `PYTHONUNBUFFERED=1` for every workflow step and reduced the runner
  timeout from 120 to 60 minutes.
- Title, description and card labels now show an ayah range such as
  `7:141–142` as `Ayahs 141–142` / `الآيات 141–142`.
- Kept only the latest 30 metrics snapshots per tracked video.

### Setup and documentation

- Installed Python 3.12.14 in an isolated local environment and synchronized
  the pinned dependency lock. setup.bat can reuse an existing environment.
- Updated CI to run on Windows and Linux and include JavaScript behavior tests.
- Rewrote stale README, automation and dashboard guidance. Corrected statements
  about workflow names, credential handling, background assets and TikTok.
- Local test tooling lives under ignored .tools/ and .venv/ directories.

## Validation actually performed

- **94 Python tests passed** on Windows / Python 3.12.14. Includes real FFmpeg
  composition/conversion, upload failure handling, bounded scans, timing,
  Arabic rendering, and authenticated local HTTP endpoint tests.
- **4 JavaScript tests passed**, executing the dashboard script in a DOM harness:
  uncertain upload blocking, correct slot rows, stale data, canceled
  authentication and repeated clicks.
- Ruff undefined-name/unused-symbol checks passed.
- Dependency consistency checks passed after syncing requirements-lock.txt.
- All five checked-in filmed backgrounds passed the source-resolution check
  (minimum dimension 1080px): forest rain, mist mountains, starry night, ocean
  moon and dawn mosque.
- Generated a 1080×1920, three-second local test-tone video using the real filmed
  background and Arabic card; frame, duration, audio and visible-motion checks
  passed. Inspected the generated frame visually. This is a rendering fixture,
  **not a Quran recitation or a publishable preview**:
  state/review-preview/video.mp4 and state/review-preview/frame.png.
- Actionlint 1.7.12 passed after ignoring its single outdated-schema diagnostic
  for queue: max. GitHub's current documentation explicitly supports this field;
  that field was verified against the docs, not accepted by this linter.
- Browser UI screenshots could not be tested because no connected browser was
  available. HTTP and JavaScript tests were completed instead.

## External validation still required

The repaired source is pushed to the remote repository. Live OAuth refresh,
real Quran API availability, a YouTube upload, the channel's processing
outcome and Linux CI execution were not exercised in this session. They require
the configured external services.

The dashboard's connection indicator is a recent observation, not a continuous
live probe. GitHub cron timing and queue limits remain external constraints.
Two separate deliberate publish requests are two requests; in-flight click
suppression is not cross-device idempotency. The included catalog/asset
attribution does not itself prove every use is authorized.

## Primary technical references

- [GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows):
  the workflow identifier may be its file name.
- [GitHub concurrency and queued runs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency):
  queue: max supports multiple pending runs.
- [HarfBuzz Python bindings](https://github.com/harfbuzz/uharfbuzz) and
  [FreeType Python bindings](https://freetype-py.readthedocs.io/):
  glyph shaping and rasterization.

