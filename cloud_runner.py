"""Daily cloud runner with a remote write-ahead upload ledger.

The catalog contains complete, licensed recordings and verified metadata.
No token or rendered media is written to Git history.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlparse

import requests
import background_provider
import bot
import notifications
from schedule_policy import BAGHDAD, PUBLICATION_HOURS as PUBLICATION_HOURS, schedule_state

ROOT = Path(__file__).resolve().parent
STATE_PATH = '.bot-state/published.json'
QURAN_API = 'https://api.quran.com/api/v4/'
QURAN_AUDIO = 'https://verses.quran.foundation/'
SHORT_SURAHS = {
    112: ('الإخلاص', 'Al-Ikhlas'),
    113: ('الفلق', 'Al-Falaq'),
    114: ('الناس', 'An-Nas'),
}

class CloudError(RuntimeError):
    """A diagnostic safe to display without external response bodies."""


class TooShortRecording(RuntimeError):
    """Complete recitation is below the minimum; select another, never pad it."""


class TooLongRecording(RuntimeError):
    """The complete recording cannot fit safely in a YouTube Short."""


class NoEligibleVerse(CloudError):
    def __init__(self, cursor, candidates_checked=0, timed_out=False, rejected=None):
        reason = 'timed out' if timed_out else 'found no eligible verse'
        details = ''
        if rejected:
            summary = ', '.join(f'{key}={value}' for key, value in rejected.items() if value)
            if summary:
                details = f' ({summary})'
        super().__init__(
            f'Quran Foundation search {reason} after {candidates_checked} candidates{details}; '
            f'the next run resumes at position {cursor}'
        )
        self.cursor = cursor


class RemoteLedger:
    def __init__(self, repository, token, branch='main'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid repository')
        self.url = f'https://api.github.com/repos/{repository}/contents/{STATE_PATH}'
        self.catalog_url = f'https://api.github.com/repos/{repository}/contents/catalog.json'
        self.clip_base_url = f'https://api.github.com/repos/{repository}/contents/.bot-state/clip-submissions'
        self.branch = branch
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}',
                                     'Accept': 'application/vnd.github+json'})
        self.sha = None
        self.data = {'schema': 1, 'jobs': {}}

    def load(self):
        response = self.session.get(self.url, params={'ref': self.branch}, timeout=30)
        if response.status_code == 404:
            if self.sha or self.data.get('jobs'):
                raise CloudError('Remote ledger disappeared; stopping to prevent duplicates')
            return
        if response.status_code != 200:
            raise RuntimeError(f'Cannot read remote ledger (HTTP {response.status_code})')
        payload = response.json()
        self.sha = payload['sha']
        self.data = json.loads(base64.b64decode(payload['content']))
        if not isinstance(self.data, dict) or self.data.get('schema') != 1 or not isinstance(self.data.get('jobs'), dict):
            raise ValueError('Remote ledger is invalid; stopping to prevent duplicates')
        for row in self.data['jobs'].values():
            if not isinstance(row, dict) or row.get('status') not in ('uploading', 'uploaded'):
                raise ValueError('Remote ledger contains an invalid job')
            if row['status'] == 'uploaded' and not row.get('video_id'):
                raise ValueError('Remote ledger is missing an uploaded video ID')
        cursor = self.data.get('cursor', len(self.data['jobs']))
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError('Remote ledger cursor is invalid')
        self.data['cursor'] = cursor

    def save(self):
        # Never retry an uncertain write blindly. A later run reads the remote record.
        encoded = base64.b64encode(json.dumps(self.data, ensure_ascii=False, sort_keys=True).encode()).decode()
        payload = {'message': 'Record Quran bot progress [skip ci]', 'content': encoded, 'branch': self.branch}
        if self.sha:
            payload['sha'] = self.sha
        response = self.session.put(self.url, json=payload, timeout=30)
        if response.status_code not in (200, 201):
            raise RuntimeError(f'Remote ledger not saved (HTTP {response.status_code}); no new upload will start')
        self.sha = response.json()['content']['sha']

    def load_custom_clip(self, clip_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(clip_id or '')):
            raise CloudError('Invalid custom video id')
        response = self.session.get(f'{self.clip_base_url}/{clip_id}.json',
                                    params={'ref': self.branch}, timeout=30)
        if response.status_code != 200:
            raise CloudError('Custom video submission was not found')
        try:
            payload = response.json()
            record = json.loads(base64.b64decode(payload['content']))
        except (KeyError, ValueError, TypeError):
            raise CloudError('Custom video submission is invalid') from None
        if (record.get('id') != clip_id or record.get('status') not in {'queued', 'processing'} or
                record.get('license_confirmed') is not True):
            raise CloudError('Custom video is not queued with licence confirmation')
        return record, payload['sha']

    def save_custom_clip(self, record, sha, message):
        clip_id = record.get('id')
        content = base64.b64encode(json.dumps(record, ensure_ascii=False, indent=2).encode()).decode()
        payload = {'message': message, 'content': content, 'branch': self.branch, 'sha': sha}
        response = self.session.put(f'{self.clip_base_url}/{clip_id}.json', json=payload, timeout=30)
        if response.status_code not in (200, 201):
            print('Custom video status could not be saved; ledger remains authoritative')
            return sha
        return response.json().get('content', {}).get('sha', sha)

    def block_reciter(self, catalog, reciter_id):
        """Persist a newly unsafe reciter without changing the reviewed allowlist."""
        if reciter_id in catalog.get('blocked_reciter_ids', []):
            return
        response = self.session.get(self.catalog_url, params={'ref': self.branch}, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f'Cannot read catalog for safety update (HTTP {response.status_code})')
        payload = response.json()
        remote = json.loads(base64.b64decode(payload['content']))
        blocked = remote.setdefault('blocked_reciter_ids', [])
        if reciter_id not in blocked:
            blocked.append(reciter_id)
            blocked.sort()
        remote['allowed_reciter_ids'] = [value for value in remote.get('allowed_reciter_ids', [])
                                         if value != reciter_id]
        body = {'message': 'Auto-block reciter after YouTube restriction [skip ci]',
                'content': base64.b64encode(json.dumps(remote, ensure_ascii=False, indent=2).encode()).decode(),
                'branch': self.branch, 'sha': payload['sha']}
        written = self.session.put(self.catalog_url, json=body, timeout=30)
        if written.status_code not in (200, 201):
            raise RuntimeError(f'Cannot save catalog safety update (HTTP {written.status_code})')
        catalog['blocked_reciter_ids'] = sorted(set(catalog.get('blocked_reciter_ids', [])) | {reciter_id})
        catalog['allowed_reciter_ids'] = [value for value in catalog['allowed_reciter_ids'] if value != reciter_id]


def get_json(url, params=None):
    """Fetch Quran Foundation data with visible, bounded progress logging."""
    for attempt in range(3):
        print(f'Quran API request {attempt + 1}/3: {url}', flush=True)
        try:
            response = requests.get(url, params=params, timeout=(10, 30),
                                    headers={'User-Agent': 'quran-shorts-bot/2.0'})
            response.raise_for_status()
            payload = response.json()
            print('Quran API response received', flush=True)
            return payload
        except (requests.RequestException, ValueError):
            print('Quran API request failed; retrying safely', flush=True)
            if attempt == 2:
                raise RuntimeError('Quran Foundation source is temporarily unavailable') from None
            time.sleep(2 ** attempt)


def api_entries(data):
    if data.get('provider') != 'quran_foundation' or data.get('reciters') != 'all':
        raise ValueError('Unsupported catalog provider configuration')
    chapters = data.get('chapters')
    if not isinstance(chapters, list) or not chapters or any(chapter not in SHORT_SURAHS for chapter in chapters):
        raise ValueError('Only approved short Surahs may be scheduled')
    permission_url = data.get('permission_url', '')
    if not permission_url.startswith('https://api-docs.quran.com/'):
        raise ValueError('Quran Foundation permission URL required')
    english = get_json(urljoin(QURAN_API, 'resources/recitations'), {'language': 'en'}).get('recitations', [])
    arabic = get_json(urljoin(QURAN_API, 'resources/recitations'), {'language': 'ar'}).get('recitations', [])
    arabic_names = {row['id']: row.get('translated_name', {}).get('name') for row in arabic}
    if not english:
        raise RuntimeError('No Quran Foundation reciters are currently available')
    entries = []
    for chapter in chapters:
        surah_ar, surah_en = SHORT_SURAHS[chapter]
        for reciter in english:
            reciter_id = reciter.get('id')
            reciter_ar = arabic_names.get(reciter_id) or reciter.get('reciter_name')
            style = reciter.get('style')
            label = reciter.get('reciter_name', '').strip()
            if style:
                label += f' ({style})'
            entries.append({
                'id': f'qf-r{reciter_id}-s{chapter}', 'source_type': 'quran_foundation',
                'recitation_id': reciter_id, 'chapter': chapter,
                'surah_ar': surah_ar, 'surah_en': surah_en,
                'reciter_ar': reciter_ar, 'reciter_en': label,
                'permission_url': permission_url,
                'attribution': data.get('attribution', ''), 'rights': data.get('rights', ''),
                'verified': True, 'whole_recording': True,
            })
    return entries


def validate_verse_catalog(data):
    if data.get('provider') != 'quran_foundation' or data.get('reciters') != 'allowlist':
        raise ValueError('Unsupported Quran Foundation catalog configuration')
    if data.get('content') != 'complete_verses':
        raise ValueError('Only complete-verse publishing is supported')
    limit = float(data.get('max_audio_seconds', 0))
    tail = float(data.get('tail_silence_seconds', 0))
    minimum = float(data.get('min_audio_seconds', 30))
    if not 30 <= minimum <= limit:
        raise ValueError('Minimum recitation duration must be at least 30 seconds and within the maximum')
    if not all(math.isfinite(value) for value in (limit, tail, minimum)) or limit <= 0 or limit + tail > 60 or tail < 0.5:
        raise ValueError('Invalid Short duration or ending-silence configuration')
    if not str(data.get('permission_url', '')).startswith('https://api-docs.quran.com/'):
        raise ValueError('Quran Foundation permission URL required')
    for field in ('attribution', 'rights'):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f'{field} required')
    allowed = data.get('allowed_reciter_ids')
    blocked = data.get('blocked_reciter_ids', [])
    if (not isinstance(allowed, list) or not allowed or
            any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in allowed)):
        raise ValueError('A verified reciter allowlist is required')
    if (not isinstance(blocked, list) or
            any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in blocked)):
        raise ValueError('Invalid blocked-reciter list')
    if len(set(allowed)) != len(allowed) or set(allowed) & set(blocked):
        raise ValueError('Reciter lists must be unique and disjoint')
    selection = data.get('reciter_selection', {})
    if not isinstance(selection, dict):
        raise ValueError('Invalid reciter selection configuration')
    weight = float(selection.get('tajwid_weight', 0.35))
    keywords = selection.get('tajwid_keywords', ['tajwid', 'tajweed', 'mujawwad', 'mujawid'])
    if not 0 < weight <= 1 or (not isinstance(keywords, list) or
                               not all(isinstance(value, str) and value.strip() for value in keywords)):
        raise ValueError('Invalid tajwid reciter selection configuration')
    if not isinstance(data.get('performance_bias_enabled', False), bool):
        raise ValueError('Performance preference must be explicitly enabled or disabled')
    minimum_samples = data.get('performance_bias_min_videos', 3)
    if isinstance(minimum_samples, bool) or not isinstance(minimum_samples, int) or not 2 <= minimum_samples <= 20:
        raise ValueError('Performance preference needs a valid minimum sample count')
    try:
        performance_strength = float(data.get('performance_bias_strength', 0.15))
    except (TypeError, ValueError):
        performance_strength = float('nan')
    if not math.isfinite(performance_strength) or not 0 <= performance_strength <= 0.25:
        raise ValueError('Performance preference must remain a small, bounded bias')
    if data.get('visual_style') not in ('premium_rotating_scenes', 'real_video_assets'):
        raise ValueError('The real-video background style is required')
    themes = data.get('visual_themes')
    if (not isinstance(themes, list) or not 12 <= len(themes) <= 30 or
            len(set(themes)) != len(themes) or
            any(not isinstance(theme, str) or not re.fullmatch(r'[a-z0-9_]{3,80}', theme) for theme in themes)):
        raise ValueError('At least 12 unique licensed visual themes are required')
    if data.get('text_presentation') != 'static_full_ayah':
        raise ValueError('The complete Arabic ayah must remain visible for the whole Short')
    if data.get('background_orientation') != 'portrait':
        raise ValueError('Licensed portrait background footage is required')
    clip_count = data.get('background_clips_per_video')
    if isinstance(clip_count, bool) or not isinstance(clip_count, int) or not 2 <= clip_count <= 4:
        raise ValueError('Each Short must mix two to four different background clips')
    if data.get('show_verified_ayah_text') is not True:
        raise ValueError('Verified ayah text must be shown')
    maximum_text = data.get('max_ayah_characters')
    if isinstance(maximum_text, bool) or not isinstance(maximum_text, int) or not 80 <= maximum_text <= 240:
        raise ValueError('Invalid ayah text limit')
    return data


def _is_tajwid_reciter(reciter, catalog):
    """Identify the slower rotation class from Quran Foundation metadata."""
    selection = catalog.get('reciter_selection', {})
    keywords = selection.get('tajwid_keywords', ['tajwid', 'tajweed', 'mujawwad', 'mujawid'])
    text = ' '.join(str(reciter.get(field) or '') for field in ('style', 'reciter_name')).casefold()
    return any(str(keyword).casefold() in text for keyword in keywords)


def _reciter_order(reciters, jobs, position, catalog, performance_history=None):
    """Rank every allowed reciter fairly, with only a small optional view bias."""
    selection = catalog.get('reciter_selection', {})
    tajwid_weight = float(selection.get('tajwid_weight', 0.35))
    usage = {reciter.get('id'): 0 for reciter in reciters}
    for row in jobs.values():
        reciter_id = row.get('reciter_id')
        if reciter_id in usage:
            usage[reciter_id] += 1

    bias = float(catalog.get('performance_bias_strength', 0.15)) \
        if catalog.get('performance_bias_enabled') is True else 0.0
    minimum = catalog.get('performance_bias_min_videos', 3)
    preferences = (_performance_preferences(
        performance_history or {}, jobs, 'reciter_id', minimum)
        if bias else {})
    ranked = []
    for index, reciter in enumerate(reciters):
        reciter_id = reciter.get('id')
        weight = tajwid_weight if _is_tajwid_reciter(reciter, catalog) else 1.0
        # A 0.15 maximum adjustment cannot outweigh one whole prior selection.
        score = (usage[reciter_id] + weight) / weight
        score -= bias * preferences.get(reciter_id, 0.0)
        tajwid_first = 1 if _is_tajwid_reciter(reciter, catalog) else 0
        tie_break = (position + index) % max(1, len(reciters))
        ranked.append((score, tajwid_first, tie_break, index, reciter))
    ranked.sort(key=lambda item: item[:4])
    return [item[4] for item in ranked]


def _visual_theme_order(themes, jobs, position, catalog, performance_history=None):
    """Prefer less-used themes; optionally nudge equally used themes by views."""
    themes = list(dict.fromkeys(themes or []))
    if not themes:
        return []
    usage = {theme: 0 for theme in themes}
    for row in jobs.values():
        theme = row.get('visual_theme')
        if theme in usage and row.get('status') in ('uploading', 'uploaded'):
            usage[theme] += 1

    bias = float(catalog.get('performance_bias_strength', 0.15)) \
        if catalog.get('performance_bias_enabled') is True else 0.0
    minimum = catalog.get('performance_bias_min_videos', 3)
    preferences = (_performance_preferences(
        performance_history or {}, jobs, 'visual_theme', minimum)
        if bias else {})
    ranked = []
    for index, theme in enumerate(themes):
        score = usage[theme] - bias * preferences.get(theme, 0.0)
        # Keep catalog order for equal scores so cursor advancement visits each theme.
        ranked.append((score, index, theme))
    ranked.sort(key=lambda item: item[:2])
    return [item[2] for item in ranked]


def passage_overlaps_jobs(jobs, chapter_id, verse_start, verse_end):
    """Return True when a previously uploaded/in-flight ayah overlaps this passage."""
    chapter_id = int(chapter_id)
    verse_start, verse_end = int(verse_start), int(verse_end)
    for job_id, row in jobs.items():
        if not isinstance(row, dict) or row.get('status') not in ('uploaded', 'uploading'):
            continue
        existing_chapter = row.get('chapter_id')
        existing_start = row.get('verse_start')
        existing_end = row.get('verse_end')
        match = re.fullmatch(
            r'qf-v-r\d+-a(\d+)-(\d+)(?:-to-(\d+))?', str(job_id))
        if match:
            existing_chapter = existing_chapter or match.group(1)
            existing_start = existing_start or match.group(2)
            existing_end = existing_end or match.group(3) or match.group(2)
        try:
            existing_chapter = int(existing_chapter)
            existing_start = int(existing_start)
            existing_end = int(existing_end if existing_end is not None else existing_start)
        except (TypeError, ValueError):
            continue
        if (existing_chapter == chapter_id and
                existing_start <= verse_end and existing_end >= verse_start):
            return True
    return False


def verse_entry_for_position(catalog, jobs, position, performance_history=None):
    print('Loading Quran Foundation reciter and chapter metadata...', flush=True)
    english = get_json(urljoin(QURAN_API, 'resources/recitations'), {'language': 'en'}).get('recitations', [])
    arabic = get_json(urljoin(QURAN_API, 'resources/recitations'), {'language': 'ar'}).get('recitations', [])
    chapters = get_json(urljoin(QURAN_API, 'chapters'), {'language': 'en'}).get('chapters', [])
    allowed = set(catalog['allowed_reciter_ids'])
    blocked = set(catalog.get('blocked_reciter_ids', []))
    english = [row for row in english if row.get('id') in allowed and row.get('id') not in blocked]
    if not english or not chapters:
        raise RuntimeError('Quran Foundation catalog is temporarily empty')
    arabic_names = {row['id']: row.get('translated_name', {}).get('name') for row in arabic}
    total_verses = sum(int(row['verses_count']) for row in chapters)
    if total_verses < 6000:
        raise RuntimeError('Quran Foundation chapter metadata is incomplete')
    try:
        max_candidates = max(30, int(os.environ.get('QURAN_MAX_CANDIDATES', '1200')))
    except (TypeError, ValueError):
        max_candidates = 1200
    try:
        search_timeout = max(60.0, float(os.environ.get('QURAN_SEARCH_TIMEOUT_SECONDS', '900')))
    except (TypeError, ValueError):
        search_timeout = 900.0
    candidate_limit = min(total_verses * len(english), max_candidates)
    deadline = time.monotonic() + search_timeout
    ordered_reciters = _reciter_order(
        english, jobs, position, catalog, performance_history)
    ordered_themes = _visual_theme_order(
        catalog['visual_themes'], jobs, position, catalog, performance_history)
    min_duration = max(30.0, float(catalog.get('min_audio_seconds', 30)))
    max_duration = float(catalog['max_audio_seconds'])
    max_ayah_characters = int(catalog['max_ayah_characters'])
    try:
        max_passage_verses = min(24, max(1, int(catalog.get('max_passage_verses', 16))))
    except (TypeError, ValueError):
        max_passage_verses = 16
    rejected = {
        'already_published': 0,
        'audio_unavailable': 0,
        'duration_out_of_range': 0,
        'arabic_text_unavailable_or_too_long': 0,
    }

    def fetch_audio(reciter_id, chapter_id, verse_number):
        expected_key = f'{chapter_id}:{verse_number}'
        payload = get_json(
            urljoin(QURAN_API, f'recitations/{reciter_id}/by_ayah/{expected_key}'),
            {'fields': 'chapter_id,verse_number,verse_key,duration,url,segments'})
        files = payload.get('audio_files', [])
        if len(files) != 1:
            return None
        audio = files[0]
        if audio.get('verse_key', expected_key) != expected_key:
            raise ValueError('Audio verse does not match the requested verse')
        try:
            duration = float(audio.get('duration') or 0)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(duration) or duration <= 0:
            return None
        try:
            audio_url = quran_audio_url(audio.get('url', ''))
        except ValueError:
            return None
        return {'verse_key': expected_key, 'verse_number': verse_number,
                'duration': duration, 'audio_url': audio_url,
                'segments': audio.get('segments')}

    def fetch_arabic_text(audio_parts):
        arabic_texts = []
        for part in audio_parts:
            expected_key = part['verse_key']
            text_payload = get_json(
                urljoin(QURAN_API, 'quran/verses/uthmani'),
                {'verse_key': expected_key})
            text_rows = text_payload.get('verses', [])
            if len(text_rows) != 1:
                return None
            if text_rows[0].get('verse_key', expected_key) != expected_key:
                raise ValueError('Quran text does not match the requested verse')
            arabic_text = str(text_rows[0].get('text_uthmani') or '').strip()
            if not arabic_text:
                return None
            arabic_number = str(part['verse_number']).translate(
                str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩'))
            arabic_texts.append(arabic_text if len(audio_parts) == 1 else
                                f'{arabic_text} ۝{arabic_number}')
        return ' '.join(arabic_texts)

    for attempt in range(candidate_limit):
        if time.monotonic() >= deadline:
            raise NoEligibleVerse(position, attempt, timed_out=True, rejected=rejected)
        if attempt == 0 or (attempt + 1) % 100 == 0:
            print(f'Checking complete-passage candidate {attempt + 1}/{candidate_limit}...', flush=True)
        reciter = ordered_reciters[attempt % len(ordered_reciters)]
        reciter_index = english.index(reciter)
        batch = position // len(english)
        verse_index = (batch + reciter_index * 521) % total_verses
        remaining = verse_index
        chapter = None
        for row in chapters:
            count = int(row['verses_count'])
            if remaining < count:
                chapter = row
                verse_number = remaining + 1
                break
            remaining -= count
        reciter_id = reciter['id']
        chapter_id = int(chapter['id'])
        verse_count = int(chapter['verses_count'])
        candidate_position = position
        position += 1

        first_audio = fetch_audio(reciter_id, chapter_id, verse_number)
        if first_audio is None:
            rejected['audio_unavailable'] += 1
            continue
        audio_parts = [first_audio]
        total_duration = first_audio['duration']
        if total_duration > max_duration:
            rejected['duration_out_of_range'] += 1
            continue

        # Some QF recordings are short per ayah. Combine only consecutive complete
        # ayahs from this same reciter and Surah until the whole passage reaches 30s.
        while total_duration < min_duration and len(audio_parts) < max_passage_verses:
            next_verse = verse_number + len(audio_parts)
            if next_verse > verse_count:
                break
            next_audio = fetch_audio(reciter_id, chapter_id, next_verse)
            if next_audio is None:
                break
            if total_duration + next_audio['duration'] > max_duration:
                break
            audio_parts.append(next_audio)
            total_duration += next_audio['duration']
        if total_duration < min_duration or total_duration > max_duration:
            rejected['duration_out_of_range'] += 1
            continue

        verse_end = audio_parts[-1]['verse_number']
        if passage_overlaps_jobs(jobs, chapter_id, verse_number, verse_end):
            rejected['already_published'] += 1
            continue
        ayah_text = fetch_arabic_text(audio_parts)
        if not ayah_text:
            rejected['arabic_text_unavailable_or_too_long'] += 1
            continue
        if len(ayah_text) > max_ayah_characters:
            rejected['arabic_text_unavailable_or_too_long'] += 1
            continue

        style = reciter.get('style') or ''
        reciter_ar = arabic_names.get(reciter_id) or reciter.get('reciter_name')
        passage_id = (f'qf-v-r{reciter_id}-a{chapter_id}-{verse_number}' if verse_number == verse_end else
                      f'qf-v-r{reciter_id}-a{chapter_id}-{verse_number}-to-{verse_end}')
        verse_key = (f'{chapter_id}:{verse_number}' if verse_number == verse_end else
                     f'{chapter_id}:{verse_number}-{verse_end}')
        return ({
            'id': passage_id, 'source_type': 'quran_passage' if len(audio_parts) > 1 else 'quran_verse',
            'recitation_id': reciter_id,
            'audio_url': audio_parts[0]['audio_url'],
            'audio_urls': [part['audio_url'] for part in audio_parts],
            'surah_ar': chapter['name_arabic'], 'surah_en': chapter['name_simple'],
            'chapter_id': chapter_id, 'verse_number': verse_number, 'verse_start': verse_number, 'verse_end': verse_end,
            'verse_key': verse_key,
            'ayah_text': ayah_text,
            'word_segments': audio_parts[0]['segments'] if len(audio_parts) == 1 else None,
            'reciter_ar': reciter_ar, 'reciter_en': reciter.get('reciter_name', ''), 'style': style,
            'permission_url': catalog['permission_url'], 'attribution': catalog['attribution'],
            'rights': catalog['rights'], 'verified': True, 'whole_recording': True,
            'duration': total_duration,
            'min_audio_seconds': min_duration,
            'max_audio_seconds': max_duration,
            'tail_silence_seconds': float(catalog['tail_silence_seconds']),
            'visual_style': catalog['visual_style'],
            'visual_theme': ordered_themes[candidate_position % len(ordered_themes)],
        }, position)
    raise NoEligibleVerse(position, candidate_limit, rejected=rejected)
def load_catalog(path):
    data = bot.read_json(path)
    if not isinstance(data, dict):
        raise ValueError('Invalid recording catalog')
    if data.get('schema') == 3:
        return validate_verse_catalog(data)
    if data.get('schema') == 2:
        return api_entries(data)
    if data.get('schema') != 1 or not isinstance(data.get('recordings'), list):
        raise ValueError('Invalid recording catalog')
    seen = set()
    for entry in data['recordings']:
        if not isinstance(entry, dict):
            raise ValueError('Recording must be an object')
        key = entry.get('id', '')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', key) or key in seen:
            raise ValueError('Recording IDs must be safe and unique')
        seen.add(key)
        if entry.get('verified') is not True or entry.get('whole_recording') is not True:
            raise ValueError(f'{key}: complete recording and source verification required')
        for field in ('audio_url', 'permission_url'):
            if not str(entry.get(field, '')).startswith('https://'):
                raise ValueError(f'{key}: {field} must use HTTPS')
        for field in ('surah_ar', 'surah_en', 'reciter_ar', 'attribution', 'rights'):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f'{key}: {field} required')
        if not re.fullmatch(r'[a-f0-9]{64}', entry.get('sha256', '')):
            raise ValueError(f'{key}: verified audio checksum required')
        bot.positive(entry.get('duration'), 'duration', 60)
    return data['recordings']


def download_recording(entry, destination):
    if entry.get('source_type') == 'quran_passage':
        return download_quran_passage(entry, destination)
    if entry.get('source_type') == 'quran_verse':
        return download_quran_verse(entry, destination)
    if entry.get('source_type') == 'quran_foundation':
        return download_quran_foundation(entry, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and bot.file_hash(destination) == entry['sha256']:
        return destination
    temp = destination.with_suffix('.part')
    for attempt in range(3):
        try:
            with requests.get(entry['audio_url'], stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                total = 0
                with temp.open('wb') as handle:
                    for block in response.iter_content(65536):
                        total += len(block)
                        if total > 50 * 1024 * 1024:
                            raise ValueError('Recording exceeds the 50 MB limit')
                        handle.write(block)
            break
        except requests.RequestException:
            if attempt == 2:
                raise RuntimeError('Approved audio source unavailable; no upload was attempted') from None
            time.sleep(2 ** attempt)
    if bot.file_hash(temp) != entry['sha256']:
        raise ValueError('Audio changed at its source; stopping until the recording is verified again')
    os.replace(temp, destination)
    return destination


CUSTOM_CLIP_URL = re.compile(r'^https://[^ ]+\.(?:mp4|mov|webm)(?:\?.*)?$', re.IGNORECASE)
MAX_CUSTOM_VIDEO_BYTES = 60 * 1024 * 1024


def delete_custom_blob(record):
    """Delete a dashboard-uploaded Blob after the YouTube upload is durable."""
    if not isinstance(record, dict) or not record.get('blob_url'):
        return False
    base = os.environ.get('DASHBOARD_URL', '').rstrip('/')
    key = os.environ.get('DASHBOARD_KEY', '')
    if not base or not key:
        print('Custom Blob cleanup deferred: DASHBOARD_URL or DASHBOARD_KEY is not configured')
        return False
    endpoint = base + '/api/blob-delete'
    for attempt in range(3):
        try:
            response = requests.post(endpoint, headers={'X-Dashboard-Key': key},
                                     json={'url': record['blob_url']}, timeout=30)
            if response.status_code == 200:
                print('Custom Blob deleted after successful publish')
                return True
            print(f'Custom Blob cleanup attempt {attempt + 1} failed (HTTP {response.status_code})')
        except requests.RequestException:
            print(f'Custom Blob cleanup attempt {attempt + 1} failed (network)')
        if attempt < 2:
            time.sleep(2 ** attempt)
    return False


def download_custom_video(record, destination):
    url = str(record.get('url') or '')
    if not CUSTOM_CLIP_URL.fullmatch(url):
        raise CloudError('Custom video URL must be a direct HTTPS MP4, MOV, or WebM link')
    if record.get('license_confirmed') is not True:
        raise CloudError('Custom video licence confirmation is required')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.part')
    total = 0
    try:
        with requests.get(url, stream=True, timeout=(15, 120),
                          headers={'User-Agent': 'quran-shorts-bot/4.0'}) as response:
            response.raise_for_status()
            declared = int(response.headers.get('content-length', '0') or 0)
            if declared > MAX_CUSTOM_VIDEO_BYTES:
                raise CloudError('Custom video exceeds the 60 MB limit')
            with temporary.open('wb') as handle:
                for block in response.iter_content(1024 * 1024):
                    total += len(block)
                    if total > MAX_CUSTOM_VIDEO_BYTES:
                        raise CloudError('Custom video exceeds the 60 MB limit')
                    handle.write(block)
    except requests.RequestException:
        raise CloudError('Custom video source is temporarily unavailable') from None
    if total < 1024:
        raise CloudError('Custom video is empty')
    duration = media_duration(temporary)
    if duration < 1:
        raise CloudError('Custom video has no moving video track')
    os.replace(temporary, destination)
    return destination


def media_duration(path):
    result = subprocess.run([bot.ffmpeg(), '-hide_banner', '-nostdin', '-i', str(path),
                             '-f', 'null', '-'], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=180)
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    if result.returncode or not match:
        raise RuntimeError('Cannot measure the complete audio recording')
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def download_quran_passage(entry, destination):
    """Download consecutive complete ayahs and join them in one final audio encode."""
    audio_urls = entry.get('audio_urls')
    if not isinstance(audio_urls, list) or len(audio_urls) < 2 or len(audio_urls) > 24:
        raise ValueError('A Quran passage must contain between 2 and 24 verified audio parts')
    destination.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    total_bytes = 0
    for index, audio_url in enumerate(audio_urls, 1):
        raw = destination.with_name(f'recitation-part-{index:02}.mp3')
        response = requests.get(
            audio_url, stream=True, timeout=(15, 60),
            headers={'User-Agent': 'quran-shorts-bot/4.0'})
        if response.status_code >= 400:
            response.close()
            raise RuntimeError(f'Verse audio source returned HTTP {response.status_code}')
        part_bytes = 0
        with response:
            with raw.open('wb') as handle:
                for block in response.iter_content(65536):
                    part_bytes += len(block)
                    total_bytes += len(block)
                    if part_bytes > 20 * 1024 * 1024 or total_bytes > 50 * 1024 * 1024:
                        raise ValueError('Quran passage audio exceeds the size limit')
                    handle.write(block)
        parts.append(raw)

    measured_parts = [media_duration(part) for part in parts]
    source_duration = sum(measured_parts)
    minimum = max(30.0, float(entry.get('min_audio_seconds', 30)))
    maximum = float(entry['max_audio_seconds'])
    if source_duration < minimum:
        raise TooShortRecording('Complete Quran passage is shorter than 30 seconds')
    if source_duration > maximum:
        raise TooLongRecording('Complete Quran passage is too long for a Short')

    inputs = []
    for part in parts:
        inputs.extend(['-i', str(part)])
    stream_labels = ''.join(f'[{index}:a]' for index in range(len(parts)))
    filters = (stream_labels + f'concat=n={len(parts)}:v=0:a=1[joined];'
               f'[joined]apad=pad_dur={float(entry["tail_silence_seconds"])}[out]')
    temporary = destination.with_suffix('.part.mp3')
    bot.run_media([
        '-y', *inputs, '-filter_complex', filters, '-map', '[out]',
        '-c:a', 'libmp3lame', '-b:a', '192k', str(temporary)])
    padded = media_duration(temporary)
    if padded > 60:
        raise TooLongRecording('Quran passage with its ending exceeds the Shorts limit')
    os.replace(temporary, destination)
    entry['duration'] = round(padded, 2)
    entry['sha256'] = bot.file_hash(destination)
    return destination


def download_quran_verse(entry, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = destination.with_name('recitation-source.mp3')
    response = requests.get(entry['audio_url'], stream=True, timeout=(15, 60),
                            headers={'User-Agent': 'quran-shorts-bot/3.0'})
    if response.status_code >= 400:
        # An HTML error page saved as .mp3 would fail downstream with a confusing
        # decode error; stop here with the real cause instead.
        response.close()
        raise RuntimeError(f'Verse audio source returned HTTP {response.status_code}')
    with response:
        total = 0
        with raw.open('wb') as handle:
            for block in response.iter_content(65536):
                total += len(block)
                if total > 20 * 1024 * 1024:
                    raise ValueError('Verse recording exceeds the size limit')
                handle.write(block)
    actual = media_duration(raw)
    if actual < max(30, float(entry.get('min_audio_seconds', 30))):
        raise TooShortRecording('Complete recitation is shorter than 30 seconds; selecting another verse')
    if actual > entry['max_audio_seconds']:
        raise TooLongRecording('Complete verse is too long for a Short')
    temporary = destination.with_suffix('.part.mp3')
    tail = entry['tail_silence_seconds']
    bot.run_media(['-y', '-i', str(raw), '-af', f'apad=pad_dur={tail}',
                   '-c:a', 'libmp3lame', '-b:a', '192k', str(temporary)])
    padded = media_duration(temporary)
    if padded > 60:
        raise TooLongRecording('Complete verse with its ending is too long for a Short')
    os.replace(temporary, destination)
    entry['duration'] = round(padded, 2)
    entry['sha256'] = bot.file_hash(destination)
    return destination


def quran_audio_url(relative_url):
    """Resolve Quran Foundation's documented relative or absolute audio URL."""
    if not isinstance(relative_url, str) or any(ord(char) < 32 for char in relative_url):
        raise ValueError('Quran Foundation returned an invalid audio path')
    value = relative_url.strip()
    if not value or '\\' in value:
        raise ValueError('Quran Foundation returned an invalid audio path')
    # Content API responses use relative paths, but deployments can return a
    # fully-qualified or network-path URL from an approved Quran audio CDN.
    if value.startswith('//'):
        value = 'https:' + value
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        host = (parsed.hostname or '').lower()
        approved = (host == 'verses.quran.foundation' or
                    host.endswith('.quran.foundation') or
                    host.endswith('.qurancdn.com') or
                    host.endswith('.quranicaudio.com'))
        if (parsed.scheme.lower() != 'https' or not approved or parsed.username or
                parsed.password or parsed.port or not parsed.path or '..' in parsed.path):
            raise ValueError('Quran Foundation returned an invalid audio path')
        return value
    if value.startswith('/') or not value or '..' in value:
        raise ValueError('Quran Foundation returned an invalid audio path')
    return urljoin(QURAN_AUDIO, value)

