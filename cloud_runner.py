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
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urljoin

import requests
import bot

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


class RemoteLedger:
    def __init__(self, repository, token, branch='main'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid repository')
        self.url = f'https://api.github.com/repos/{repository}/contents/{STATE_PATH}'
        self.catalog_url = f'https://api.github.com/repos/{repository}/contents/catalog.json'
        self.branch = branch
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}',
                                     'Accept': 'application/vnd.github+json'})
        self.sha = None
        self.data = {'schema': 1, 'jobs': {}}

    def load(self):
        response = self.session.get(self.url, params={'ref': self.branch}, timeout=30)
        if response.status_code == 404:
            return
        if response.status_code != 200:
            raise RuntimeError(f'Cannot read remote ledger (HTTP {response.status_code})')
        payload = response.json()
        self.sha = payload['sha']
        self.data = json.loads(base64.b64decode(payload['content']))
        if self.data.get('schema') != 1 or not isinstance(self.data.get('jobs'), dict):
            raise ValueError('Remote ledger is invalid; stopping to prevent duplicates')
        for row in self.data['jobs'].values():
            if not isinstance(row, dict) or row.get('status') not in ('uploading', 'uploaded'):
                raise ValueError('Remote ledger contains an invalid job')
            if row['status'] == 'uploaded' and not row.get('video_id'):
                raise ValueError('Remote ledger is missing an uploaded video ID')
        cursor = self.data.get('cursor', len(self.data['jobs']))
        if not isinstance(cursor, int) or cursor < 0:
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
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=(15, 60),
                                    headers={'User-Agent': 'quran-shorts-bot/2.0'})
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
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
    if limit <= 0 or limit + tail > 60 or tail < 0.5:
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
    if data.get('visual_style') not in ('premium_rotating_scenes', 'real_video_assets'):
        raise ValueError('The real-video background style is required')
    themes = data.get('visual_themes')
    supported = {'forest_rain', 'mist_mountains', 'starry_night', 'ocean_moon', 'dawn_mosque'}
    if not isinstance(themes, list) or set(themes) != supported or len(themes) != len(supported):
        raise ValueError('All premium visual themes are required exactly once')
    if data.get('show_verified_ayah_text') is not True:
        raise ValueError('Verified ayah text must be shown')
    maximum_text = data.get('max_ayah_characters')
    if isinstance(maximum_text, bool) or not isinstance(maximum_text, int) or not 80 <= maximum_text <= 240:
        raise ValueError('Invalid ayah text limit')
    return data


def verse_entry_for_position(catalog, jobs, position):
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
    for _ in range(total_verses * len(english)):
        reciter_index = position % len(english)
        batch = position // len(english)
        # Every adjacent post changes both reciter and verse. Each reciter still
        # visits every Quran verse exactly once before the sequence repeats.
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
        reciter = english[reciter_index]
        reciter_id = reciter['id']
        chapter_id = int(chapter['id'])
        key = f'qf-v-r{reciter_id}-a{chapter_id}-{verse_number}'
        next_position = position + 1
        position = next_position
        if key in jobs:
            continue
        payload = get_json(urljoin(QURAN_API, f'recitations/{reciter_id}/by_ayah/{chapter_id}:{verse_number}'),
                           {'fields': 'chapter_id,verse_number,verse_key,duration,url'})
        files = payload.get('audio_files', [])
        if len(files) != 1:
            continue
        audio = files[0]
        hint = float(audio.get('duration') or 0)
        if not float(catalog.get('min_audio_seconds', 30)) <= hint <= float(catalog['max_audio_seconds']):
            continue
        relative_url = audio.get('url', '')
        if not relative_url or '://' in relative_url or '..' in relative_url:
            raise ValueError('Quran Foundation returned an invalid audio path')
        text_payload = get_json(urljoin(QURAN_API, 'quran/verses/uthmani'),
                                {'verse_key': f'{chapter_id}:{verse_number}'})
        text_rows = text_payload.get('verses', [])
        ayah_text = text_rows[0].get('text_uthmani', '').strip() if len(text_rows) == 1 else ''
        if not ayah_text or len(ayah_text) > catalog['max_ayah_characters']:
            continue
        style = reciter.get('style') or ''
        reciter_ar = arabic_names.get(reciter_id) or reciter.get('reciter_name')
        return ({
            'id': key, 'source_type': 'quran_verse', 'recitation_id': reciter_id,
            'audio_url': urljoin(QURAN_AUDIO, relative_url),
            'surah_ar': chapter['name_arabic'], 'surah_en': chapter['name_simple'],
            'verse_number': verse_number, 'verse_key': f'{chapter_id}:{verse_number}',
            'ayah_text': ayah_text,
            'reciter_ar': reciter_ar, 'reciter_en': reciter.get('reciter_name', ''), 'style': style,
            'permission_url': catalog['permission_url'], 'attribution': catalog['attribution'],
            'rights': catalog['rights'], 'verified': True, 'whole_recording': True,
            'min_audio_seconds': max(30, float(catalog.get('min_audio_seconds', 30))),
            'max_audio_seconds': float(catalog['max_audio_seconds']),
            'tail_silence_seconds': float(catalog['tail_silence_seconds']),
            'visual_style': catalog['visual_style'],
            # Rotate scenes deterministically so adjacent Shorts cannot use the
            # same visual theme, even after a restart.
            'visual_theme': catalog['visual_themes'][(next_position - 1) % len(catalog['visual_themes'])],
        }, next_position)
    raise RuntimeError('All Quran verse and reciter combinations have been published')


