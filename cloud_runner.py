"""Daily cloud runner with a remote write-ahead upload ledger.

The catalog contains complete, licensed recordings and verified metadata.
No token or rendered media is written to Git history.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
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
                reciter_ar += f' ({style})'
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


def load_catalog(path):
    data = bot.read_json(path)
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
    """Original geometric artwork; Arabic shaping preserves joined letter forms."""
    from PIL import Image, ImageDraw, ImageFont
    import arabic_reshaper
    from bidi.algorithm import get_display
    font = ROOT / 'assets' / 'Amiri-Regular.ttf'
    if not font.is_file():
        raise FileNotFoundError('Arabic font missing: assets/Amiri-Regular.ttf')
    image = Image.new('RGB', (1080, 1920), '#091b23')
    draw = ImageDraw.Draw(image)
    for y in range(1920):
        blend = y / 1920
        draw.line((0, y, 1080, y), fill=(8+int(blend*6), 27+int(blend*18), 35+int(blend*13)))
    gold = '#cfb477'
    draw.rounded_rectangle((65, 130, 1015, 1790), radius=280, outline=gold, width=3)
    draw.rounded_rectangle((88, 155, 992, 1765), radius=260, outline='#52604e', width=1)
    def centered(text, y, size, color=gold, rtl=False):
        if rtl:
            text = get_display(arabic_reshaper.reshape(text))
        while size > 24:
            face = ImageFont.truetype(str(font), size)
            box = draw.textbbox((0, 0), text, font=face)
            if box[2]-box[0] <= 840:
                break
            size -= 2
        draw.text(((1080-(box[2]-box[0]))/2-box[0], y), text, font=face, fill=color)
    centered('القرآن الكريم', 300, 64, rtl=True)
    draw.line((270, 540, 810, 540), fill=gold, width=2)
    centered('سورة ' + entry['surah_ar'], 705, 104, '#f1eee2', rtl=True)
    centered(entry['surah_en'], 915, 46, '#d1d8d1')
    centered(entry['reciter_ar'], 1130, 51, rtl=True)
    draw.line((270, 1370, 810, 1370), fill=gold, width=2)
    centered('تلاوة كاملة', 1480, 42, '#d1d8d1', rtl=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    return destination


def item_for(entry, source, background):
    return {'id': entry['id'], 'mode': 'compose', 'source': str(source.resolve()),
            'background': str(background.resolve()), 'start': 0, 'duration': entry['duration'],
            'title': f"سورة {entry['surah_ar']} | {entry['reciter_ar']} #Shorts",
            'description': f"سورة {entry['surah_ar']} كاملة، دون تغيير سرعة التلاوة.\n{entry['permission_url']}",
            'attribution': entry['attribution'], 'rights': entry['rights'],
            'rights_confirmed': True, 'made_for_kids': False}


def run(args, ledger=None, service=None):
    config = bot.read_json(ROOT / 'automation.json')
    if config.get('privacy', 'private') not in ('private', 'unlisted', 'public'):
        raise CloudError('Invalid publication privacy setting')
    entries = load_catalog(ROOT / 'catalog.json')
    if not entries:
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
    job = item_for(entry, source, card)
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

