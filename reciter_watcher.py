"""Weekly, notification-only watcher for new Quran Foundation reciters.

This tool is deliberately isolated from cloud_runner.py and the daily publish
workflow. A failed source/API check is reported and exits successfully.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import logging
import os
import re

import requests
import notifications

LOG = logging.getLogger("quran-bot.reciter-watcher")
QURAN_API = "https://api.quran.com/api/v4/resources/recitations"
GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = (10, 30)
STATE_PATH = ".bot-state/reciter-watcher.json"
CATALOG_PATH = "catalog.json"


class WatcherError(RuntimeError):
    """Safe diagnostic that contains no request headers, credentials, or bodies."""


class ContentsConflict(WatcherError):
    """The catalog changed while the watcher was preparing its update."""


def _valid_ids(values, label):
    if not isinstance(values, list):
        raise WatcherError(f"{label} is not a list")
    result = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise WatcherError(f"{label} contains an invalid id")
        result.add(value)
    return result


def parse_reciters(payload):
    """Return {id: display name} from the documented Quran Foundation listing."""
    if not isinstance(payload, dict) or not isinstance(payload.get("recitations"), list):
        raise WatcherError("Quran Foundation returned an invalid reciter list")
    rows = payload["recitations"]
    if not rows:
        raise WatcherError("Quran Foundation returned an empty reciter list")
    reciters = {}
    for row in rows:
        if not isinstance(row, dict):
            raise WatcherError("Quran Foundation returned an invalid reciter record")
        reciter_id = row.get("id")
        if isinstance(reciter_id, bool) or not isinstance(reciter_id, int) or reciter_id <= 0:
            raise WatcherError("Quran Foundation returned an invalid reciter id")
        if reciter_id in reciters:
            raise WatcherError("Quran Foundation returned a duplicate reciter id")
        translated = row.get("translated_name")
        translated = translated.get("name") if isinstance(translated, dict) else None
        name = row.get("reciter_name") or translated or "ناوی بەردەست نییە"
        if not isinstance(name, str):
            raise WatcherError("Quran Foundation returned an invalid reciter name")
        reciters[reciter_id] = name.strip() or "ناوی بەردەست نییە"
    return dict(sorted(reciters.items()))


def fetch_reciters(session=None):
    """Fetch the full English-name listing; the API falls back if unavailable."""
    client = session or requests
    response = client.get(
        QURAN_API,
        params={"language": "en"},
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "quran-shorts-bot-reciter-watcher/1.0"},
    )
    if response.status_code >= 400:
        raise WatcherError(f"Quran Foundation listing returned HTTP {response.status_code}")
    try:
        return parse_reciters(response.json())
    except (ValueError, TypeError):
        raise WatcherError("Quran Foundation returned unreadable reciter data") from None


def validate_snapshot(state):
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise WatcherError("Saved reciter snapshot is invalid")
    return _valid_ids(state.get("reciter_ids"), "Saved reciter snapshot")


def find_new_reciters(current, previous_ids, catalog):
    """Exclude the prior snapshot and every ID already known to the bot."""
    allowed = _valid_ids(catalog.get("allowed_reciter_ids"), "allowed_reciter_ids")
    proposed = _valid_ids(catalog.get("proposed_reciter_ids_for_review", []),
                          "proposed_reciter_ids_for_review")
    blocked = _valid_ids(catalog.get("blocked_reciter_ids", []), "blocked_reciter_ids")
    known = allowed | proposed | blocked | set(previous_ids or ())
    return {reciter_id: current[reciter_id] for reciter_id in sorted(current)
            if reciter_id not in known}


class GitHubContents:
    """Small optimistic-concurrency wrapper for the two durable JSON files."""

    def __init__(self, repository, branch, token, session=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
            raise WatcherError("GitHub repository is not configured")
        if not branch or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch):
            raise WatcherError("GitHub branch is not configured")
        if not token:
            raise WatcherError("GitHub write token is not configured")
        self.repository = repository
        self.branch = branch
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _url(self, path):
        return f"{GITHUB_API}/repos/{self.repository}/contents/{path}"

    def read_json(self, path):
        response = self.session.get(self._url(path), params={"ref": self.branch},
                                    timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            return None, None
        if response.status_code != 200:
            raise WatcherError(f"GitHub could not read {path} (HTTP {response.status_code})")
        try:
            payload = response.json()
            content = base64.b64decode(payload["content"]).decode("utf-8")
            document = json.loads(content)
            sha = payload["sha"]
        except (KeyError, ValueError, TypeError, UnicodeDecodeError):
            raise WatcherError(f"GitHub returned invalid {path}") from None
        if not isinstance(document, dict) or not isinstance(sha, str):
            raise WatcherError(f"GitHub returned invalid {path}")
        return document, sha

    def write_json(self, path, document, sha, message):
        encoded = base64.b64encode(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        ).decode("ascii")
        payload = {"message": message, "content": encoded, "branch": self.branch}
        if sha:
            payload["sha"] = sha
        response = self.session.put(self._url(path), json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code in (409, 422):
            raise ContentsConflict(f"GitHub file changed while updating {path}")
        if response.status_code not in (200, 201):
            raise WatcherError(f"GitHub could not save {path} (HTTP {response.status_code})")
        return response.json().get("content", {}).get("sha")


def add_review_proposals(contents, candidates):
    """Merge candidates into the review-only list without touching the allowlist."""
    for _attempt in range(3):
        catalog, sha = contents.read_json(CATALOG_PATH)
        if catalog is None:
            raise WatcherError("catalog.json was not found")
        allowed = _valid_ids(catalog.get("allowed_reciter_ids"), "allowed_reciter_ids")
        proposed = _valid_ids(catalog.get("proposed_reciter_ids_for_review", []),
                              "proposed_reciter_ids_for_review")
        blocked = _valid_ids(catalog.get("blocked_reciter_ids", []), "blocked_reciter_ids")
        additions = sorted(set(candidates) - allowed - proposed - blocked)
        if not additions:
            return []
        catalog["proposed_reciter_ids_for_review"] = sorted(proposed | set(additions))
        # allowed_reciter_ids is never written or changed by this watcher.
        try:
            contents.write_json(
                CATALOG_PATH,
                catalog,
                sha,
                "Suggest newly available reciters for manual review",
            )
            return additions
        except ContentsConflict:
            continue
    raise WatcherError("catalog.json kept changing; review proposals were not saved")


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _render_summary(status, checked_at, total=0, new_reciters=None, note=""):
    new_reciters = new_reciters or {}
    lines = ["## Weekly Quran Foundation reciter check", ""]
    if status == "baseline":
        lines += [
            f"Initial baseline saved at {checked_at}; no reciters were proposed on this first check.",
            f"Available reciters recorded: **{total}**.",
        ]
    elif status == "ok":
        lines += [
            f"Checked at {checked_at}. Available reciters: **{total}**.",
            "",
        ]
        if new_reciters:
            lines += ["New reciters added to proposed_reciter_ids_for_review for manual review:"]
            lines += [f"- **ID {reciter_id}** — {name}" for reciter_id, name in new_reciters.items()]
            lines += ["", "Review and approve each reciter manually before moving an ID to allowed_reciter_ids."]
        else:
            lines.append("No genuinely new reciter IDs since the last successful check.")
    else:
        lines += [
            f"Check skipped at {checked_at}; no snapshot was advanced. {note or 'The source or state store was unavailable.'}",
            "The normal daily publish workflow is separate and is not affected.",
        ]
    return "\n".join(lines).strip() + "\n"


def run_check(contents, fetcher=fetch_reciters, notifier=None, notify_env=None):
    """Run one check. Source and storage failures are reported, never raised."""
    checked_at = _utc_now()
    try:
        catalog, _catalog_sha = contents.read_json(CATALOG_PATH)
        if catalog is None:
            raise WatcherError("catalog.json was not found")
        state, state_sha = contents.read_json(STATE_PATH)
        previous_ids = validate_snapshot(state) if state is not None else None
        current = fetcher()
        if not current:
            raise WatcherError("Quran Foundation returned no reciters")

        # The first run establishes a baseline. It must not mislabel the entire
        # existing API catalog as new because no historical snapshot exists yet.
        if previous_ids is None:
            state_doc = {"schema": 1, "checked_at": checked_at,
                         "reciter_ids": sorted(current),
                         "reciter_names": {str(key): value for key, value in current.items()}}
            contents.write_json(STATE_PATH, state_doc, state_sha,
                                "Save Quran Foundation reciter watcher baseline")
            return {
                "status": "baseline", "new_reciters": {},
                "summary": _render_summary("baseline", checked_at, len(current)),
            }

        candidates = find_new_reciters(current, previous_ids, catalog)
        added = add_review_proposals(contents, candidates) if candidates else []
        added_reciters = {reciter_id: current[reciter_id] for reciter_id in added}

        # Send one short, idempotency-keyed alert per reciter. The GitHub run
        # summary remains the complete fallback if Discord is not configured.
        if notifier and added_reciters:
            for reciter_id, name in added_reciters.items():
                message = (
                    "قورئانخوێنی نوێ دۆزرایەوە و بۆ پشکنینی دەستی زیاد کرا:\n"
                    f"ناسنامە: {reciter_id}\nناو: {name}\n\n"
                    "تکایە پێش بەکارهێنان، خۆت لە catalog.json پەسەندی بکە و "
                    "ناسنامەکە بگوازەوە بۆ allowed_reciter_ids."
                )
                try:
                    notifier(message, kind="info", key=f"reciter-review:{reciter_id}",
                             env=notify_env or {})
                except Exception as exc:
                    LOG.warning("Reciter notification unavailable: %s", type(exc).__name__)

        state_doc = {"schema": 1, "checked_at": checked_at,
                     "reciter_ids": sorted(current),
                     "reciter_names": {str(key): value for key, value in current.items()}}
        contents.write_json(STATE_PATH, state_doc, state_sha,
                            "Update Quran Foundation reciter watcher snapshot")
        return {
            "status": "ok", "new_reciters": added_reciters,
            "summary": _render_summary("ok", checked_at, len(current), added_reciters),
        }
    except Exception as exc:
        # Never leak request details or credentials and never fail a publish job.
        note = str(exc) if isinstance(exc, WatcherError) else f"temporary error ({type(exc).__name__})"
        summary = _render_summary("skipped", checked_at, note=note)
        LOG.warning("Reciter watcher skipped: %s", note)
        return {"status": "skipped", "new_reciters": {}, "summary": summary}


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    env = os.environ
    try:
        contents = GitHubContents(
            env.get("GITHUB_REPOSITORY", ""),
            env.get("GITHUB_REF_NAME", "main"),
            env.get("GITHUB_TOKEN", ""),
        )
        result = run_check(
            contents,
            notifier=notifications.notify,
            notify_env={
                "DISCORD_WEBHOOK_URL": env.get("DISCORD_WEBHOOK_URL", ""),
                "NOTIFY_RUN_URL": env.get("NOTIFY_RUN_URL", ""),
            },
        )
    except Exception as exc:
        checked_at = _utc_now()
        note = str(exc) if isinstance(exc, WatcherError) else f"temporary error ({type(exc).__name__})"
        result = {"status": "skipped", "summary": _render_summary("skipped", checked_at, note=note)}
        LOG.warning("Reciter watcher skipped: %s", note)
    _publish_summary(result["summary"], env.get("GITHUB_STEP_SUMMARY"))
    # This standalone watcher is informational. Its failures never gate daily uploads.
    return 0


def _publish_summary(summary, path=None):
    print(summary, flush=True)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as output:
                output.write(summary + "\n")
        except OSError as exc:
            LOG.warning("Could not write GitHub step summary: %s", type(exc).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
