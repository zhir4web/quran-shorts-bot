"""Upload one rendered TikTok video to the creator's inbox as an unpublished draft."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

import requests


API_ROOT = 'https://open.tiktokapis.com/v2'
TOKEN_PATH = '/post/publish/inbox/video/init/'
STATUS_PATH = '/post/publish/status/fetch/'
MIN_DURATION_SECONDS = 61.0
MAX_DURATION_SECONDS = 600.0
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024 * 1024
MIN_CHUNK_BYTES = 5 * 1024 * 1024
STATUS_POLL_ATTEMPTS = 12
STATUS_POLL_INTERVAL_SECONDS = 5
STATE_PATH = '.bot-state/tiktok-drafts.json'


class TikTokDraftError(RuntimeError):
    """Safe error message; never contains access tokens or upload URLs."""


def chunk_plan(video_size):
    if isinstance(video_size, bool) or not isinstance(video_size, int) or video_size <= 0:
        raise TikTokDraftError('The rendered video is empty')
    if video_size > MAX_VIDEO_BYTES:
        raise TikTokDraftError('TikTok limits uploaded videos to 4 GB')
    if video_size < MIN_CHUNK_BYTES:
        return video_size, 1
    count = math.ceil(video_size / MAX_CHUNK_BYTES)
    chunk_size = math.ceil(video_size / count)
    return chunk_size, math.ceil(video_size / chunk_size)


def media_duration(path):
    """Read duration using the same bundled ffmpeg binary as the renderer."""
    from bot import ffmpeg

    result = subprocess.run([ffmpeg(), '-hide_banner', '-nostdin', '-i', str(path), '-f', 'null', '-'],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    if result.returncode or not match:
        raise TikTokDraftError('The rendered MP4 could not be checked')
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def latest_video(path='state/cloud'):
    candidates = list(Path(path).glob('*/video.mp4'))
    if not candidates:
        raise TikTokDraftError('No rendered MP4 was found in this workflow run')
    return max(candidates, key=lambda candidate: candidate.stat().st_mtime)


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as media:
        for block in iter(lambda: media.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class GitHubDraftLedger:
    """Store non-secret run IDs and TikTok publish IDs in the repo state ledger."""

    def __init__(self, token, repository, branch, session=None):
        if not token or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or ''):
            raise TikTokDraftError('GitHub workflow state is not configured')
        self.session = session or requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}',
                                     'Accept': 'application/vnd.github+json',
                                     'User-Agent': 'quran-shorts-tiktok-draft/1.0'})
        self.url = f'https://api.github.com/repos/{repository}/contents/{STATE_PATH}'
        self.branch = branch

    def _read(self):
        response = self.session.get(self.url, params={'ref': self.branch}, timeout=20)
        if response.status_code == 404:
            return {'schema': 1, 'runs': {}}, None
        if response.status_code != 200:
            raise TikTokDraftError('Could not read TikTok draft history')
        try:
            payload = response.json()
            data = json.loads(base64.b64decode(payload['content']).decode('utf-8'))
            if not isinstance(data, dict) or not isinstance(data.get('runs'), dict):
                raise ValueError
            return data, payload['sha']
        except (KeyError, ValueError, TypeError):
            raise TikTokDraftError('TikTok draft history is invalid') from None

    def _write(self, data, sha, message):
        body = {'message': message, 'content': base64.b64encode(
            json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')).decode('ascii'),
            'branch': self.branch}
        if sha:
            body['sha'] = sha
        response = self.session.put(self.url, json=body, timeout=30)
        if response.status_code not in (200, 201):
            raise TikTokDraftError('Could not safely save TikTok draft history')
        return response.json().get('content', {}).get('sha')

    def claim(self, run_id, video_sha256, timestamp, schedule_slot=None):
        data, sha = self._read()
        key = str(run_id)
        if schedule_slot and any(
                isinstance(row, dict) and row.get('schedule_slot') == schedule_slot
                for row in data['runs'].values()):
            raise TikTokDraftError('This TikTok schedule slot already has an upload attempt; it was blocked to prevent a duplicate')
        if key in data['runs']:
            raise TikTokDraftError('This GitHub run already has a TikTok draft attempt; it was blocked to prevent a duplicate')
        row = {'status': 'uploading', 'video_sha256': video_sha256, 'started_at': timestamp}
        if schedule_slot:
            row['schedule_slot'] = schedule_slot
        data['runs'][key] = row
        sha = self._write(data, sha, f'Record TikTok draft attempt {key} [skip ci]')
        return sha

    def update(self, run_id, sha, values):
        data, current_sha = self._read()
        if sha and current_sha != sha:
            # Only this workflow owns the shared concurrency group. A SHA change
            # means an unexpected concurrent edit, so stop rather than overwrite it.
            raise TikTokDraftError('TikTok draft history changed concurrently; review Actions before retrying')
        row = data['runs'].get(str(run_id))
        if not isinstance(row, dict):
            raise TikTokDraftError('TikTok draft attempt is missing from the history')
        row.update(values)
        return self._write(data, current_sha, f'Update TikTok draft attempt {run_id} [skip ci]')


class TikTokClient:
    def __init__(self, access_token, session=None, sleep=time.sleep):
        if not access_token:
            raise TikTokDraftError('TikTok authorization is missing')
        self.session = session or requests.Session()
        self.access_token = access_token
        self.sleep = sleep
        self.headers = {'Authorization': 'Bearer ' + access_token,
                        'Content-Type': 'application/json; charset=UTF-8'}

    def _json(self, response, label):
        if not response.ok:
            raise TikTokDraftError(f'TikTok {label} request failed (HTTP {response.status_code})')
        try:
            result = response.json()
        except ValueError:
            raise TikTokDraftError(f'TikTok {label} returned an invalid response') from None
        if not isinstance(result, dict) or (result.get('error') or {}).get('code', 'ok') != 'ok':
            raise TikTokDraftError(f'TikTok {label} was rejected')
        return result.get('data') or {}

    def upload(self, path, on_initialized=None):
        video_size = Path(path).stat().st_size
        chunk_size, total_chunks = chunk_plan(video_size)
        try:
            initialized = self.session.post(API_ROOT + TOKEN_PATH, headers=self.headers,
                json={'source_info': {'source': 'FILE_UPLOAD', 'video_size': video_size,
                                      'chunk_size': chunk_size, 'total_chunk_count': total_chunks}},
                timeout=30)
        except requests.RequestException:
            raise TikTokDraftError('Could not initialize the TikTok draft upload') from None
        data = self._json(initialized, 'upload initialization')
        publish_id, upload_url = data.get('publish_id'), data.get('upload_url')
        parsed = urlparse(str(upload_url or ''))
        if not publish_id or not parsed.scheme == 'https' or not parsed.hostname or not parsed.hostname.endswith('.tiktokapis.com'):
            raise TikTokDraftError('TikTok returned an invalid upload destination')
        if on_initialized:
            on_initialized(publish_id)

        try:
            with open(path, 'rb') as video:
                offset = 0
                for _ in range(total_chunks):
                    chunk = video.read(chunk_size)
                    if not chunk:
                        raise TikTokDraftError('The rendered video changed during upload')
                    end = offset + len(chunk) - 1
                    response = self.session.put(upload_url, data=chunk, headers={
                        'Content-Type': 'video/mp4', 'Content-Length': str(len(chunk)),
                        'Content-Range': f'bytes {offset}-{end}/{video_size}'},
                        timeout=180, allow_redirects=False)
                    expected = 201 if end + 1 == video_size else 206
                    if response.status_code != expected:
                        raise TikTokDraftError(f'TikTok media transfer failed (HTTP {response.status_code})')
                    offset = end + 1
        except requests.RequestException:
            raise TikTokDraftError('TikTok media transfer outcome is uncertain; do not retry this workflow run') from None

        status = 'PROCESSING_UPLOAD'
        # Upload processing is asynchronous. A short four-poll window can
        # report "still processing" before TikTok has delivered the inbox
        # notification, so allow up to one minute while staying below the
        # documented status endpoint rate limit.
        for attempt in range(STATUS_POLL_ATTEMPTS):
            try:
                checked = self.session.post(API_ROOT + STATUS_PATH, headers=self.headers,
                    json={'publish_id': publish_id}, timeout=20)
            except requests.RequestException:
                break
            result = self._json(checked, 'status check')
            status = str(result.get('status') or 'PROCESSING_UPLOAD')
            if status in ('SEND_TO_USER_INBOX', 'FAILED', 'PUBLISH_COMPLETE'):
                break
            if attempt < STATUS_POLL_ATTEMPTS - 1:
                self.sleep(STATUS_POLL_INTERVAL_SECONDS)
        if status == 'FAILED':
            raise TikTokDraftError('TikTok rejected the video during processing; see the TikTok draft details in the workflow')
        return publish_id, status


def access_token_from_dashboard(session=None, base_url=None, dashboard_key=None):
    session = session or requests.Session()
    base_url = (base_url or os.environ.get('DASHBOARD_URL', '')).rstrip('/')
    dashboard_key = dashboard_key or os.environ.get('DASHBOARD_KEY', '')
    if not base_url.startswith('https://') or not dashboard_key:
        raise TikTokDraftError('The secure TikTok token service is not configured')
    try:
        response = session.post(base_url + '/api/tiktok/access-token', headers={
            'X-Dashboard-Key': dashboard_key, 'Accept': 'application/json'}, timeout=30)
    except requests.RequestException:
        raise TikTokDraftError('TikTok authorization service is temporarily unavailable') from None
    if not response.ok:
        raise TikTokDraftError('TikTok is not connected; connect it from the dashboard and try again')
    try:
        token = response.json().get('access_token')
    except (ValueError, AttributeError):
        token = None
    if not isinstance(token, str) or not token:
        raise TikTokDraftError('TikTok authorization response was invalid')
    return token


def upload_latest(session=None, *, env=None, duration_reader=media_duration, sleep=time.sleep):
    env = os.environ if env is None else env
    path = latest_video()
    if path.suffix.lower() != '.mp4' or path.stat().st_size <= 0:
        raise TikTokDraftError('Only a non-empty MP4 video can be sent to TikTok')
    duration = float(duration_reader(path))
    if duration < MIN_DURATION_SECONDS:
        raise TikTokDraftError('TikTok draft must be at least 61 seconds long')
    if duration > MAX_DURATION_SECONDS:
        raise TikTokDraftError('TikTok draft cannot exceed 10 minutes')
    sha256 = digest_file(path)
    run_id = env.get('GITHUB_RUN_ID', '')
    if not run_id:
        raise TikTokDraftError('A GitHub run ID is required for duplicate protection')
    schedule_slot = env.get('TIKTOK_SCHEDULE_SLOT', '').strip()
    if schedule_slot and not re.fullmatch(r'\d{4}-\d{2}-\d{2}/(?:06|12|18):00', schedule_slot):
        raise TikTokDraftError('The TikTok schedule slot is invalid')
    history = GitHubDraftLedger(env.get('GITHUB_TOKEN', ''), env.get('GITHUB_REPOSITORY', ''),
                                env.get('GITHUB_REF_NAME', 'main'), session=session)
    access_token = access_token_from_dashboard(session=session)
    timestamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    history_sha = history.claim(run_id, sha256, timestamp, schedule_slot=schedule_slot or None)
    current_sha = history_sha

    def save_publish_id(publish_id):
        nonlocal current_sha
        current_sha = history.update(run_id, current_sha, {'publish_id': publish_id})

    try:
        publish_id, status = TikTokClient(access_token, session=session, sleep=sleep).upload(
            path, on_initialized=save_publish_id)
    except TikTokDraftError as error:
        # A claimed run remains closed to reruns even when provider outcome is
        # unclear. This avoids a second inbox draft from the same workflow run.
        history.update(run_id, current_sha, {'status': 'needs_review', 'error': str(error),
                                             'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        raise
    history.update(run_id, current_sha, {'status': status.lower(), 'publish_id': publish_id,
                                         'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    if status == 'SEND_TO_USER_INBOX':
        print('TikTok draft sent to your inbox. Open the TikTok app, tap the inbox notification, review, and publish it yourself.')
    else:
        print('TikTok accepted the upload and is still processing it. Check the TikTok inbox notification before retrying.')
    print('Video duration:', f'{duration:.1f}s')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('upload-latest',))
    parser.parse_args(argv)
    try:
        return upload_latest()
    except TikTokDraftError as error:
        print('TikTok draft was not confirmed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