def download_quran_foundation(entry, destination):
    payload = get_json(urljoin(QURAN_API, f"recitations/{entry['recitation_id']}/by_chapter/{entry['chapter']}"),
                       {'per_page': 50, 'fields': 'chapter_id,verse_number,verse_key,duration,url'})
    audio_files = payload.get('audio_files', [])
    if not audio_files:
        raise RuntimeError('The selected recitation has no audio files')
    duration = sum(float(row.get('duration') or 0) for row in audio_files)
    if duration <= 0 or duration > 60:
        raise ValueError('The selected recitation is not a valid Short')
    destination.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    for index, row in enumerate(audio_files, 1):
        relative_url = row.get('url', '')
        audio_url = quran_audio_url(relative_url)
        part = destination.parent / f'verse-{index:03}.mp3'
        with requests.get(audio_url, stream=True, timeout=(15, 60),
                          headers={'User-Agent': 'quran-shorts-bot/2.0'}) as response:
            response.raise_for_status()
            total = 0
            with part.open('wb') as handle:
                for block in response.iter_content(65536):
                    total += len(block)
                    if total > 20 * 1024 * 1024:
                        raise ValueError('Verse recording exceeds the size limit')
                    handle.write(block)
        parts.append(part)
    command = []
    for part in parts:
        command.extend(['-i', str(part)])
    filters = ''.join(f'[{index}:a]' for index in range(len(parts))) + f'concat=n={len(parts)}:v=0:a=1[out]'
    temporary = destination.with_suffix('.part.mp3')
    bot.run_media(['-y', *command, '-filter_complex', filters, '-map', '[out]',
                   '-c:a', 'libmp3lame', '-b:a', '192k', str(temporary)])
    os.replace(temporary, destination)
    measured = media_duration(destination)
    if measured > 60:
        raise TooLongRecording('Complete recording is too long for a Short')
    entry['duration'] = measured
    entry['sha256'] = bot.file_hash(destination)
    return destination