def load_catalog(path):
    data = bot.read_json(path)
    if data.get('schema') == 3:
        return validate_verse_catalog(data)
    if data.get('schema') == 2:
        return api_entries(data)
    if data.get('schema') != 1 or not isinstance(data.get('recordings'), list):
        raise ValueError('Invalid recording catalog')
    seen = set()
    for entry in data['recordings']:
        key = entry.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', key) or key in seen:
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


def media_duration(path):
    result = subprocess.run([bot.ffmpeg(), '-hide_banner', '-nostdin', '-i', str(path),
                             '-f', 'null', '-'], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=180)
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    if not match:
        raise RuntimeError('Cannot measure the complete audio recording')
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def download_quran_verse(entry, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = destination.with_name('recitation-source.mp3')
    with requests.get(entry['audio_url'], stream=True, timeout=(15, 60),
                      headers={'User-Agent': 'quran-shorts-bot/3.0'}) as response:
        response.raise_for_status()
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
        if not relative_url or '://' in relative_url or '..' in relative_url:
            raise ValueError('Quran Foundation returned an invalid audio path')
        part = destination.parent / f'verse-{index:03}.mp3'
        with requests.get(urljoin(QURAN_AUDIO, relative_url), stream=True, timeout=(15, 60),
                          headers={'User-Agent': 'quran-shorts-bot/2.0'}) as response:
            response.raise_for_status()
            with part.open('wb') as handle:
                for block in response.iter_content(65536):
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
    entry['duration'] = duration
    entry['sha256'] = bot.file_hash(destination)
    return destination


def make_card(entry, destination):
    """Render the Arabic-only card as a transparent overlay for filmed clips."""
    from PIL import Image, ImageDraw, ImageFont
    import arabic_reshaper
    from bidi.algorithm import get_display
    font = ROOT / 'assets' / 'Amiri-Regular.ttf'
    if not font.is_file():
        raise FileNotFoundError('Arabic font missing: assets/Amiri-Regular.ttf')
    image = Image.new('RGBA', (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, 'RGBA')
    gold = '#dcc58e'
    def centered(text, y, size, color=gold, rtl=False):
        if rtl:
            text = get_display(arabic_reshaper.reshape(text))
        # Arabic is shaped explicitly below. BASIC prevents Linux builds with
        # RAQM from applying bidi/shaping a second time and scrambling words.
        face = ImageFont.truetype(str(font), size, layout_engine=ImageFont.Layout.BASIC)
        box = draw.textbbox((0, 0), text, font=face)
        while size > 18 and box[2]-box[0] > 840:
            size -= 2
            face = ImageFont.truetype(str(font), size, layout_engine=ImageFont.Layout.BASIC)
            box = draw.textbbox((0, 0), text, font=face)
        draw.text(((1080-(box[2]-box[0]))/2-box[0], y), text, font=face, fill=color)
    def wrap_arabic(text, size, width=800):
        words = text.split()
        lines, current = [], ''
        face = ImageFont.truetype(str(font), size, layout_engine=ImageFont.Layout.BASIC)
        for word in words:
            candidate = (current + ' ' + word).strip()
            shaped = get_display(arabic_reshaper.reshape(candidate))
            box = draw.textbbox((0, 0), shaped, font=face)
            if current and box[2] - box[0] > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    verse_size = 46
    lines = wrap_arabic(entry.get('ayah_text', ''), verse_size)
    # Keep the complete verse visible.  The panel has room for five compact
    # lines; keep shrinking until that limit is met rather than silently
    # dropping the end of a long ayah.
    while len(lines) > 5 and verse_size > 22:
        verse_size -= 2
        lines = wrap_arabic(entry.get('ayah_text', ''), verse_size)
    verse_y = 975
    line_step = min(62, max(42, verse_size + 14))
    reciter_y = verse_y + len(lines)*line_step + 42
    # Size the panel from the actual wrapped verse so a long verified ayah is
    # never hidden behind its lower edge or the reciter label.
    panel_bottom = max(1270, reciter_y + 95)
    draw.rounded_rectangle((90, 520, 990, panel_bottom), radius=70,
                           fill=(3, 18, 22, 178), outline=(207, 180, 119, 150), width=3)
    centered('سورة ' + entry['surah_ar'], 665, 96, '#f4f1e8', rtl=True)
    if entry.get('verse_number'):
        centered('الآية ' + str(entry['verse_number']), 825, 58, '#e7e9e4', rtl=True)
    draw.line((330, 945, 750, 945), fill=gold, width=2)
    for index, line in enumerate(lines):
        centered(line, verse_y + index*line_step, verse_size, '#f4f1e8', rtl=True)
    centered(entry['reciter_ar'], reciter_y, 49, gold, rtl=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, 'PNG')
    return destination


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


def video_background_for(entry):
    """Return a checked-in filmed clip when one is available for the theme."""
    theme = entry.get('visual_theme', 'forest_rain')
    path = ROOT / 'assets' / 'backgrounds' / 'video' / f'{theme}.mp4'
    if path.is_file():
        return path
    # A missing optional theme asset falls back to the verified filmed rain
    # clip; it never falls back to the old static/overlay renderer.
    fallback = ROOT / 'assets' / 'backgrounds' / 'video' / 'forest_rain.mp4'
    return fallback if fallback.is_file() else None


def item_for(entry, source, background, motion_overlay=None):
    cta_index = int(hashlib.sha256(entry['id'].encode('utf-8')).hexdigest(), 16) % len(bot.CTA_COMMENTS)
    cta = bot.CTA_COMMENTS[cta_index]
    if entry.get('verse_number'):
        suffix = f" | Surah {entry['surah_en']}, Ayah {entry['verse_number']} #Shorts"
        prefix = f"سورة {entry['surah_ar']}، الآية {entry['verse_number']} | القارئ "
        reciter = entry['reciter_ar']
        room = max(1, 100 - len(prefix) - len(suffix))
        title = prefix + reciter[:room].rstrip() + suffix
        description = (f"{entry.get('ayah_text', '')}\n\n"
                       f"Beautiful Quran recitation — Surah {entry['surah_en']}, "
                       f"Ayah {entry['verse_number']}, recited by {entry['reciter_en']}\n\n"
                       f"{entry['attribution']}\n\n{entry['permission_url']}\n\n{cta}")
    else:
        suffix = f" | Surah {entry['surah_en']} #Shorts"
        prefix = f"سورة {entry['surah_ar']} | القارئ "
        room = max(1, 100 - len(prefix) - len(suffix))
        title = prefix + entry['reciter_ar'][:room].rstrip() + suffix
        description = (f"Beautiful Quran recitation — Surah {entry['surah_en']}, "
                       f"recited by {entry.get('reciter_en', entry['reciter_ar'])}\n\n"
                       f"{entry['attribution']}\n\n{entry['permission_url']}\n\n{cta}")
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
    filmed = video_background_for(entry)
    if filmed:
        item['background_video'] = str(filmed.resolve())
        item['background_motion'] = 'real_video'
        item.pop('motion_overlay', None)
    if motion_overlay and not filmed:
        item['motion_overlay'] = str(motion_overlay.resolve())
    return item


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
            ledger.block_reciter(catalog, reciter_id)
            incident = {'video_id': row['video_id'], 'reciter_id': reciter_id,
                        'reason': reason, 'timestamp': datetime.now(timezone.utc).isoformat()}
            row['safety_incident'] = incident
            incidents.append(incident)
    if incidents:
        ledger.data.setdefault('incidents', []).extend(incidents)
        ledger.save()
    return incidents


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def performance_averages(history, jobs, window=10):
    """Return simple rolling view averages without affecting content selection."""
    groups = {'reciters': {}, 'visual_themes': {}}
    for job_id, row in jobs.items():
        video_id = row.get('video_id')
        samples = history.get(video_id, [])[-window:]
        if not samples:
            continue
        values = [_count(sample.get('view_count')) for sample in samples]
        reciter = row.get('reciter_name') or (
            f"reciter {row['reciter_id']}" if row.get('reciter_id') else 'unknown')
        theme = row.get('visual_theme') or 'unknown'
        groups['reciters'].setdefault(reciter, []).extend(values)
        groups['visual_themes'].setdefault(theme, []).extend(values)
    return {kind: {name: round(sum(values) / len(values), 1)
                   for name, values in sorted(rows.items())}
            for kind, rows in groups.items()}


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
            continue
        statistics = video.get('statistics', {})
        privacy = video.get('status', {}).get('privacyStatus', 'unknown')
        snapshot = {'timestamp': timestamp,
                    'view_count': _count(statistics.get('viewCount')),
                    'like_count': _count(statistics.get('likeCount')),
                    'comment_count': _count(statistics.get('commentCount')),
                    'privacy_status': privacy}
        history.setdefault(video_id, []).append(snapshot)
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
    ledger.data['performance_averages'] = averages
    schedule = schedule_timing_summary(ledger.data.get('schedule_history', []), jobs, now=now)
    ledger.data['schedule_summary'] = schedule
    ledger.save()
    return {'tracked': len(tracked), 'new_flags': new_flags, 'averages': averages,
            'schedule': schedule, 'timestamp': timestamp}


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
    lines.extend(['', '### Rolling average views by reciter'])
    reciters = report['averages']['reciters']
    lines.extend([f'- {name}: {average:.1f}' for name, average in reciters.items()] or ['- No data yet'])
    lines.extend(['', '### Rolling average views by visual theme'])
    themes = report['averages']['visual_themes']
    lines.extend([f'- {name}: {average:.1f}' for name, average in themes.items()] or ['- No data yet'])
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
    return '\n'.join(lines) + '\n'


def run_performance_report(service, ledger):
    """Best-effort reporting entry point; monitoring must never fail publishing."""
    try:
        config = bot.read_json(ROOT / 'automation.json')
        ledger.load()
        report = collect_performance_metrics(service, ledger, config.get('privacy', 'private'))
        summary = performance_summary(report)
    except Exception:
        summary = ('## Quran Shorts performance report\n\n'
                   'Metrics are temporarily unavailable; the upload workflow remains healthy.\n')
    print(summary)
    step_summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if step_summary:
        try:
            with open(step_summary, 'a', encoding='utf-8') as handle:
                handle.write(summary)
        except OSError:
            print('GitHub step summary could not be written; report was printed above')
    return summary


BAGHDAD = timezone(timedelta(hours=3), 'Asia/Baghdad')
PUBLICATION_HOURS = (11, 16, 20)
SCHEDULE_HISTORY_DAYS = 30
SCHEDULE_REPORT_DAYS = 7


def schedule_slot_time(slot):
    """Convert a durable Baghdad slot label into an aware datetime."""
    try:
        return datetime.strptime(slot, '%Y-%m-%d/%H:%M').replace(tzinfo=BAGHDAD)
    except (TypeError, ValueError):
        raise CloudError('Invalid schedule slot in the ledger; stopping to prevent extra posts')


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
    except Exception as error:
        print('Schedule timing telemetry could not be saved; publication will continue')
    return event


def schedule_timing_summary(history, jobs=None, now=None, days=SCHEDULE_REPORT_DAYS):
    """Summarize target-to-upload offsets over the recent observation window."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    events = list(history or [])
    # Backfill timing for older scheduled uploads recorded before telemetry was
    # introduced, so the first report is useful immediately.
    for row in (jobs or {}).values():
        slot = row.get('schedule_slot')
        uploaded = row.get('uploaded_at')
        if not slot or not uploaded:
            continue
        try:
            uploaded_at = datetime.fromisoformat(uploaded.replace('Z', '+00:00'))
            target_at = schedule_slot_time(slot).astimezone(timezone.utc)
        except (AttributeError, TypeError, ValueError, CloudError):
            continue
        if uploaded_at.tzinfo and uploaded_at >= cutoff:
            events.append({'triggered_at': uploaded_at.isoformat(), 'outcome': 'uploaded',
                           'target_slot': slot, 'target_at': target_at.isoformat(),
                           'uploaded_at': uploaded_at.isoformat(),
                           'upload_offset_minutes': round((uploaded_at - target_at).total_seconds() / 60, 1)})
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
    unresolved = sum(event.get('outcome') == 'selected' for event in recent)
    return {
        'window_days': days,
        'heartbeat_runs': len(recent),
        'uploads': len(uploads),
        'unresolved': unresolved,
        'average_offset_minutes': round(sum(offsets) / len(offsets), 1) if offsets else None,
        'max_offset_minutes': round(max(offsets), 1) if offsets else None,
        'delayed_uploads': sum(offset > 30 for offset in offsets),
    }


def next_schedule_slot(jobs, now=None):
    """Catch up today's due slots once the first target time has passed.

    GitHub's scheduled runners can start well after the requested cron minute.
    Keep accepting today's oldest missed slot after the final target instead of
    dropping it at an arbitrary 22:00 cutoff. Legacy/manual daytime uploads
    still count toward the target, and delayed uploads remain spaced at least
    twenty minutes apart.
    """
    local = (now or datetime.now(timezone.utc)).astimezone(BAGHDAD)
    if local.hour < PUBLICATION_HOURS[0]:
        return None
    prefix = local.date().isoformat() + '/'
    completed = set()
    unassigned = 0
    latest = None
    for row in jobs.values():
        if row.get('status') != 'uploaded':
            continue
        slot = row.get('schedule_slot', '')
        if slot.startswith(prefix):
            completed.add(slot)
        stamp = row.get('uploaded_at') or row.get('started_at')
        if stamp:
            try:
                uploaded = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if uploaded.tzinfo is None:
                    raise ValueError('Upload time must include timezone')
                uploaded = uploaded.astimezone(BAGHDAD)
            except (ValueError, TypeError):
                raise CloudError('Invalid ledger upload time; stopping to prevent extra posts')
            if uploaded.date() == local.date() and uploaded.hour >= PUBLICATION_HOURS[0]:
                latest = max(latest, uploaded) if latest else uploaded
                if not slot.startswith(prefix):
                    unassigned += 1
    if latest and (local - latest).total_seconds() < 20 * 60:
        return None
    for hour in PUBLICATION_HOURS:
        slot = prefix + f'{hour:02d}:00'
        if slot in completed:
            continue
        if unassigned:
            unassigned -= 1
            continue
        if local.hour >= hour:
            return slot
    return None


def run(args, ledger=None, service=None):
    run_started_at = datetime.now(timezone.utc)
    config = bot.read_json(ROOT / 'automation.json')
    if config.get('privacy', 'private') not in ('private', 'unlisted', 'public'):
        raise CloudError('Invalid publication privacy setting')
    catalog = load_catalog(ROOT / 'catalog.json')
    if not catalog:
        raise RuntimeError('No verified recordings configured. Publishing remains inactive.')
    if args.mode in ('publish', 'scheduled') and config.get('enabled') is not True:
        raise RuntimeError('Automatic publishing is not activated')
    if args.mode in ('publish', 'scheduled') and not config.get('channel_id'):
        raise RuntimeError('An expected YouTube channel must be configured before publishing')
    if ledger:
        ledger.load()
    jobs = ledger.data['jobs'] if ledger else {}
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
    if args.mode in ('publish', 'scheduled') and ledger and service and isinstance(catalog, dict) and catalog.get('schema') == 3:
        try:
            check_upload_restrictions(service, ledger, catalog)
        except Exception:
            print('YouTube restriction check was unavailable; publishing safety rules remain unchanged')
    if any(row.get('status') == 'uploading' for row in jobs.values()):
        raise RuntimeError('An earlier upload is uncertain. Check YouTube Studio before continuing.')
    next_cursor = None
    if isinstance(catalog, dict) and catalog.get('schema') == 3:
        cursor = ledger.data.get('cursor', len(jobs)) if ledger else len(jobs)
        for _ in range(100):
            entry, next_cursor = verse_entry_for_position(catalog, jobs, cursor)
            workspace = ROOT / 'state' / 'cloud' / entry['id']
            try:
                source = download_recording(entry, workspace / 'recitation.mp3')
                break
            except (TooLongRecording, TooShortRecording):
                cursor = next_cursor
                if ledger:
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
    card = make_card(entry, workspace / 'background.png')
    motion = make_motion_overlay(entry, workspace / 'moving-rain.png')
    job = item_for(entry, source, card, motion)
    queue = workspace / 'queue.json'
    bot.atomic_json(queue, {'items': [job]})
    bot.load_queue(queue)  # Apply the same metadata and permission checks as local runs.
    target = bot.render(job, ROOT, workspace)
    if media_duration(target) < 30:
        raise TooShortRecording('Rendered video is under 30 seconds; upload blocked')
    print('Preview ready:', target)
    if args.mode == 'preview':
        return
    if not ledger or not service:
        raise RuntimeError('Cloud publishing requires YouTube and a durable remote ledger')
    channels = service.channels().list(part='id', mine=True).execute()
    if config['channel_id'] not in [row['id'] for row in channels.get('items', [])]:
        raise RuntimeError('Authorized YouTube channel does not match the configured channel')
    jobs[entry['id']] = {'status': 'uploading', 'audio_sha256': entry['sha256'],
                         'reciter_id': entry.get('recitation_id'),
                         'reciter_name': entry.get('reciter_en'),
                         'visual_theme': entry.get('visual_theme'),
                         'render_sha256': bot.file_hash(target),
                         'started_at': datetime.now(timezone.utc).isoformat()}
    if schedule_slot:
        jobs[entry['id']]['schedule_slot'] = schedule_slot
    if next_cursor is not None:
        ledger.data['cursor'] = next_cursor
    ledger.save()  # Must succeed BEFORE sending any upload bytes.
    video_id = bot.upload(service, job, target, config.get('privacy', 'private'))
    cta_index, comment_ok = bot.post_cta_comment(service, video_id)
    uploaded_at = datetime.now(timezone.utc)
    jobs[entry['id']].update(status='uploaded', video_id=video_id,
                             uploaded_at=uploaded_at.isoformat(),
                             cta_comment_variant=cta_index,
                             cta_comment_succeeded=comment_ok)
    if args.mode == 'scheduled' and schedule_slot:
        schedule_event.update({'outcome': 'uploaded', 'uploaded_at': uploaded_at.isoformat(),
                               'upload_offset_minutes': round(
                                   (uploaded_at.astimezone(BAGHDAD) -
                                    schedule_slot_time(schedule_slot)).total_seconds() / 60, 1)})
    ledger.save()
    print('Uploaded: https://www.youtube.com/watch?v=' + video_id)
    # Processing signals can appear shortly after upload. This check is best-effort;
    # the next scheduled run checks every tracked upload again.
    time.sleep(12)
    try:
        check_upload_restrictions(service, ledger, catalog)
    except Exception:
        print('Post-upload restriction check deferred to the next scheduled run')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['preview', 'publish', 'scheduled', 'report'])
    args = parser.parse_args()
    try:
        if args.mode == 'preview':
            run(args)
        else:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            token = json.loads(os.environ['YOUTUBE_TOKEN'])
            credentials = Credentials.from_authorized_user_info(token)
            if not credentials.valid:
                credentials.refresh(Request())
            service = build('youtube', 'v3', credentials=credentials, cache_discovery=False)
            ledger = RemoteLedger(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN'])
            if args.mode == 'report':
                run_performance_report(service, ledger)
            else:
                run(args, ledger, service)
        return 0
    except Exception as error:
        # External exception bodies can contain secrets. Print only our own diagnostics.
        print(str(error) if isinstance(error, CloudError) else type(error).__name__ + ': cloud run failed; check configuration, source availability and authorization')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

