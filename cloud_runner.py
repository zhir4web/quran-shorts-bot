"""Daily cloud runner with a remote write-ahead upload ledger.

The catalog contains complete, licensed recordings and verified metadata.
No token or rendered media is written to Git history.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
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


class TooLongRecording(RuntimeError):
    """The complete recording cannot fit safely in a YouTube Short."""


class RemoteLedger:
    def __init__(self, repository, token, branch='main'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid repository')
        self.url = f'https://api.github.com/repos/{repository}/contents/{STATE_PATH}'
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
    if data.get('visual_style') != 'premium_rotating_scenes':
        raise ValueError('The premium original visual style is required')
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
        if hint <= 0 or hint > float(catalog['max_audio_seconds']):
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
    """Original peaceful forest artwork with only the requested title and name."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import arabic_reshaper
    from bidi.algorithm import get_display
    import random
    font = ROOT / 'assets' / 'Amiri-Regular.ttf'
    if not font.is_file():
        raise FileNotFoundError('Arabic font missing: assets/Amiri-Regular.ttf')
    image = Image.new('RGB', (1080, 1920), '#0a2025')
    draw = ImageDraw.Draw(image)
    for y in range(1920):
        blend = y / 1920
        draw.line((0, y, 1080, y), fill=(8+int(blend*10), 30+int(blend*22), 38+int(blend*18)))
    seed = int(hashlib.sha256(entry['id'].encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    theme = entry.get('visual_theme', 'forest_rain')
    # Every scene is drawn locally; no downloaded image or video is used.
    glow = Image.new('RGBA', image.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    glow_color = (229, 224, 193, 125) if theme != 'dawn_mosque' else (255, 198, 125, 150)
    gd.ellipse((745, 170, 945, 370), fill=glow_color)
    glow = glow.filter(ImageFilter.GaussianBlur(30))
    image = Image.alpha_composite(image.convert('RGBA'), glow).convert('RGB')
    draw = ImageDraw.Draw(image, 'RGBA')
    if theme == 'starry_night':
        for _ in range(115):
            x, y = rng.randrange(1080), rng.randrange(1000)
            r = rng.choice((1, 1, 2, 3))
            draw.ellipse((x-r, y-r, x+r, y+r), fill=(232, 229, 203, rng.randrange(80, 190)))
    for band in range(5):
        y = 930 + band * 120
        draw.ellipse((-250, y-110, 1330, y+180), fill=(174, 195, 187, 10+band*4))
    def pine(x, base, height, color):
        width = int(height * .42)
        draw.rectangle((x-8, base-height*.18, x+8, base), fill=color)
        for level in range(5):
            top = base-height + level*height*.16
            half = width*(.48+level*.13)
            draw.polygon(((x, top), (x-half, top+height*.36), (x+half, top+height*.36)), fill=color)
    if theme in ('forest_rain', 'starry_night'):
        for base, low, high, color in [
                (1420, 330, 560, (20, 55, 52, 210)),
                (1600, 430, 720, (11, 42, 40, 235)),
                (1920, 560, 920, (5, 29, 30, 255))]:
            x = -80
            while x < 1160:
                pine(x, base+rng.randint(-35, 35), rng.randint(low, high), color)
                x += rng.randint(105, 190)
    elif theme == 'mist_mountains':
        draw.polygon(((-100, 1540), (260, 920), (510, 1490), (760, 820), (1200, 1580)),
                     fill=(19, 52, 57, 235))
        draw.polygon(((-100, 1800), (340, 1130), (600, 1690), (880, 1080), (1220, 1800)),
                     fill=(8, 35, 41, 255))
    elif theme == 'ocean_moon':
        for y in range(1160, 1920, 34):
            offset = (y // 34) % 2 * 45
            for x in range(-80+offset, 1160, 150):
                draw.arc((x, y, x+180, y+55), 190, 350,
                         fill=(100, 158, 160, max(35, 125-(y-1160)//8)), width=3)
    elif theme == 'dawn_mosque':
        draw.rectangle((0, 1430, 1080, 1920), fill=(9, 37, 42, 255))
        draw.rectangle((360, 1210, 720, 1660), fill=(7, 31, 36, 255))
        draw.ellipse((405, 1040, 675, 1340), fill=(7, 31, 36, 255))
        draw.rectangle((185, 1030, 245, 1660), fill=(7, 31, 36, 255))
        draw.polygon(((215, 900), (165, 1050), (265, 1050)), fill=(7, 31, 36, 255))
        draw.rectangle((835, 1030, 895, 1660), fill=(7, 31, 36, 255))
        draw.polygon(((865, 900), (815, 1050), (915, 1050)), fill=(7, 31, 36, 255))
    shade = Image.new('RGBA', image.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    sd.rounded_rectangle((90, 520, 990, 1270), radius=70, fill=(3, 18, 22, 118), outline=(207, 180, 119, 90), width=2)
    image = Image.alpha_composite(image.convert('RGBA'), shade).convert('RGB')
    draw = ImageDraw.Draw(image)
    gold = '#dcc58e'
    def centered(text, y, size, color=gold, rtl=False):
        if rtl:
            text = get_display(arabic_reshaper.reshape(text))
        while size > 24:
            # Arabic is shaped explicitly below. BASIC prevents Linux builds with
            # RAQM from applying bidi/shaping a second time and scrambling words.
            face = ImageFont.truetype(str(font), size, layout_engine=ImageFont.Layout.BASIC)
            box = draw.textbbox((0, 0), text, font=face)
            if box[2]-box[0] <= 840:
                break
            size -= 2
        draw.text(((1080-(box[2]-box[0]))/2-box[0], y), text, font=face, fill=color)
    centered('سورة ' + entry['surah_ar'], 665, 96, '#f4f1e8', rtl=True)
    if entry.get('verse_number'):
        centered('الآية ' + str(entry['verse_number']), 825, 58, '#e7e9e4', rtl=True)
    draw.line((330, 945, 750, 945), fill=gold, width=2)
    words = entry.get('ayah_text', '').split()
    lines, current = [], ''
    for word in words:
        candidate = (current + ' ' + word).strip()
        shaped = get_display(arabic_reshaper.reshape(candidate))
        face = ImageFont.truetype(str(font), 46, layout_engine=ImageFont.Layout.BASIC)
        if current and draw.textbbox((0, 0), shaped, font=face)[2] > 800:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) <= 3:
        for index, line in enumerate(lines):
            centered(line, 990 + index*64, 46, '#f4f1e8', rtl=True)
        reciter_y = 1010 + len(lines)*72
    else:
        reciter_y = 1015
    centered(entry['reciter_ar'], reciter_y, 49, gold, rtl=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
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


def item_for(entry, source, background, motion_overlay=None):
    if entry.get('verse_number'):
        style = f" ({entry['style']})" if entry.get('style') else ''
        title = f"سورة {entry['surah_ar']}، الآية {entry['verse_number']} | {entry['reciter_ar']}{style} #Shorts"
        description = (f"{entry.get('ayah_text', '')}\n\n"
                       f"تلاوة كاملة للآية {entry['verse_key']} من سورة {entry['surah_ar']}، "
                       f"دون تغيير سرعة التلاوة.\n{entry['permission_url']}")
    else:
        title = f"سورة {entry['surah_ar']} | {entry['reciter_ar']} #Shorts"
        description = f"سورة {entry['surah_ar']} كاملة، دون تغيير سرعة التلاوة.\n{entry['permission_url']}"
    item = {'id': entry['id'], 'mode': 'compose', 'source': str(source.resolve()),
            'background': str(background.resolve()), 'start': 0, 'duration': entry['duration'],
            'background_motion': 'premium_motion',
            'visual_theme': entry.get('visual_theme', 'forest_rain'),
            'title': title, 'description': description,
            'attribution': entry['attribution'], 'rights': entry['rights'],
            'rights_confirmed': True, 'made_for_kids': False}
    if motion_overlay:
        item['motion_overlay'] = str(motion_overlay.resolve())
    return item


def run(args, ledger=None, service=None):
    config = bot.read_json(ROOT / 'automation.json')
    if config.get('privacy', 'private') not in ('private', 'unlisted', 'public'):
        raise CloudError('Invalid publication privacy setting')
    catalog = load_catalog(ROOT / 'catalog.json')
    if not catalog:
        raise RuntimeError('No verified recordings configured. Publishing remains inactive.')
    if args.mode == 'publish' and config.get('enabled') is not True:
        raise RuntimeError('Automatic publishing is not activated')
    if args.mode == 'publish' and not config.get('channel_id'):
        raise RuntimeError('An expected YouTube channel must be configured before publishing')
    if ledger:
        ledger.load()
    jobs = ledger.data['jobs'] if ledger else {}
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
            except TooLongRecording:
                cursor = next_cursor
                if ledger:
                    ledger.data['cursor'] = cursor
                    ledger.save()
        else:
            raise RuntimeError('No complete verse under the Shorts duration limit was found')
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
    card = make_card(entry, workspace / 'background.png')
    motion = make_motion_overlay(entry, workspace / 'moving-rain.png')
    job = item_for(entry, source, card, motion)
    queue = workspace / 'queue.json'
    bot.atomic_json(queue, {'items': [job]})
    bot.load_queue(queue)  # Apply the same metadata and permission checks as local runs.
    target = bot.render(job, ROOT, workspace)
    print('Preview ready:', target)
    if args.mode == 'preview':
        return
    if not ledger or not service:
        raise RuntimeError('Cloud publishing requires YouTube and a durable remote ledger')
    channels = service.channels().list(part='id', mine=True).execute()
    if config['channel_id'] not in [row['id'] for row in channels.get('items', [])]:
        raise RuntimeError('Authorized YouTube channel does not match the configured channel')
    jobs[entry['id']] = {'status': 'uploading', 'audio_sha256': entry['sha256'],
                         'render_sha256': bot.file_hash(target),
                         'started_at': datetime.now(timezone.utc).isoformat()}
    if next_cursor is not None:
        ledger.data['cursor'] = next_cursor
    ledger.save()  # Must succeed BEFORE sending any upload bytes.
    video_id = bot.upload(service, job, target, config.get('privacy', 'private'))
    jobs[entry['id']].update(status='uploaded', video_id=video_id)
    ledger.save()
    print('Uploaded: https://www.youtube.com/watch?v=' + video_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['preview', 'publish'])
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
            run(args, ledger, service)
        return 0
    except Exception as error:
        # External exception bodies can contain secrets. Print only our own diagnostics.
        print(str(error) if isinstance(error, CloudError) else type(error).__name__ + ': cloud run failed; check configuration, source availability and authorization')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