def arabic_font_path():
    """Return the configured font, falling back to the source tree in isolated tests."""
    candidate = ROOT / 'assets' / 'Amiri-Regular.ttf'
    if candidate.is_file():
        return candidate
    return Path(__file__).resolve().parent / 'assets' / 'Amiri-Regular.ttf'


def make_card(entry, destination):
    """Render a compact Arabic-only ayah card over a transparent 9:16 overlay."""
    from PIL import Image, ImageDraw, ImageFilter
    from arabic_text import ArabicText

    font = arabic_font_path()
    if not font.is_file():
        raise FileNotFoundError('Arabic font missing: assets/Amiri-Regular.ttf')
    image = Image.new('RGBA', (1080, 1920), (0, 0, 0, 0))
    typography = ArabicText(font)

    surah = 'سورة ' + str(entry['surah_ar']).strip()
    caption = verse_label(entry, arabic=True)
    verse = str(entry.get('ayah_text') or '').strip()
    reciter = str(entry.get('reciter_ar') or '').strip()
    if not verse:
        raise ValueError('Verified Arabic ayah text is required for the fixed card')

    def fit_text(text, size, width, minimum):
        mask = typography.mask(text, size)
        while mask.width > width and size > minimum:
            size -= 2
            mask = typography.mask(text, size)
        if mask.width > width:
            raise ValueError('Arabic label does not fit inside the compact ayah card')
        return size, mask.height

    surah_size, surah_height = fit_text(surah, 54, 850, 32)
    caption_size, caption_height = fit_text(caption, 36, 840, 24) if caption else (0, 0)
    reciter_size, reciter_height = fit_text(reciter, 32, 820, 22) if reciter else (0, 0)

    verse_size = 45
    verse_width = 840
    verse_gap = 8
    max_verse_height = 360
    lines = typography.wrap(verse, verse_size, verse_width)
    line_heights = [typography.mask(line, verse_size).height for line in lines]
    verse_height = sum(line_heights) + max(0, len(lines) - 1) * verse_gap
    while verse_size > 27 and (not lines or verse_height > max_verse_height):
        verse_size -= 2
        lines = typography.wrap(verse, verse_size, verse_width)
        line_heights = [typography.mask(line, verse_size).height for line in lines]
        verse_height = sum(line_heights) + max(0, len(lines) - 1) * verse_gap
    if not lines or verse_height > max_verse_height:
        raise ValueError('Complete Arabic ayah is too long for the compact card')

    top_pad, bottom_pad = 30, 30
    caption_block = 12 + caption_height if caption else 0
    reciter_block = 20 + reciter_height + bottom_pad if reciter else 20 + bottom_pad
    panel_height = max(
        390,
        top_pad + surah_height + caption_block + 20 + 2 + 18 +
        verse_height + 20 + 2 + reciter_block
    )
    panel_left, panel_right = 70, 1010
    panel_top = (image.height - panel_height) // 2
    panel_bottom = panel_top + panel_height

    draw = ImageDraw.Draw(image, 'RGBA')
    draw.rounded_rectangle(
        (panel_left, panel_top, panel_right, panel_bottom), radius=42,
        fill=(4, 18, 27, 196), outline=(220, 198, 145, 205), width=2
    )
    draw.rounded_rectangle(
        (panel_left + 16, panel_top + 16, panel_right - 16, panel_bottom - 16),
        radius=32, outline=(226, 215, 179, 45), width=1
    )

    def draw_centered(text, y, size, color, width=840):
        mask = typography.mask(text, size)
        while mask.width > width and size > 20:
            size -= 2
            mask = typography.mask(text, size)
        if (mask.width > width or y < panel_top + 18 or
                y + mask.height > panel_bottom - 18):
            raise ValueError('Arabic text does not fit inside the compact ayah card')
        x = (image.width - mask.width) // 2
        glow = Image.new('RGBA', image.size, (0, 0, 0, 0))
        glow.paste((240, 218, 168, 62), (x, y), mask)
        image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(12)))
        shadow = Image.new('RGBA', image.size, (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 190), (x, y + 4), mask)
        image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)))
        image.paste(color, (x, y), mask)
        return mask.height

    cursor = panel_top + top_pad
    draw_centered(surah, cursor, surah_size, '#f4f1e8', 850)
    cursor += surah_height
    if caption:
        cursor += 12
        draw_centered(caption, cursor, caption_size, '#e7e9e4', 840)
        cursor += caption_height + 18
    else:
        cursor += 18
    draw.line((390, cursor, 690, cursor), fill=(220, 197, 142, 190), width=2)
    cursor += 20
    for index, line in enumerate(lines):
        draw_centered(line, cursor, verse_size, '#fffdf5', verse_width)
        cursor += line_heights[index]
        if index < len(lines) - 1:
            cursor += verse_gap
    cursor += 20
    if reciter:
        draw.line((430, cursor, 650, cursor), fill=(220, 197, 142, 150), width=2)
        cursor += 20
        draw_centered(reciter, cursor, reciter_size, '#dcc58e', 820)

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, 'PNG')
    return destination

