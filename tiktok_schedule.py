"""Select and safely gate the three daily TikTok inbox-draft slots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import sys

import requests

from tiktok_draft import GitHubDraftLedger, TikTokDraftError


BAGHDAD = timezone(timedelta(hours=3), 'Asia/Baghdad')
SCHEDULE_HOURS = (6, 12, 18)
MAX_PENDING_SHARES = 5
PENDING_WINDOW = timedelta(hours=24)


def _aware_now(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Schedule clock must include a timezone')
    return now.astimezone(BAGHDAD)


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def pending_share_count(runs, now=None):
    """Count recent draft attempts that may still be pending at TikTok."""
    current = _aware_now(now).astimezone(timezone.utc)
    count = 0
    pending_states = {'uploading', 'processing_upload', 'send_to_user_inbox', 'needs_review'}
    for row in runs.values():
        if not isinstance(row, dict) or str(row.get('status', '')).lower() not in pending_states:
            continue
        started = _parse_timestamp(row.get('started_at'))
        if started and timedelta(0) <= current - started.astimezone(timezone.utc) < PENDING_WINDOW:
            count += 1
    return count


def next_due_slot(runs, now=None):
    """Return the earliest unattempted due slot for today in Baghdad time."""
    local = _aware_now(now)
    claimed = {row.get('schedule_slot') for row in runs.values() if isinstance(row, dict)}
    for hour in SCHEDULE_HOURS:
        due = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        slot = f'{local.date().isoformat()}/{hour:02d}:00'
        if local >= due and slot not in claimed:
            return slot
    return None


def tiktok_ready(session=None, base_url=None, dashboard_key=None):
    """Fail closed unless TikTok is configured and the creator granted video.upload."""
    base_url = (base_url or os.environ.get('DASHBOARD_URL', '')).rstrip('/')
    dashboard_key = dashboard_key or os.environ.get('DASHBOARD_KEY', '')
    if not base_url.startswith('https://') or not dashboard_key:
        return False
    client = session or requests.Session()
    try:
        response = client.get(base_url + '/api/tiktok/status', headers={
            'X-Dashboard-Key': dashboard_key, 'Accept': 'application/json'}, timeout=20)
        if not response.ok:
            return False
        status = response.json()
    except (requests.RequestException, ValueError, AttributeError):
        return False
    scopes = str(status.get('scope') or '').replace(',', ' ').split() if isinstance(status, dict) else []
    return (isinstance(status, dict) and status.get('configured') is True and
            status.get('connected') is True and 'video.upload' in scopes)


def select_slot(*, now=None, ledger=None, env=None, session=None):
    """Fail closed on incomplete TikTok setup, full inbox quota, or bad state."""
    env = os.environ if env is None else env
    if not tiktok_ready(session=session, base_url=env.get('DASHBOARD_URL'),
                        dashboard_key=env.get('DASHBOARD_KEY')):
        return None
    ledger = ledger or GitHubDraftLedger(env.get('GITHUB_TOKEN', ''),
        env.get('GITHUB_REPOSITORY', ''), env.get('GITHUB_REF_NAME', 'main'), session=session)
    data, _ = ledger._read()
    runs = data.get('runs', {}) if isinstance(data, dict) else {}
    if not isinstance(runs, dict) or pending_share_count(runs, now=now) >= MAX_PENDING_SHARES:
        return None
    return next_due_slot(runs, now=now)


def write_output(path, slot):
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as output:
        print('slot=' + (slot or ''), file=output)


def main():
    try:
        slot = select_slot()
    except (TikTokDraftError, requests.RequestException, ValueError) as error:
        # This is a separate workflow; transient setup/state issues should skip
        # this heartbeat and let the next scheduled check catch up safely.
        print('TikTok scheduled draft check skipped: ' + str(error), file=sys.stderr, flush=True)
        slot = None
    if slot:
        print('TikTok draft slot is due: ' + slot, flush=True)
    else:
        print('No TikTok draft slot is due or the account is not ready.', flush=True)
    write_output(os.environ.get('GITHUB_OUTPUT'), slot)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
