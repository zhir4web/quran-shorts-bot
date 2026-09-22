"""Shared Baghdad scheduling rules for the runner and dashboard."""
from datetime import datetime, timedelta, timezone


BAGHDAD = timezone(timedelta(hours=3), 'Asia/Baghdad')
# Deliberately early slots absorb GitHub Actions queue delay.
PUBLICATION_HOURS = (5, 9, 15)


def parse_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def schedule_state(jobs, now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(BAGHDAD)
    prefix = local.date().isoformat() + '/'
    slots = [prefix + f'{hour:02d}:00' for hour in PUBLICATION_HOURS]
    completed, manual, latest = set(), 0, None
    for row in jobs.values():
        if row.get('status') != 'uploaded':
            continue
        slot = row.get('schedule_slot')
        if slot in slots:
            completed.add(slot)
        stamp = row.get('uploaded_at') or row.get('started_at')
        uploaded = parse_iso(stamp)
        if not uploaded:
            raise ValueError('Invalid ledger upload time; stopping to prevent extra posts')
        uploaded = uploaded.astimezone(BAGHDAD)
        if uploaded.date() == local.date() and uploaded.hour >= PUBLICATION_HOURS[0]:
            latest = max(latest, uploaded) if latest else uploaded
            if slot not in slots:
                manual += 1
    for slot in slots:
        if slot not in completed and manual:
            completed.add(slot)
            manual -= 1
    spacing = bool(latest and (local - latest).total_seconds() < 20 * 60)
    next_due = next(
        (slot for slot, hour in zip(slots, PUBLICATION_HOURS)
         if slot not in completed and local.hour >= hour),
        None,
    )
    return {'slots': slots, 'completed': completed, 'spacing': spacing,
            'next_due': None if spacing else next_due}