def normalize_word_segments(segments, word_count, duration):
    """Validate Quran Foundation's [word index, start ms, end ms] timings."""
    if not isinstance(segments, list) or len(segments) != word_count or word_count < 1:
        return []
    parsed = []
    for row in segments:
        if isinstance(row, dict):
            index = row.get('word_index', row.get('index'))
            start = row.get('start_ms', row.get('start'))
            end = row.get('end_ms', row.get('end'))
        elif isinstance(row, (list, tuple)) and len(row) >= 3:
            index, start, end = row[:3]
        else:
            return []
        try:
            index, start, end = int(index), float(start), float(end)
        except (TypeError, ValueError, OverflowError):
            return []
        if not math.isfinite(start) or not math.isfinite(end):
            return []
        parsed.append((index, start / 1000.0, end / 1000.0))
    first_index = parsed[0][0]
    if first_index not in (0, 1):
        return []
    maximum = float(duration)
    previous_start = -1.0
    previous_end = 0.0
    normalized = []
    for offset, (index, start, end) in enumerate(parsed):
        if index != first_index + offset:
            return []
        if start < 0 or end <= start or end > maximum + 0.75:
            return []
        if start < previous_start or end < previous_end:
            return []
        normalized.append((start, end))
        previous_start, previous_end = start, end
    # Some records contain monotonic but collapsed word starts while the
    # final word is stretched to the full audio duration. Validate the spread
    # of starts as well as the end boundary so these cannot reveal the whole
    # ayah in the first few milliseconds.
    minimum_start_span = max(0.1, min(2.0, maximum * 0.1, word_count * 0.2))
    if normalized[-1][0] - normalized[0][0] < minimum_start_span:
        return []
    return normalized


def make_word_reveal_layers(entry, destination):
    """Build Arabic phrase reveals using provider timings when valid."""
    from PIL import Image, ImageFilter
    from arabic_text import ArabicText
    font = arabic_font_path()
    if not font.is_file():
        raise FileNotFoundError('Arabic font missing: assets/Amiri-Regular.ttf')
    words = str(entry.get('ayah_text') or '').split()
    if not words:
        return []
    duration = float(entry.get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Word reveal needs the measured audio duration')
    maximum_layers = min(8, len(words))
    groups = []
    for bucket in range(maximum_layers):
        start_index = round(bucket * len(words) / maximum_layers)
        end_index = round((bucket + 1) * len(words) / maximum_layers)
        groups.append((start_index, end_index))
    segment_times = normalize_word_segments(
        entry.get('word_segments'), len(words), duration)
    if segment_times:
        timing_mode = 'quran_foundation_word_segments'
        intervals = []
        for index, (start_index, end_index) in enumerate(groups):
            start = segment_times[start_index][0]
            end = (segment_times[groups[index + 1][0]][0]
                   if index + 1 < len(groups) else duration)
            if end <= start:
                segment_times = []
                break
            intervals.append((start, end))
    if not segment_times:
        timing_mode = 'proportional_word_length_estimate'
        intro = min(3.5, max(1.5, duration * 0.18))
        finish = max(intro + 1.0, duration - 0.65)
        usable = max(0.5, finish - intro)
        weights = [
            max(1, len(''.join(ch for ch in ' '.join(words[a:b]) if ch.isalnum())))
            for a, b in groups
        ]
        total_weight = float(sum(weights)) or 1.0
        intervals = []
        elapsed = intro
        for index, weight in enumerate(weights):
            end = finish if index == len(weights) - 1 else elapsed + usable * weight / total_weight
            intervals.append((elapsed, end))
            elapsed = end
        entry['word_reveal_intro_seconds'] = round(intro, 3)
    else:
        entry['word_reveal_intro_seconds'] = 2.6
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    typography = ArabicText(font)
    result = []
    cumulative = []
    for group_index, ((start_index, end_index), (start, end)) in enumerate(zip(groups, intervals), 1):
        cumulative = words[:end_index]
        text = ' '.join(cumulative)
        image = Image.new('RGBA', (1080, 1920), (0, 0, 0, 0))
        lines = typography.wrap(text, 54, 820)
        step = 72
        first_y = 980 - (len(lines) - 1) * step / 2
        glow = Image.new('RGBA', image.size, (0, 0, 0, 0))
        for line_index, line in enumerate(lines):
            typography.draw_centered(glow, line, int(first_y + line_index * step), 54, '#fff4c2')
        image = Image.alpha_composite(image, glow.filter(ImageFilter.GaussianBlur(13)))
        for line_index, line in enumerate(lines):
            typography.draw_centered(image, line, int(first_y + line_index * step), 54, '#fffdf4')
        path = destination / ('phrase-%03d.png' % group_index)
        image.save(path, 'PNG')
        result.append({'path': str(path), 'start': round(start, 3), 'end': round(end, 3)})
    entry['word_timing_mode'] = timing_mode
    return result


def make_motion_overlay(entry, destination):
    """Create a tall transparent atmosphere sheet that visibly moves."""
    from PIL import Image, ImageDraw, ImageFilter
    import random
    seed = int(hashlib.sha256((entry['id'] + ':moving-rain').encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    rain = Image.new('RGBA', (1080, 3840), (0, 0, 0, 0))
    draw = ImageDraw.Draw(rain, 'RGBA')
    theme = entry.get('visual_theme', 'forest_rain')
    count = {'forest_rain': 650, 'mist_mountains': 180, 'starry_night': 150,
             'ocean_moon': 260, 'dawn_mosque': 130}.get(theme, 300)
    for _ in range(count):
        x, y = rng.randrange(1100), rng.randrange(3840)
        if theme == 'starry_night':
            radius = rng.choice((2, 3, 4, 6))
            draw.ellipse((x-radius, y-radius, x+radius, y+radius),
                         fill=(235, 231, 196, rng.randrange(55, 145)))
        else:
            length = rng.randrange(28, 105 if theme == 'forest_rain' else 68)
            width = 1 if length < 70 else 2
            draw.line((x, y, x-13, y+length),
                      fill=(198, 225, 225, rng.randrange(30, 115)), width=width)
    # Two soft translucent fog bands move with the rain sheet at a slower visual pace.
    fog = Image.new('RGBA', rain.size, (0, 0, 0, 0))
    fog_draw = ImageDraw.Draw(fog, 'RGBA')
    fog_draw.ellipse((-400, 900, 1480, 1370), fill=(190, 210, 204, 24))
    fog_draw.ellipse((-650, 2700, 1250, 3260), fill=(190, 210, 204, 20))
    fog = fog.filter(ImageFilter.GaussianBlur(45))
    rain = Image.alpha_composite(rain, fog)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rain.save(destination)
    return destination


def verse_span(entry):
    """Return the numeric ayah span, including legacy range keys."""
    start = entry.get('verse_start', entry.get('verse_number'))
    end = entry.get('verse_end')
    match = re.search(r':(\d+)(?:-(\d+))?$', str(entry.get('verse_key') or ''))
    if match:
        if start is None:
            start = match.group(1)
        if end is None:
            end = match.group(2) or match.group(1)
    if start is None:
        return None
    try:
        start, end = int(start), int(end if end is not None else start)
    except (TypeError, ValueError):
        return None
    if start <= 0 or end < start:
        return None
    return start, end


def verse_range_label(entry):
    span = verse_span(entry)
    if not span:
        return None
    start, end = span
    return str(start) if start == end else f'{start}\u2013{end}'


def verse_label(entry, arabic=False):
    number = verse_range_label(entry)
    if not number:
        return None
    start, end = verse_span(entry)
    if arabic:
        number = number.translate(str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩'))
        return ('الآية ' if start == end else 'الآيات ') + number
    return ('Ayah ' if start == end else 'Ayahs ') + number


def video_background_for(entry):
    """Return the primary filmed clip only when its licence is reviewed."""
    theme = entry.get('visual_theme', 'forest_rain')
    if not isinstance(theme, str) or not re.fullmatch(r'[a-z0-9_]{3,80}', theme):
        raise ValueError(f'Unknown filmed background theme: {theme}')
    path = ROOT / 'assets' / 'backgrounds' / 'video' / f'{theme}.mp4'
    reviewed = bot.theme_clips(theme, ROOT)
    if reviewed:
        return reviewed[0]
    # Test fixtures and legacy hand-built jobs may intentionally omit the
    # production visual style and licence manifest. Production entries fail
    # closed instead of silently substituting an unreviewed clip.
    if path.is_file() and entry.get('visual_style') != 'real_video_assets':
        return path
    if entry.get('visual_style') == 'real_video_assets':
        raise FileNotFoundError(f'Reviewed filmed background missing or not listed: {path}')
    return None


def _background_identity(path):
    stem = Path(path).stem.casefold()
    parts = stem.split('_')
    if len(parts) == 3 and parts[0] == 'cinematic' and parts[1] in {
            'coast', 'forest', 'mist', 'mountain', 'rain', 'river'} and parts[2].isdigit():
        kind = parts[1]
        family = {'coast': 'water', 'river': 'water', 'mountain': 'mountain',
                  'forest': 'woodland', 'mist': 'woodland', 'rain': 'woodland'}[kind]
        return kind, family
    return stem, stem


def choose_background_playlist(entry_id, primary, used_clips, pool,
                               used_playlists=(), count=3):
    """Prefer unused, mood-matched clips and avoid repeating a full montage."""
    from itertools import permutations

    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError('Background clip count must be a positive integer')

    def canonical(value):
        return str(Path(value).resolve())

    candidates = []
    seen = set()
    for value in [*pool, *([primary] if primary else [])]:
        path = Path(value).resolve()
        key = canonical(path)
        if key not in seen and path.is_file():
            candidates.append(path)
            seen.add(key)
    if not candidates:
        raise FileNotFoundError('No reviewed background clips are available')
    count = min(count, len(candidates))
    primary_key = canonical(primary) if primary else None
    used = {canonical(value) for value in (used_clips or []) if value}
    fresh = {canonical(path) for path in candidates if canonical(path) not in used}
    primary_is_fresh = primary_key in fresh
    primary_identity = _background_identity(primary) if primary else None

    previous_plans = set()
    for previous in used_playlists or []:
        if isinstance(previous, dict):
            previous = previous.get('background_playlist', [])
        if isinstance(previous, (list, tuple)):
            plan = tuple(canonical(value) for value in previous if value)
            if plan:
                previous_plans.add(plan)

    def plan_score(plan):
        fresh_count = sum(canonical(path) in fresh for path in plan)
        identities = [_background_identity(path) for path in plan]
        mood_cost = sum(
            0 if identity[0] == primary_identity[0] else
            1 if identity[1] == primary_identity[1] else 2
            for identity in identities
        ) if primary_identity else 0
        primary_position = next(
            (index for index, path in enumerate(plan) if canonical(path) == primary_key),
            count + 1
        )
        primary_missing = int(primary_is_fresh and primary_position > count)
        order_key = hashlib.sha256(
            (str(entry_id) + ':' + '|'.join(canonical(path) for path in plan)).encode('utf-8')
        ).hexdigest()
        return (-fresh_count, mood_cost, primary_missing, primary_position, order_key)

    non_repeated = [
        plan for plan in permutations(candidates, count)
        if tuple(canonical(path) for path in plan) not in previous_plans
    ]
    if non_repeated:
        best = min(non_repeated, key=plan_score)
    else:
        best = min(permutations(candidates, count), key=plan_score)
    return list(best), any(canonical(path) in used for path in best), (
        tuple(canonical(path) for path in best) in previous_plans
    )


def choose_scene_cuts(duration, scene_count, word_layers, minimum_segment=6.0):
    """Place montage cuts on the nearest phrase boundary without tiny scenes."""
    total = float(duration)
    if not math.isfinite(total) or total <= 0 or scene_count < 1:
        raise ValueError('Scene timing needs a positive duration and scene count')
    if scene_count == 1:
        return []
    if total < scene_count * minimum_segment:
        return [round(total * index / scene_count, 3)
                for index in range(1, scene_count)]
    phrase_ends = set()
    for layer in word_layers or []:
        try:
            end = float(layer.get('end'))
        except (TypeError, ValueError):
            continue
        if math.isfinite(end) and 0 < end < total:
            phrase_ends.add(end)
    boundaries = []
    previous = 0.0
    for index in range(1, scene_count):
        lower = previous + minimum_segment
        upper = total - (scene_count - index) * minimum_segment
        target = total * index / scene_count
        eligible = [value for value in phrase_ends if lower <= value <= upper]
        cut = min(eligible, key=lambda value: (abs(value - target), value)) if eligible else target
        cut = min(max(cut, lower), upper)
        boundaries.append(round(cut, 3))
        previous = cut
    return boundaries


def item_for(entry, source, background, motion_overlay=None, background_video=None, used_clips=None, used_playlists=None, portrait_backgrounds=False, background_clip_count=3, external_backgrounds=None):
    cta_index = int(hashlib.sha256(entry['id'].encode('utf-8')).hexdigest(), 16) % len(bot.CTA_COMMENTS)
    cta = bot.CTA_COMMENTS[cta_index]
    verse_caption = verse_label(entry, arabic=True)
    if verse_caption:
        suffix = " #Shorts"
        prefix = f"سورة {entry['surah_ar']}، {verse_caption} | القارئ "
        reciter = entry['reciter_ar']
        room = max(1, 100 - len(prefix) - len(suffix))
        title = prefix + reciter[:room].rstrip() + suffix
        description = (f"{entry.get('ayah_text', '')}\n\n"
                       f"تلاوة سورة {entry['surah_ar']}، {verse_caption}، بصوت {entry['reciter_ar']}\n\n"
                       f"{entry['attribution']}\n\n{entry['permission_url']}\n\n{cta}")
    else:
        suffix = " #Shorts"
        prefix = f"سورة {entry['surah_ar']} | القارئ "
        room = max(1, 100 - len(prefix) - len(suffix))
        title = prefix + entry['reciter_ar'][:room].rstrip() + suffix
        description = (f"تلاوة سورة {entry['surah_ar']}، بصوت "
                       f"{entry.get('reciter_ar', 'قارئ')}.\n\n"
                       f"{entry['attribution']}\n\n{entry['permission_url']}\n\n{cta}")
    external_backgrounds = [row for row in (external_backgrounds or [])
                            if isinstance(row, dict)]
    if external_backgrounds:
        credits = []
        for row in external_backgrounds:
            creator = ' '.join(str(row.get('creator') or 'Pexels creator').split())[:120]
            page_url = str(row.get('page_url') or 'https://www.pexels.com/')
            creator_url = str(row.get('creator_url') or page_url)
            credits.append(f"خلفية الفيديو: {creator} — {creator_url}; Pexels: {page_url}")
        credits.append("Pexels: https://www.pexels.com/")
        credits.append("الترخيص: Pexels License — https://www.pexels.com/license/")
        description = description.rstrip() + "\n\n" + "\n".join(credits)

    item = {'id': entry['id'], 'mode': 'compose', 'source': str(source.resolve()),
            'background': str(background.resolve()), 'start': 0, 'duration': entry['duration'],
            'min_duration_seconds': 30,
            'background_motion': 'premium_motion',
            'visual_theme': entry.get('visual_theme', 'forest_rain'),
            'title': title, 'description': description, 'metadata_complete': True,
            'surah_ar': entry['surah_ar'], 'surah_en': entry['surah_en'],
            'reciter_ar': entry['reciter_ar'],
            'reciter_en': entry.get('reciter_en', entry['reciter_ar']),
            'cta_description_variant': cta_index,
            'attribution': entry['attribution'], 'rights': entry['rights'],
            'rights_confirmed': True, 'made_for_kids': False}
    span = verse_span(entry)
    if span:
        item['verse_start'], item['verse_end'] = span
    if entry.get('verse_key'):
        item['verse_key'] = entry['verse_key']
    explicit_background = Path(background_video).resolve() if background_video else None
    folder = ROOT / 'assets' / 'backgrounds' / 'video'
    external_paths = [Path(row['path']).resolve() for row in external_backgrounds
                      if row.get('path')]
    if external_backgrounds and explicit_background is None:
        if len(external_paths) < 2 or any(not path.is_file() for path in external_paths):
            raise FileNotFoundError('Pexels backgrounds are incomplete')
        filmed = external_paths[0]
        pool = external_paths
    elif portrait_backgrounds and explicit_background is None:
        pool = reviewed_background_pool(folder, portrait=True)
        if len(pool) < 2:
            raise FileNotFoundError('At least two licensed 9:16 background clips are required')
        preferred = next(
            (clip for clip in pool if clip.stem == entry.get('visual_theme')),
            None
        )
        if preferred is None:
            stable_choice = int(hashlib.sha256(
                str(entry['id']).encode('utf-8')
            ).hexdigest()[:16], 16)
            preferred = pool[stable_choice % len(pool)]
        filmed = preferred
    else:
        filmed = explicit_background if explicit_background else video_background_for(entry)
        pool = sorted(folder.glob('cinematic_*.mp4'))
        if filmed and filmed not in pool:
            pool.insert(0, filmed)
    if filmed and not filmed.is_file():
        raise FileNotFoundError(f'Reviewed filmed background missing: {filmed}')
    if filmed:
        item['background_video'] = str(filmed.resolve())
        item['background_motion'] = 'real_video'
        item['visual_theme'] = (entry.get('visual_theme', filmed.stem)
                                if external_backgrounds and explicit_background is None
                                else filmed.stem)
        if external_backgrounds and explicit_background is None:
            ordered = external_paths
            item['background_orientation'] = 'portrait'
            item['background_sources'] = [
                {key: row.get(key) for key in (
                    'video_id', 'creator', 'creator_url', 'page_url', 'license',
                    'license_url', 'sha256', 'width', 'height', 'duration', 'reused'
                ) if row.get(key) is not None}
                for row in external_backgrounds
            ]
            item['background_clip_cycle_reused'] = any(
                row.get('reused') is True for row in external_backgrounds
            )
            item['background_playlist_reused'] = item['background_clip_cycle_reused']
        else:
            if portrait_backgrounds and explicit_background is None:
                item['background_orientation'] = 'portrait'
            ordered, repeated_clip, repeated_plan = choose_background_playlist(
                entry['id'], filmed, used_clips, pool,
                used_playlists=used_playlists, count=background_clip_count
            )
            if len(ordered) < 2 and portrait_backgrounds and explicit_background is None:
                raise FileNotFoundError('A Short needs at least two distinct portrait clips')
            item['background_clip_cycle_reused'] = repeated_clip
            item['background_playlist_reused'] = repeated_plan
        item['background_playlist'] = [str(clip.resolve()) for clip in ordered]
        item['background_scene_boundaries'] = choose_scene_cuts(
            float(entry['duration']), len(ordered), None
        )
        item.pop('motion_overlay', None)
    if not filmed and entry.get('visual_style') == 'real_video_assets':
        raise ValueError('No reviewed filmed background is available')
    if motion_overlay and not filmed:
        item['motion_overlay'] = str(motion_overlay.resolve())
    return item


def reviewed_background_pool(folder, portrait=False):
    """Return only manifest-listed cinematic clips, optionally native portrait."""
    manifest = Path(folder) / 'LICENSES.md'
    try:
        text = manifest.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        return []
    licensed = set(re.findall(
        r'(?<![A-Za-z0-9_-])\x60([A-Za-z0-9_-]+\.mp4)\x60',
        text, flags=re.IGNORECASE
    ))
    clips = []
    for clip in sorted(Path(folder).glob('cinematic_*.mp4')):
        if clip.name not in licensed:
            continue
        if portrait:
            try:
                bot.validate_background_source(clip, portrait=True)
            except (ValueError, OSError):
                continue
        clips.append(clip.resolve())
    return clips


def best_effort_pexels_backgrounds(api_key, theme, used_ids, count, destination=None):
    """Download and validate stock footage without ever blocking local fallback."""
    if not api_key:
        return [], None
    folder = Path(destination) if destination else Path(
        tempfile.mkdtemp(prefix='quran-shorts-pexels-')
    )
    try:
        sources = background_provider.download_fresh_backgrounds(
            api_key, theme, used_ids, destination=folder, count=count
        )
        for source in sources:
            bot.validate_background_source(Path(source['path']), portrait=True)
        return sources, folder
    except Exception as error:
        shutil.rmtree(folder, ignore_errors=True)
        # Do not log response bodies, URLs, or credentials.
        print('Pexels backgrounds unavailable; using reviewed local footage '
              f'({type(error).__name__}).', flush=True)
        return [], None


def cleanup_pexels_backgrounds(folder):
    if folder:
        shutil.rmtree(folder, ignore_errors=True)


def surah_playlist_title(entry):
    return f"سورة {entry['surah_ar']} | {entry['surah_en']} — Quran Shorts"


def ensure_playlist(youtube, ledger, entry, privacy='public'):
    """Return the surah playlist id, reusing or creating it exactly once."""
    surah_en = str(entry.get('surah_en') or '').strip()
    surah_ar = str(entry.get('surah_ar') or '').strip()
    if not surah_en or not surah_ar:
        raise CloudError('Playlist organization needs verified surah names')
    playlists = ledger.data.setdefault('playlists', {})
    playlist_id = playlists.get(surah_en)
    if playlist_id:
        return playlist_id
    title = surah_playlist_title(entry)
    response = youtube.playlists().list(part='snippet', mine=True, maxResults=50).execute()
    for row in response.get('items', []):
        if row.get('snippet', {}).get('title') == title:
            playlists[surah_en] = row['id']
            return row['id']
    created = youtube.playlists().insert(part='snippet,status', body={
        'snippet': {'title': title,
                    'description': ('Complete-verse Quran Shorts for this surah. '
                                    + str(entry.get('attribution') or '')).strip()},
        'status': {'privacyStatus': privacy}}).execute()
    if not created.get('id'):
        raise CloudError('YouTube did not return a playlist id')
    playlists[surah_en] = created['id']
    return created['id']


def add_video_to_playlist(youtube, ledger, entry, video_id, privacy='public'):
    """Best-effort playlist membership for a published video.

    Playlist operations can fail for many transient reasons; they must never
    invalidate a recorded upload, so every failure is swallowed here.
    """
    try:
        playlist_id = ensure_playlist(youtube, ledger, entry, privacy)
        existing = youtube.playlistItems().list(
            part='id', playlistId=playlist_id, videoId=video_id, maxResults=1).execute()
        if not existing.get('items'):
            youtube.playlistItems().insert(part='snippet', body={
                'snippet': {'playlistId': playlist_id,
                            'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}}).execute()
            print('Added to playlist:', playlist_id)
        try:
            ledger.save()  # Persist the playlist id cache for future runs.
        except Exception:
            print('Playlist cache could not be saved; it will be rebuilt next run')
        return playlist_id
    except Exception as error:
        print('Playlist update skipped:', type(error).__name__)
        return None


def restriction_reason(video):
    status = video.get('status', {})
    details = video.get('contentDetails', {})
    upload_status = status.get('uploadStatus')
    if upload_status in {'rejected', 'failed', 'deleted'}:
        return status.get('rejectionReason') or status.get('failureReason') or upload_status
    regions = details.get('regionRestriction', {})
    if regions.get('blocked'):
        return 'region blocked: ' + ','.join(regions['blocked'][:20])
    if regions.get('allowed'):
        return 'region restricted to: ' + ','.join(regions['allowed'][:20])
    return None


def check_upload_restrictions(service, ledger, catalog):
    """Auto-block reciters when YouTube reports a takedown or region restriction."""
    tracked = [(job_id, row) for job_id, row in ledger.data['jobs'].items()
               if row.get('status') == 'uploaded' and row.get('video_id') and row.get('reciter_id')]
    if not tracked:
        return []
    incidents = []
    for offset in range(0, len(tracked), 50):
        batch = tracked[offset:offset + 50]
        ids = ','.join(row['video_id'] for _, row in batch)
        response = service.videos().list(part='status,contentDetails', id=ids).execute()
        videos = {row['id']: row for row in response.get('items', [])}
        for job_id, row in batch:
            video = videos.get(row['video_id'])
            reason = restriction_reason(video) if video else None
            if not reason or row.get('safety_incident'):
                continue
            reciter_id = int(row['reciter_id'])
            # A user deletion or encoding failure is not evidence against
            # every recording by that reciter.
            block = reason == 'copyright' or reason.startswith('region ')
            if block:
                ledger.block_reciter(catalog, reciter_id)
            incident = {'video_id': row['video_id'], 'reciter_id': reciter_id,
                        'reason': reason, 'reciter_blocked': block,
                        'timestamp': datetime.now(timezone.utc).isoformat()}
            row['safety_incident'] = incident
            incidents.append(incident)
    if incidents:
        ledger.data.setdefault('incidents', []).extend(incidents)
        ledger.save()
        for incident in incidents:
            notifications.notify(
                f"Reciter {incident['reciter_id']} was restricted: {incident['reason']}"
                f"\nhttps://www.youtube.com/watch?v={incident['video_id']}",
                kind="safety", key=f"safety:{incident['video_id']}:{incident['reason']}")
    return incidents


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _latest_view_snapshot(history, video_id, window=10):
    samples = history.get(video_id, [])
    if not isinstance(samples, list):
        return None
    for sample in reversed(samples[-window:]):
        if isinstance(sample, dict) and 'view_count' in sample:
            return sample
    return None


def _performance_group_values(history, jobs, field, window=10, reciter_labels=False):
    """Use one latest view sample per uploaded video, so frequent polling adds no weight."""
    groups = {}
    for row in jobs.values():
        if row.get('status') != 'uploaded' or not row.get('video_id'):
            continue
        video_id = row['video_id']
        sample = _latest_view_snapshot(history, video_id, window)
        if not sample:
            continue
        key = row.get(field)
        if reciter_labels:
            key = row.get('reciter_name') or (
                f"reciter {row['reciter_id']}" if row.get('reciter_id') else 'unknown')
        if key in (None, ''):
            continue
        groups.setdefault(key, {})[video_id] = _count(sample.get('view_count'))
    return {key: list(values.values()) for key, values in groups.items()}


def _performance_preferences(history, jobs, field, minimum_videos=3):
    """Return normalized view preferences only after independent videos provide signal."""
    try:
        minimum_videos = int(minimum_videos)
    except (TypeError, ValueError):
        return {}
    if minimum_videos < 2:
        return {}
    groups = _performance_group_values(history, jobs, field)
    means = {key: sum(values) / len(values) for key, values in groups.items()
             if len(values) >= minimum_videos}
    if len(means) < 2:
        return {}
    low, high = min(means.values()), max(means.values())
    if high <= low:
        return {}
    return {key: (value - low) / (high - low) for key, value in means.items()}


def performance_averages(history, jobs, window=10):
    """Average each video's latest view count, not repeated polling snapshots."""
    groups = {
        'reciters': _performance_group_values(
            history, jobs, 'reciter_id', window, reciter_labels=True),
        'visual_themes': _performance_group_values(
            history, jobs, 'visual_theme', window),
    }
    return {kind: {name: round(sum(values) / len(values), 1)
                   for name, values in sorted(rows.items()) if values}
            for kind, rows in groups.items()}


def performance_sample_counts(history, jobs, window=10):
    groups = {
        'reciters': _performance_group_values(
            history, jobs, 'reciter_id', window, reciter_labels=True),
        'visual_themes': _performance_group_values(
            history, jobs, 'visual_theme', window),
    }
    return {kind: {name: len(values) for name, values in rows.items()}
            for kind, rows in groups.items()}


# Retention analytics from the interactive YouTube Analytics API. Thumbnail
# impressions and CTR exist only in the bulk Reporting API, so watch-through
# percentage is the actionable retention proxy collected here.
ANALYTICS_METRICS = ('views,estimatedMinutesWatched,averageViewDuration,'
                     'averageViewPercentage,likes,comments,shares,subscribersGained')
ANALYTICS_FALLBACK_METRICS = 'views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage'
RETENTION_WINDOW_DAYS = 30
RETENTION_MAX_VIDEOS = 30


def _http_status(error):
    status = getattr(getattr(error, 'resp', None), 'status', None)
    return status if isinstance(status, int) else None


def _parse_analytics_rows(payload):
    if not isinstance(payload, dict):
        return None
    headers = [str(row.get('name') or '') for row in payload.get('columnHeaders', [])]
    rows = payload.get('rows') or []
    if not headers or not rows or not isinstance(rows[0], list) or len(rows[0]) != len(headers):
        return None
    parsed = {}
    for name, value in zip(headers, rows[0]):
        try:
            parsed[name] = float(value)
        except (TypeError, ValueError):
            parsed[name] = 0.0
    return parsed


def collect_retention_metrics(analytics, ledger, now=None, window_days=RETENTION_WINDOW_DAYS,
                              max_videos=RETENTION_MAX_VIDEOS):
    """Best-effort watch-through analytics; never raises and never blocks publishing.

    Updates each recent uploaded job with the latest retention snapshot and
    aggregates the recorded CTA comment variants so the report can finally
    compare them. A missing scope degrades to an explicit "unavailable" reason.
    """
    now = now or datetime.now(timezone.utc)
    result = {'available': analytics is not None, 'queried': 0, 'variants': {}, 'reason': None}
    if analytics is None:
        result['reason'] = 'analytics service not configured'
        return result
    tracked = []
    for row in ledger.data['jobs'].values():
        if row.get('status') != 'uploaded' or not row.get('video_id'):
            continue
        uploaded = row.get('uploaded_at') or row.get('started_at')
        try:
            when = datetime.fromisoformat(str(uploaded).replace('Z', '+00:00'))
        except (AttributeError, TypeError, ValueError):
            continue
        if not when.tzinfo:
            when = when.replace(tzinfo=timezone.utc)
        if now - when <= timedelta(days=window_days):
            tracked.append((when, row))
    tracked.sort(key=lambda pair: pair[0], reverse=True)  # newest uploads first
    metrics = ANALYTICS_METRICS
    for when, row in tracked[:max_videos]:
        video_id = row['video_id']
        query = dict(ids='channel==MINE', startDate=when.date().isoformat(),
                     endDate=now.date().isoformat(), metrics=metrics,
                     filters='video==' + video_id)
        try:
            payload = analytics.reports().query(**query).execute()
        except Exception as error:
            status = _http_status(error)
            if status == 403:
                result['available'] = False
                result['reason'] = ('YouTube refused analytics access; re-authorize the '
                                    'token with the yt-analytics.readonly scope')
                break
            if status == 400 and metrics != ANALYTICS_FALLBACK_METRICS:
                # Some channel configurations reject one optional metric; retry
                # with the compatible core retention set before giving up.
                metrics = ANALYTICS_FALLBACK_METRICS
                try:
                    payload = analytics.reports().query(**{**query, 'metrics': metrics}).execute()
                except Exception:
                    continue
            else:
                continue
        parsed = _parse_analytics_rows(payload)
        if not parsed:
            continue  # A brand-new video has no analytics rows yet.
        row['analytics'] = {'checked_at': now.isoformat(),
                            'views': int(parsed.get('views') or 0),
                            'estimated_minutes_watched': round(parsed.get('estimatedMinutesWatched') or 0.0, 1),
                            'average_view_duration_seconds': round(parsed.get('averageViewDuration') or 0.0, 1),
                            'average_view_percentage': round(parsed.get('averageViewPercentage') or 0.0, 1),
                            'subscribers_gained': int(parsed.get('subscribersGained') or 0)}
        result['queried'] += 1
        variant = row.get('cta_comment_variant')
        if isinstance(variant, bool) or not isinstance(variant, int) or not 0 <= variant < len(bot.CTA_COMMENTS):
            continue
        stats = result['variants'].setdefault(variant, {'videos': 0, 'view_percentage': [], 'views': []})
        stats['videos'] += 1
        stats['view_percentage'].append(row['analytics']['average_view_percentage'])
        stats['views'].append(row['analytics']['views'])
    if result['variants']:
        result['variants'] = {str(variant): {'videos': stats['videos'],
                                             'avg_view_percentage': round(sum(stats['view_percentage']) / len(stats['view_percentage']), 1),
                                             'avg_views': round(sum(stats['views']) / len(stats['views']), 1)}
                              for variant, stats in sorted(result['variants'].items())}
        ledger.data['cta_performance'] = dict(result['variants'], checked_at=now.isoformat())
        try:
            ledger.save()
        except Exception:
            print('Retention analytics could not be saved; the next report retries')
    return result


def collect_performance_metrics(service, ledger, expected_privacy, now=None):
    """Append YouTube statistics and update non-destructive health flags."""
    now = now or datetime.now(timezone.utc)
    jobs = ledger.data['jobs']
    tracked = [(job_id, row) for job_id, row in jobs.items()
               if row.get('status') == 'uploaded' and row.get('video_id')]
    history = ledger.data.setdefault('metrics_history', {})
    previous_flags = ledger.data.setdefault('health_flags', {})
    current_flags = {}
    new_flags = []
    videos = {}
    for offset in range(0, len(tracked), 50):
        ids = ','.join(row['video_id'] for _, row in tracked[offset:offset + 50])
        response = service.videos().list(part='statistics,status', id=ids).execute()
        videos.update({video['id']: video for video in response.get('items', [])})
    timestamp = now.isoformat()
    for job_id, row in tracked:
        video_id = row['video_id']
        video = videos.get(video_id)
        if not video:
            current_flags[video_id] = ['video unavailable']
            if 'video unavailable' not in previous_flags.get(video_id, []):
                new_flags.append({'video_id': video_id, 'flag': 'video unavailable'})
            continue
        statistics = video.get('statistics', {})
        privacy = video.get('status', {}).get('privacyStatus', 'unknown')
        snapshot = {'timestamp': timestamp,
                    'view_count': _count(statistics.get('viewCount')),
                    'like_count': _count(statistics.get('likeCount')),
                    'comment_count': _count(statistics.get('commentCount')),
                    'privacy_status': privacy}
        history.setdefault(video_id, []).append(snapshot)
        history[video_id] = history[video_id][-30:]
        flags = []
        uploaded = row.get('uploaded_at') or row.get('started_at')
        if uploaded:
            try:
                age = now - datetime.fromisoformat(uploaded.replace('Z', '+00:00'))
                if age.total_seconds() > 48 * 3600 and snapshot['view_count'] == 0:
                    flags.append('no traction')
            except (TypeError, ValueError):
                pass
        if privacy != expected_privacy:
            flags.append('visibility problem')
        if flags:
            current_flags[video_id] = flags
            old = set(previous_flags.get(video_id, []))
            for flag in flags:
                if flag not in old:
                    new_flags.append({'video_id': video_id, 'flag': flag})
    ledger.data['health_flags'] = current_flags
    averages = performance_averages(history, jobs)
    sample_counts = performance_sample_counts(history, jobs)
    ledger.data['performance_averages'] = averages
    schedule = schedule_timing_summary(ledger.data.get('schedule_history', []), jobs, now=now)
    ledger.data['schedule_summary'] = schedule
    ledger.data['metrics_checked_at'] = timestamp
    ledger.save()
    return {'tracked': len(tracked), 'new_flags': new_flags, 'averages': averages,
            'sample_counts': sample_counts, 'schedule': schedule, 'timestamp': timestamp}


def performance_summary(report):
    lines = ['## Quran Shorts performance report', '',
             f"Generated: {report['timestamp']}",
             f"Total uploaded videos tracked: {report['tracked']}", '']
    if report['new_flags']:
        lines.append('### Newly flagged videos')
        for row in report['new_flags']:
            lines.append(f"- [{row['flag']}](https://www.youtube.com/watch?v={row['video_id']}): "
                         f"https://www.youtube.com/watch?v={row['video_id']}")
    else:
        lines.extend(['### Newly flagged videos', '- None'])
    sample_counts = report.get('sample_counts') or {}
    lines.extend(['', '### Rolling average views by reciter'])
    reciters = report['averages']['reciters']
    lines.extend([
        f"- {name}: {average:.1f}" +
        (f" views ({sample_counts.get('reciters', {}).get(name)} videos)"
         if sample_counts.get('reciters', {}).get(name) else '')
        for name, average in reciters.items()
    ] or ['- No data yet'])
    lines.extend(['', '### Rolling average views by visual theme'])
    themes = report['averages']['visual_themes']
    lines.extend([
        f"- {name}: {average:.1f}" +
        (f" views ({sample_counts.get('visual_themes', {}).get(name)} videos)"
         if sample_counts.get('visual_themes', {}).get(name) else '')
        for name, average in themes.items()
    ] or ['- No data yet'])
    schedule = report.get('schedule')
    if schedule:
        lines.extend(['', f"### Schedule timing (last {schedule['window_days']} days)",
                      f"- Heartbeat runs observed: {schedule['heartbeat_runs']}",
                      f"- Scheduled uploads observed: {schedule['uploads']}"])
        average = schedule.get('average_offset_minutes')
        maximum = schedule.get('max_offset_minutes')
        lines.append('- Average target-to-upload offset: ' +
                     (f'{average:.1f} minutes' if average is not None else 'No upload data yet'))
        lines.append('- Maximum target-to-upload offset: ' +
                     (f'{maximum:.1f} minutes' if maximum is not None else 'No upload data yet'))
        lines.append(f"- Uploads delayed over 30 minutes: {schedule['delayed_uploads']}")
        if schedule['unresolved']:
            lines.append(f"- Unresolved scheduled attempts: {schedule['unresolved']}")
        overdue = schedule.get('overdue_slots', [])
        lines.append('- Overdue Baghdad publication slots (>45 minutes):')
        if overdue:
            for row in overdue:
                detail = ('upload outcome is uncertain; check YouTube before retrying'
                          if row.get('upload_uncertain') else
                          'the scheduled recovery run can catch up this slot')
                lines.append(f"- {row['slot']}: {row['minutes_overdue']} minutes overdue "
                             f"({detail})")
        else:
            lines.append('- None')
    bias = report.get('selection_bias')
    if bias:
        state = 'enabled' if bias.get('enabled') else 'disabled pending review'
        lines.extend(['', '### Performance preference',
                      f"- Fair, small view-based rotation: {state}",
                      f"- Minimum evidence before a preference applies: "
                      f"{bias.get('minimum_videos', 3)} uploaded videos per option"])
    analytics = report.get('analytics')
    if analytics is not None:
        lines.extend(['', '### Watch-through analytics (uploads of the last 30 days)'])
        if not analytics.get('available'):
            lines.append('- Unavailable: ' + str(analytics.get('reason') or 'unknown'))
        else:
            lines.append(f"- Videos queried: {analytics.get('queried', 0)}")
            variants = analytics.get('variants') or {}
            if variants:
                lines.append('- CTA comment variants (correlation, not proof):')
                for variant, stats in sorted(variants.items(), key=lambda row: int(row[0])):
                    lines.append(f"  - Variant {variant}: {stats['videos']} videos, "
                                 f"avg retention {stats['avg_view_percentage']}%, "
                                 f"avg views {stats['avg_views']}")
            else:
                lines.append('- No CTA variant data yet')
    return '\n'.join(lines) + '\n'


def run_performance_report(service, ledger, analytics=None):
    """Best-effort reporting entry point; monitoring must never fail publishing."""
    try:
        config = bot.read_json(ROOT / 'automation.json')
        ledger.load()
        verify_channel(service, ledger, config)
        report = collect_performance_metrics(service, ledger, config.get('privacy', 'private'))
        try:
            catalog = load_catalog(ROOT / 'catalog.json')
            if not isinstance(catalog, dict):
                catalog = {}
        except Exception as error:
            print(f"Performance preference configuration unavailable: {type(error).__name__}")
            catalog = {}
        report['selection_bias'] = {
            'enabled': bool(catalog.get('performance_bias_enabled', False)),
            'minimum_videos': catalog.get('performance_bias_min_videos', 3),
        }
        for row in report['new_flags']:
            notifications.notify(
                f"Health flag: {row['flag']}"
                f"\nhttps://www.youtube.com/watch?v={row['video_id']}",
                kind="health", key=f"health:{row['video_id']}:{row['flag']}")
        alerted_slots = ledger.data.setdefault('schedule_alerts', {})
        saved_alert = False
        for row in (report.get('schedule') or {}).get('overdue_slots', []):
            slot = row['slot']
            if slot in alerted_slots:
                continue
            if row.get('upload_uncertain'):
                message = (f"Scheduled slot {slot} has an uncertain upload. "
                           "Check YouTube Studio before retrying to avoid a duplicate.")
            else:
                message = (f"Scheduled slot {slot} is {row['minutes_overdue']} minutes late. "
                           "The recurring workflow will attempt safe catch-up.")
            delivered = notifications.notify(
                message, kind="health", key=f"schedule-late:{slot}")
            if delivered:
                alerted_slots[slot] = datetime.now(timezone.utc).isoformat()
                saved_alert = True
        if saved_alert:
            ledger.save()
        report['analytics'] = collect_retention_metrics(analytics, ledger)
        summary = performance_summary(report)
    except Exception:
        try:
            ledger.data['youtube_check'] = {'ok': False, 'checked_at': datetime.now(timezone.utc).isoformat()}
            ledger.save()
        except Exception:
            pass
        summary = ('## Quran Shorts performance report\n\n'
                   'Metrics are temporarily unavailable; upload and connection health are unknown.\n')
    print(summary)
    step_summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if step_summary:
        try:
            with open(step_summary, 'a', encoding='utf-8') as handle:
                handle.write(summary)
        except OSError:
            print('GitHub step summary could not be written; report was printed above')
    return summary


SCHEDULE_HISTORY_DAYS = 30
SCHEDULE_REPORT_DAYS = 7


def schedule_slot_time(slot):
    """Convert a durable Baghdad slot label into an aware datetime."""
    try:
        return datetime.strptime(slot, '%Y-%m-%d/%H:%M').replace(tzinfo=BAGHDAD)
    except (TypeError, ValueError):
        raise CloudError('Invalid schedule slot in the ledger; stopping to prevent extra posts')


SCHEDULE_ALERT_GRACE_MINUTES = 45


def overdue_publication_slots(jobs, now=None, grace_minutes=SCHEDULE_ALERT_GRACE_MINUTES):
    """List today's missed slots after a grace window; never creates or retries uploads."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(BAGHDAD)
    completed = schedule_state(jobs, now=current)['completed']
    slots = []
    for hour in PUBLICATION_HOURS:
        slot = f"{local.date().isoformat()}/{hour:02d}:00"
        elapsed = (local - schedule_slot_time(slot)).total_seconds() / 60
        if elapsed < grace_minutes or slot in completed:
            continue
        uncertain = any(
            isinstance(row, dict) and row.get('status') == 'uploading' and
            row.get('schedule_slot') == slot
            for row in jobs.values()
        )
        slots.append({'slot': slot, 'minutes_overdue': int(elapsed),
                      'upload_uncertain': uncertain})
    return slots


def record_schedule_event(ledger, target_slot, triggered_at, outcome):
    """Best-effort durable timing telemetry; never blocks a publication."""
    event = {'triggered_at': triggered_at.isoformat(), 'outcome': outcome}
    if target_slot:
        target_at = schedule_slot_time(target_slot)
        local_trigger = triggered_at.astimezone(BAGHDAD)
        event.update({
            'target_slot': target_slot,
            'target_at': target_at.isoformat(),
            'triggered_local_at': local_trigger.isoformat(),
            'trigger_offset_minutes': round((local_trigger - target_at).total_seconds() / 60, 1),
        })
    history = ledger.data.setdefault('schedule_history', [])
    history.append(event)
    cutoff = triggered_at - timedelta(days=SCHEDULE_HISTORY_DAYS)
    kept = []
    for row in history:
        try:
            when = datetime.fromisoformat(row.get('triggered_at', '').replace('Z', '+00:00'))
        except (AttributeError, TypeError, ValueError):
            continue
        if when.tzinfo and when >= cutoff:
            kept.append(row)
    ledger.data['schedule_history'] = kept[-500:]
    try:
        ledger.save()
    except Exception:
        print('Schedule timing telemetry could not be saved; publication will continue')
    return event


def schedule_timing_summary(history, jobs=None, now=None, days=SCHEDULE_REPORT_DAYS):
    """Summarize target-to-upload offsets over the recent observation window."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    events = [dict(event) for event in (history or []) if isinstance(event, dict)]
    completed_slots = {event.get('target_slot') for event in events if event.get('outcome') == 'uploaded'}
    # Backfill timing for older scheduled uploads recorded before telemetry was
    # introduced, so the first report is useful immediately.
    for row in (jobs or {}).values():
        slot = row.get('schedule_slot')
        uploaded = row.get('uploaded_at')
        if not slot or not uploaded or slot in completed_slots:
            continue
        try:
            uploaded_at = datetime.fromisoformat(uploaded.replace('Z', '+00:00'))
            target_at = schedule_slot_time(slot).astimezone(timezone.utc)
        except (AttributeError, TypeError, ValueError, CloudError):
            continue
        if uploaded_at.tzinfo and uploaded_at >= cutoff:
            events.append({'triggered_at': uploaded_at.isoformat(), 'outcome': 'uploaded', 'backfilled': True,
                           'target_slot': slot, 'target_at': target_at.isoformat(),
                           'uploaded_at': uploaded_at.isoformat(),
                           'upload_offset_minutes': round((uploaded_at - target_at).total_seconds() / 60, 1)})
            completed_slots.add(slot)
    recent = []
    for event in events:
        try:
            when = datetime.fromisoformat(event.get('triggered_at', '').replace('Z', '+00:00'))
        except (AttributeError, TypeError, ValueError):
            continue
        if when.tzinfo and when >= cutoff:
            recent.append(event)
    uploads = [event for event in recent
               if event.get('outcome') == 'uploaded' and event.get('upload_offset_minutes') is not None]
    offsets = [float(event['upload_offset_minutes']) for event in uploads]
    unresolved = sum(event.get('outcome') == 'selected' and event.get('target_slot') not in completed_slots for event in recent)
    return {
        'window_days': days,
        'heartbeat_runs': sum(not event.get('backfilled') for event in recent),
        'uploads': len(uploads),
        'unresolved': unresolved,
        'average_offset_minutes': round(sum(offsets) / len(offsets), 1) if offsets else None,
        'max_offset_minutes': round(max(offsets), 1) if offsets else None,
        'delayed_uploads': sum(offset > 30 for offset in offsets),
        'overdue_slots': overdue_publication_slots(jobs or {}, now=now),
    }


def next_schedule_slot(jobs, now=None):
    """Catch up today's due slots once the first target time has passed.

    GitHub's scheduled runners can start well after the requested cron minute.
    Keep accepting today's oldest missed slot after the final target instead of
    dropping it at an arbitrary 22:00 cutoff. Legacy/manual daytime uploads
    still count toward the target, and delayed uploads remain spaced at least
    twenty minutes apart.
    """
    try:
        return schedule_state(jobs, now)['next_due']
    except ValueError as error:
        raise CloudError(str(error)) from error


def verify_channel(service, ledger, config):
    expected = config.get('channel_id')
    channels = service.channels().list(part='id', mine=True).execute()
    if not expected or expected not in [row['id'] for row in channels.get('items', [])]:
        raise CloudError('Authorized YouTube channel does not match the configured channel')
    ledger.data['youtube_check'] = {'ok': True, 'channel_id': expected,
                                    'checked_at': datetime.now(timezone.utc).isoformat()}


def run(args, ledger=None, service=None):
    run_started_at = datetime.now(timezone.utc)
    print(f'Cloud runner started: mode={args.mode}', flush=True)
    config = bot.read_json(ROOT / 'automation.json')
    if not isinstance(config, dict):
        raise CloudError('Invalid automation configuration')
    if config.get('privacy', 'private') not in ('private', 'unlisted', 'public'):
        raise CloudError('Invalid publication privacy setting')
    playlist_privacy = config.get('playlist_privacy', 'public')
    if playlist_privacy not in ('private', 'unlisted', 'public'):
        raise CloudError('Invalid playlist privacy setting')
    catalog = load_catalog(ROOT / 'catalog.json')
    catalog_settings = catalog if isinstance(catalog, dict) else {}
    print('Catalog validated; selecting a complete recitation...', flush=True)
    if not catalog:
        raise RuntimeError('No verified recordings configured. Publishing remains inactive.')
    if args.mode in ('publish', 'scheduled') and config.get('enabled') is not True:
        raise RuntimeError('Automatic publishing is not activated')
    if args.mode in ('publish', 'scheduled') and not config.get('channel_id'):
        raise RuntimeError('An expected YouTube channel must be configured before publishing')
    if ledger:
        ledger.load()
    jobs = ledger.data['jobs'] if ledger else {}
    custom_id = os.environ.get('CUSTOM_VIDEO_ID', '').strip()
    custom_clip = None
    custom_clip_sha = None
    if custom_id:
        if args.mode != 'publish' or not ledger or not service:
            raise CloudError('Custom video submissions require publish mode')
        for row in jobs.values():
            if row.get('custom_clip_id') == custom_id:
                if row.get('status') == 'uploaded' and row.get('video_id'):
                    print('Custom video already uploaded: https://www.youtube.com/watch?v=' + row['video_id'])
                    return row['video_id']
                if row.get('status') == 'uploading':
                    raise CloudError('This custom video already has an uncertain upload')
        custom_clip, custom_clip_sha = ledger.load_custom_clip(custom_id)
        custom_clip['status'] = 'processing'
        custom_clip_sha = ledger.save_custom_clip(custom_clip, custom_clip_sha,
                                                   f'Process custom video {custom_id} [skip ci]')
    schedule_slot = None
    if args.mode == 'scheduled':
        if not ledger or not service:
            raise CloudError('Scheduled publishing requires a durable ledger and YouTube service')
        if any(row.get('status') == 'uploading' for row in jobs.values()):
            raise CloudError('An earlier upload is uncertain; review it before retrying')
        schedule_slot = next_schedule_slot(jobs)
        message = ('Schedule: catching up slot ' + schedule_slot if schedule_slot else
                   'Schedule: no slot due (before first target, completed, or spacing limit).')
        print(message)
        schedule_event = record_schedule_event(ledger, schedule_slot, run_started_at,
                                               'selected' if schedule_slot else 'no_slot')
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
                handle.write('## Publication schedule\n\n' + message + '\n')
        if not schedule_slot:
            return
    if args.mode != 'preview' and any(row.get('status') == 'uploading' for row in jobs.values()):
        raise RuntimeError('An earlier upload is uncertain. Check YouTube Studio before continuing.')
    if args.mode in ('publish', 'scheduled'):
        if not ledger or not service:
            raise CloudError('Cloud publishing requires YouTube and a durable remote ledger')
        verify_channel(service, ledger, config)
        if isinstance(catalog, dict) and catalog.get('schema') == 3:
            try:
                check_upload_restrictions(service, ledger, catalog)
            except Exception:
                raise CloudError('Restriction verification unavailable; no new upload was attempted') from None
    next_cursor = None
    if isinstance(catalog, dict) and catalog.get('schema') == 3:
        cursor = ledger.data.get('cursor', len(jobs)) if ledger else len(jobs)
        for _ in range(100):
            try:
                entry, next_cursor = verse_entry_for_position(
                    catalog, jobs, cursor,
                    ledger.data.get('metrics_history', {}) if ledger else {})
            except NoEligibleVerse as error:
                if ledger and args.mode != 'preview':
                    ledger.data['cursor'] = error.cursor
                    ledger.save()
                raise
            workspace = ROOT / 'state' / 'cloud' / entry['id']
            try:
                print(f'Downloading recitation for {entry.get("verse_key", entry["id"])}...', flush=True)
                source = download_recording(entry, workspace / 'recitation.mp3')
                print('Recitation ready; preparing the cinematic render...', flush=True)
                break
            except (TooLongRecording, TooShortRecording):
                cursor = next_cursor
                if ledger and args.mode != 'preview':
                    ledger.data['cursor'] = cursor
                    ledger.save()
        else:
            raise RuntimeError('No complete recitation between 30 seconds and the Shorts limit was found')
    else:
        entries = catalog
        for entry in entries:
            if entry['id'] in jobs and entry.get('sha256') and jobs[entry['id']].get('audio_sha256') != entry['sha256']:
                raise CloudError('A published recording changed; restore its original catalog entry')
        entry = next((entry for entry in entries if entry['id'] not in jobs), None)
        if entry is None:
            print('All verified recordings have been published; no duplicates will be created.')
            return
        workspace = ROOT / 'state' / 'cloud' / entry['id']
        source = download_recording(entry, workspace / 'recitation.mp3')
    # Enforce the policy for every source type, including legacy catalogs.
    if media_duration(source) < 30 or float(entry['duration']) < 30:
        raise TooShortRecording('Recitation must be at least 30 seconds before rendering')
    custom_background = None
    if custom_clip:
        entry['visual_theme'] = custom_clip['theme']
        custom_background = download_custom_video(custom_clip, workspace / 'custom-background.mp4')
    card = make_card(entry, workspace / 'background.png')
    print('Building the fixed Arabic ayah card...', flush=True)
    motion = (None if custom_background or video_background_for(entry) else
              make_motion_overlay(entry, workspace / 'moving-rain.png'))
    used_backgrounds = {str(path) for row in jobs.values()
                        for path in row.get('background_playlist', [])}
    prior_playlists = [row.get('background_playlist', []) for row in jobs.values()
                       if row.get('background_playlist')]
    background_clip_count = int(catalog_settings.get('background_clips_per_video', 3))
    used_pexels_ids = {
        str(source.get('video_id'))
        for row in jobs.values() if isinstance(row, dict)
        for source in row.get('background_sources', [])
        if isinstance(source, dict) and source.get('video_id') is not None
    }
    external_backgrounds, pexels_temp = [], None
    if (not custom_background and
            catalog_settings.get('pexels_backgrounds_enabled') is True):
        external_backgrounds, pexels_temp = best_effort_pexels_backgrounds(
            os.environ.get('PEXELS_API_KEY', '').strip(),
            entry.get('visual_theme'), used_pexels_ids, background_clip_count
        )
    def build_job(background_sources):
        return item_for(
            entry, source, card, motion, background_video=custom_background,
            used_clips=used_backgrounds, used_playlists=prior_playlists,
            portrait_backgrounds=(catalog_settings.get('background_orientation') == 'portrait'),
            background_clip_count=background_clip_count,
            external_backgrounds=background_sources
        )

    try:
        job = build_job(external_backgrounds)
    except Exception as error:
        if not external_backgrounds:
            raise
        print('Pexels metadata setup failed; using reviewed local footage '
              f'({type(error).__name__}).', flush=True)
        cleanup_pexels_backgrounds(pexels_temp)
        pexels_temp, external_backgrounds = None, []
        job = build_job([])

    print('Rendering cinematic montage with crossfades...', flush=True)
    queue = workspace / 'queue.json'
    try:
        bot.atomic_json(queue, {'items': [job]})
        bot.load_queue(queue)  # Apply the same metadata and permission checks as local runs.
        try:
            target = bot.render(job, ROOT, workspace)
        except Exception as error:
            if not external_backgrounds:
                raise
            print('Pexels render failed; retrying with reviewed local footage '
                  f'({type(error).__name__}).', flush=True)
            cleanup_pexels_backgrounds(pexels_temp)
            pexels_temp, external_backgrounds = None, []
            job = build_job([])
            bot.atomic_json(queue, {'items': [job]})
            bot.load_queue(queue)
            target = bot.render(job, ROOT, workspace)
    finally:
        cleanup_pexels_backgrounds(pexels_temp)
    if media_duration(target) < 30:
        raise TooShortRecording('Rendered video is under 30 seconds; upload blocked')
    print('Preview ready:', target)
    if args.mode == 'preview':
        return
    if not ledger or not service:
        raise RuntimeError('Cloud publishing requires YouTube and a durable remote ledger')
    jobs[entry['id']] = {'status': 'uploading', 'audio_sha256': entry['sha256'],
                         'reciter_id': entry.get('recitation_id'),
                         'reciter_name': entry.get('reciter_en'),
                         'chapter_id': entry.get('chapter_id'),
                         'verse_start': entry.get('verse_start', entry.get('verse_number')),
                         'verse_end': entry.get('verse_end', entry.get('verse_number')),
                         'visual_theme': job.get('visual_theme'),
                         'background_playlist': job.get('background_playlist', []),
                         'background_sources': job.get('background_sources', []),
                         'word_timing_mode': job.get('word_timing_mode'),
                         'render_sha256': bot.file_hash(target),
                         'started_at': datetime.now(timezone.utc).isoformat()}
    if custom_id:
        jobs[entry['id']]['custom_clip_id'] = custom_id
        jobs[entry['id']]['custom_source_url'] = custom_clip['url']
    if schedule_slot:
        jobs[entry['id']]['schedule_slot'] = schedule_slot
    if next_cursor is not None:
        ledger.data['cursor'] = next_cursor
    ledger.save()  # Must succeed BEFORE sending any upload bytes.
    video_id = bot.upload(service, job, target, config.get('privacy', 'private'))
    uploaded_at = datetime.now(timezone.utc)
    jobs[entry['id']].update(status='uploaded', video_id=video_id,
                             uploaded_at=uploaded_at.isoformat())
    if args.mode == 'scheduled' and schedule_slot:
        schedule_event.update({'outcome': 'uploaded', 'uploaded_at': uploaded_at.isoformat(),
                               'upload_offset_minutes': round(
                                   (uploaded_at.astimezone(BAGHDAD) -
                                    schedule_slot_time(schedule_slot)).total_seconds() / 60, 1)})
    ledger.save()
    if custom_clip and custom_clip_sha:
        cleanup_ok = delete_custom_blob(custom_clip)
        custom_clip.update(status='uploaded', video_id=video_id,
                           uploaded_at=uploaded_at.isoformat(),
                           cleanup_status='deleted' if cleanup_ok else 'pending')
        if cleanup_ok:
            custom_clip['cleanup_at'] = datetime.now(timezone.utc).isoformat()
        ledger.save_custom_clip(custom_clip, custom_clip_sha,
                                f'Complete custom video {custom_id} [skip ci]')
    print('Uploaded: https://www.youtube.com/watch?v=' + video_id)
    notifications.notify(
        f"Uploaded {entry['id']}\n{entry['surah_en']} — {entry.get('reciter_en', '')}"
        f"\nhttps://www.youtube.com/watch?v={video_id}",
        kind="success", key="upload:" + entry['id'])
    # Save the video ID before optional comments: an interrupted comment must
    # never leave a completed upload marked uncertain.
    try:
        cta_index, comment_ok = bot.post_cta_comment(service, video_id)
        jobs[entry['id']].update(cta_comment_variant=cta_index, cta_comment_succeeded=comment_ok)
        ledger.save()
    except Exception:
        print('Optional comment metadata could not be saved; upload is recorded')
    add_video_to_playlist(service, ledger, entry, video_id, playlist_privacy)
    # Processing signals can appear shortly after upload. This check is best-effort;
    # the next scheduled run checks every tracked upload again.
    time.sleep(12)
    try:
        check_upload_restrictions(service, ledger, catalog)
    except Exception:
        print('Post-upload restriction check deferred to the next scheduled run')
    return video_id


def run_batch(args, ledger, service):
    count = getattr(args, 'count', 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
        raise CloudError('Post count must be between 1 and 5')
    if args.mode != 'publish' and count != 1:
        raise CloudError('Only publish mode supports multiple posts')
    for _ in range(count):
        if not run(args, ledger, service):
            break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['preview', 'publish', 'scheduled', 'report'])
    parser.add_argument('--count', type=int, choices=range(1, 6), default=1)
    args = parser.parse_args(argv)
    if args.mode != 'publish' and args.count != 1:
        parser.error('--count is only supported for publish')
    try:
        if args.mode == 'preview':
            ledger = None
            if os.environ.get('GITHUB_REPOSITORY') and os.environ.get('GITHUB_TOKEN'):
                ledger = RemoteLedger(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN'],
                                      os.environ.get('GITHUB_REF_NAME', 'main'))
            run(args, ledger)
        else:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            token = json.loads(os.environ['YOUTUBE_TOKEN'])
            credentials = Credentials.from_authorized_user_info(token)
            if not credentials.valid:
                credentials.refresh(Request())
            service = build('youtube', 'v3', credentials=credentials, cache_discovery=False)
            ledger = RemoteLedger(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN'],
                                  os.environ.get('GITHUB_REF_NAME', 'main'))
            if args.mode == 'report':
                try:
                    analytics = build('youtubeAnalytics', 'v2', credentials=credentials,
                                      cache_discovery=False)
                except Exception:
                    analytics = None
                run_performance_report(service, ledger, analytics=analytics)
            else:
                run_batch(args, ledger, service)
        return 0
    except Exception as error:
        # External exception bodies can contain secrets. Print only our own diagnostics.
        safe_detail_types = (CloudError, ValueError, FileNotFoundError, TooShortRecording, TooLongRecording)
        detail = str(error) if isinstance(error, safe_detail_types) or (isinstance(error, RuntimeError) and str(error).startswith("Media processing failed:")) else type(error).__name__ + ": cloud run failed; check configuration, source availability and authorization"
        print(detail)
        if (args.mode != 'preview' and
                os.environ.get('QURAN_BOT_DEFER_FAILURE_NOTICE') != '1'):
            # A retry wrapper reports only the final workflow result.
            notifications.notify(detail, kind="failure", key="run-failure:" + args.mode)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
