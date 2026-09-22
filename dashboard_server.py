"""Small, dependency-light dashboard for the Quran Shorts bot.

The browser never receives GitHub or YouTube credentials.  The server reads the
remote ledger and dispatches the existing, audited GitHub workflow on request.
Run locally with ``python dashboard_server.py``.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests
from schedule_policy import BAGHDAD, PUBLICATION_HOURS, parse_iso, schedule_state


ROOT = Path(__file__).resolve().parent
DASHBOARD_ROOT = ROOT / 'dashboard'
STATE_PATH = '.bot-state/published.json'
REPOSITORY = os.environ.get('GITHUB_REPOSITORY', 'zhir4web/quran-shorts-bot')
BRANCH = os.environ.get('GITHUB_BRANCH', 'main')
WORKFLOW_PATH = '.github/workflows/daily.yml'
WORKFLOW_FILE = 'daily.yml'
# Baghdad stays at UTC+3; use a fixed offset so the dashboard needs no extra
# tzdata package on the small hosted runner or a fresh local Python install.
DEFAULT_SLOTS = PUBLICATION_HOURS
MAX_REQUEST_BYTES = 64 * 1024
# Background clips travel through a JSON URL, not a binary upload: GitHub
# contents-API payloads stay tiny and the Actions runner downloads the file
# itself. Direct peer-to-peer binary uploads would need storage this project
# deliberately does not run.
MAX_CLIP_MB = 60
CLIP_URL = re.compile(r'https://[A-Za-z0-9._~:/?#@!$&()*+,;=%\-]+\.(mp4|mov|webm)(\?[A-Za-z0-9._~:/?#@!$&()*+,;=%\-]*)?', re.IGNORECASE)
CLIP_NAME = re.compile(r'[A-Za-z0-9_-]{1,80}')
SUGGESTED_THEMES = ('forest_rain', 'mist_mountains', 'starry_night', 'ocean_moon', 'dawn_mosque')


class DashboardError(RuntimeError):
    """Safe error message for the dashboard API."""


class BadRequest(DashboardError):
    """Invalid input from a dashboard client."""


def video_url(video_id):
    return f'https://www.youtube.com/shorts/{video_id}' if video_id else None


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_overview(automation, catalog, ledger, now=None, workflow=None):
    """Convert the durable ledger into a small, UI-friendly read model."""
    now = now or datetime.now(timezone.utc)
    if (not isinstance(automation, dict) or not isinstance(ledger, dict)
            or not isinstance(ledger.get('jobs', {}), dict)):
        raise DashboardError('Invalid remote configuration or ledger')
    jobs = ledger.get('jobs', {})
    if any(not isinstance(row, dict) or row.get('status') not in ('uploaded', 'uploading')
           or (row.get('status') == 'uploaded' and not row.get('video_id'))
           for row in jobs.values()):
        raise DashboardError('Invalid remote ledger job')
    today = now.astimezone(BAGHDAD).date()
    uploaded = []
    for job_id, row in jobs.items():
        if not isinstance(row, dict) or row.get('status') != 'uploaded' or not row.get('video_id'):
            continue
        uploaded_at = parse_iso(row.get('uploaded_at') or row.get('started_at'))
        uploaded.append({
            'id': job_id,
            'video_id': row['video_id'],
            'url': video_url(row['video_id']),
            'uploaded_at': uploaded_at.isoformat() if uploaded_at else None,
            'uploaded_today': bool(uploaded_at and uploaded_at.astimezone(BAGHDAD).date() == today),
            'reciter': row.get('reciter_name') or 'Unknown reciter',
            'theme': row.get('visual_theme') or 'Unknown theme',
            'schedule_slot': row.get('schedule_slot'),
            'cta_ok': row.get('cta_comment_succeeded'),
        })
    uploaded.sort(key=lambda row: row.get('uploaded_at') or '', reverse=True)
    retention = []
    for row in uploaded:
        snapshot = jobs.get(row['id'], {}).get('analytics') if isinstance(jobs, dict) else None
        if not isinstance(snapshot, dict):
            continue
        percentage = safe_int(snapshot.get('average_view_percentage'), default=0)
        retention.append({'id': row['id'], 'url': row['url'], 'reciter': row['reciter'],
                          'percentage': percentage,
                          'views': safe_int(snapshot.get('views'), default=0)})
    retention.sort(key=lambda row: (row['percentage'], row['views']), reverse=True)
    today_count = sum(row['uploaded_today'] for row in uploaded)
    slots, target = list(DEFAULT_SLOTS), len(DEFAULT_SLOTS)
    issues = []
    try:
        schedule = schedule_state(jobs, now)
        slot_states = [{'time': slot.split('/')[1], 'completed': slot in schedule['completed'],
                        'due': slot == schedule['next_due']} for slot in schedule['slots']]
    except ValueError:
        issues.append('invalid_schedule_data')
        slot_states = []
    metrics = ledger.get('metrics_history', {}) if isinstance(ledger, dict) else {}
    views = []
    for history in metrics.values() if isinstance(metrics, dict) else []:
        if isinstance(history, list) and history and isinstance(history[-1], dict):
            views.append(safe_int(history[-1].get('view_count')))
    flags = ledger.get('health_flags', {}) if isinstance(ledger, dict) else {}
    flagged_ids = {key for key, value in flags.items() if value} if isinstance(flags, dict) else set()
    flagged_ids.update(row['video_id'] for row in jobs.values() if row.get('video_id') and row.get('safety_incident'))
    flagged = len(flagged_ids)
    uncertain = [dict(id=key, started_at=row.get('started_at')) for key, row in jobs.items()
                 if row.get('status') == 'uploading']
    if uncertain:
        issues.append('uncertain_upload')
    if flagged:
        issues.append('flagged_videos')
    connection = ledger.get('youtube_check', {})
    checked_at = parse_iso(connection.get('checked_at')) if isinstance(connection, dict) else None
    verified = bool(checked_at and timedelta(0) <= now - checked_at <= timedelta(hours=24)
                    and connection.get('channel_id') == automation.get('channel_id')
                    and connection.get('ok') is True)
    if not verified:
        issues.append('youtube_not_verified')
    if workflow and workflow.get('conclusion') in ('failure', 'cancelled', 'timed_out', 'action_required', 'startup_failure'):
        issues.append('workflow_failed')
    if not workflow or workflow.get('status') == 'unknown':
        issues.append('workflow_unknown')
    if automation.get('enabled') is not True:
        issues.append('automation_disabled')
    playlists = ledger.get('playlists', {}) if isinstance(ledger, dict) else {}
    playlist_rows = [{'surah': str(surah)[:60], 'url': f'https://www.youtube.com/playlist?list={pid}' if re.fullmatch(r'[A-Za-z0-9_-]{10,80}', str(pid)) else None}
                     for surah, pid in playlists.items() if isinstance(pid, str)]
    playlist_rows.sort(key=lambda row: row['surah'])
    cta = ledger.get('cta_performance', {}) if isinstance(ledger, dict) else {}
    cta_rows = []
    for variant, stats in cta.items():
        if isinstance(variant, bool) or not str(variant).isdigit() or not isinstance(stats, dict):
            continue
        if int(variant) >= 8:
            continue
        cta_rows.append({'variant': int(variant),
                         'videos': safe_int(stats.get('videos')),
                         'avg_view_percentage': safe_int(stats.get('avg_view_percentage')),
                         'avg_views': safe_int(stats.get('avg_views'))})
    cta_rows.sort(key=lambda row: row['variant'])
    latest_run = uploaded[0]['uploaded_at'] if uploaded else None
    return {
        'generated_at': now.isoformat(),
        'channel': {
            'id': automation.get('channel_id') if isinstance(automation, dict) else None,
            'privacy': automation.get('privacy', 'public') if isinstance(automation, dict) else 'public',
            'enabled': automation.get('enabled') is True if isinstance(automation, dict) else False,
        },
        'schedule': {
            'slots': [f'{hour:02d}:00' for hour in slots],
            'today_count': today_count,
            'target': target,
            'remaining': target - sum(slot['completed'] for slot in slot_states),
            'slot_states': slot_states,
        },
        'health': {
            'state': 'attention' if any(issue not in ('youtube_not_verified', 'workflow_unknown') for issue in issues)
                     else ('unknown' if issues else 'healthy'),
            'issues': issues,
            'uncertain_uploads': uncertain,
            'flagged_videos': flagged,
            'tracked_videos': len(uploaded),
            'average_latest_views': round(sum(views) / len(views), 1) if views else 0,
        },
        'integrations': {
            'youtube': verified,
            'youtube_configured': bool(automation.get('channel_id')),
            'youtube_checked_at': checked_at.isoformat() if checked_at else None,
            'tiktok': False,
        },
        'analytics': {
            'retention': retention[:12],
            'cta_variants': cta_rows,
            'playlists': playlist_rows,
        },
        'workflow': workflow or {'status': 'unknown'},
        'latest_upload': latest_run,
        'recent': uploaded[:12],
    }


class GitHubClient:
    def __init__(self, token, repository=REPOSITORY, branch=BRANCH):
        if not token:
            raise DashboardError('GITHUB_TOKEN is not configured on the dashboard server')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise DashboardError('Invalid GITHUB_REPOSITORY configuration')
        self.repository = repository
        self.branch = branch
        self.base = f'https://api.github.com/repos/{repository}'
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'quran-shorts-dashboard/1.0',
        })

    def file_json(self, path, default=None):
        response = self.session.get(f'{self.base}/contents/{path}', params={'ref': self.branch}, timeout=20)
        if response.status_code == HTTPStatus.NOT_FOUND:
            if default is not None:
                return default
            raise DashboardError('Required remote configuration is missing')
        if response.status_code != HTTPStatus.OK:
            raise DashboardError(f'GitHub ledger read failed (HTTP {response.status_code})')
        try:
            payload = response.json()
            content = base64.b64decode(payload['content']).decode('utf-8')
            return json.loads(content)
        except (KeyError, ValueError, TypeError) as error:
            raise DashboardError('GitHub returned an invalid state file') from error

    def workflow_status(self):
        response = self.session.get(f'{self.base}/actions/workflows/{WORKFLOW_FILE}/runs',
                                    params={'branch': self.branch, 'per_page': 1}, timeout=20)
        if response.status_code != HTTPStatus.OK:
            raise DashboardError(f'Workflow status unavailable (HTTP {response.status_code})')
        runs = response.json().get('workflow_runs', [])
        if not runs:
            return {'status': 'unknown'}
        row = runs[0]
        return {key: row.get(key) for key in ('status', 'conclusion', 'updated_at', 'html_url')}

    def repo_root_sha(self):
        response = self.session.get(f'{self.base}/contents/', params={'ref': self.branch}, timeout=20)
        if response.status_code != HTTPStatus.OK:
            raise DashboardError(f'Repository root unavailable (HTTP {response.status_code})')
        return response.json()

    def write_file(self, path, content_bytes, message):
        """Create or update one file through the contents API."""
        existing = self.session.get(f'{self.base}/contents/{path}', params={'ref': self.branch}, timeout=20)
        body = {'message': message,
                'content': base64.b64encode(content_bytes).decode('ascii'),
                'branch': self.branch}
        if existing.status_code == HTTPStatus.OK:
            body['sha'] = existing.json()['sha']
        elif existing.status_code != HTTPStatus.NOT_FOUND:
            raise DashboardError(f'Cannot read {path} (HTTP {existing.status_code})')
        response = self.session.put(f'{self.base}/contents/{path}', json=body, timeout=30)
        if response.status_code not in (HTTPStatus.OK, HTTPStatus.CREATED):
            raise DashboardError(f'Cannot write {path} (HTTP {response.status_code})')

    def dispatch(self, mode, count=1, custom_id=''):
        if not isinstance(mode, str) or mode not in {'publish', 'preview', 'scheduled'}:
            raise BadRequest('Unsupported workflow mode')
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
            raise BadRequest('count must be an integer between 1 and 5')
        if mode != 'publish' and count != 1:
            raise BadRequest('Only publish supports multiple posts')
        if custom_id and not CLIP_NAME.fullmatch(custom_id):
            raise BadRequest('Invalid custom video id')
        url = f'{self.base}/actions/workflows/{WORKFLOW_FILE}/dispatches'
        inputs = {'mode': mode, 'count': str(count)}
        if custom_id:
            inputs['custom_id'] = custom_id
        try:
            response = self.session.post(url, json={
                'ref': self.branch, 'inputs': inputs,
            }, timeout=20)
        except requests.RequestException:
            raise DashboardError('Dispatch outcome unknown; check GitHub Actions before retrying') from None
        if response.status_code != HTTPStatus.NO_CONTENT:
            raise DashboardError(f'Workflow dispatch failed (HTTP {response.status_code})')
        return count

    def _read_model(self):
        client = GitHubClient(self.server.github_token, self.server.repository, self.server.branch)
        automation = client.file_json('automation.json')
        ledger = client.file_json(STATE_PATH, {'schema': 1, 'jobs': {}})
        try:
            workflow = client.workflow_status()
        except (DashboardError, requests.RequestException, ValueError, TypeError, KeyError):
            workflow = {'status': 'unknown'}
        return build_overview(automation, {}, ledger, workflow=workflow), client

    def do_GET(self):  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path.startswith('/api/') and not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {'error': 'Dashboard key required'})
            return
        if path == '/api/overview':
            try:
                overview, _ = self._read_model()
                self._json(HTTPStatus.OK, overview)
            except (DashboardError, requests.RequestException, ValueError, TypeError) as error:
                self._json(HTTPStatus.BAD_GATEWAY, {'error': str(error) if isinstance(error, DashboardError) else 'Remote data unavailable'})
            return
        if path == '/api/config':
            self._json(HTTPStatus.OK, {
                'repository': self.server.repository,
                'branch': self.server.branch,
                'key_required': bool(self.server.dashboard_key),
                'workflow': WORKFLOW_PATH,
                'features': {'youtube': True, 'tiktok': False},
            })
            return
        self._serve_static(path)

    def do_POST(self):  # noqa: N802 - stdlib handler API
        # Reject cross-site form/fetch requests, including on keyless localhost.
        origin = self.headers.get('Origin')
        if (self.headers.get('Sec-Fetch-Site') == 'cross-site'
                or (origin and urlparse(origin).netloc != self.headers.get('Host'))):
            self._json(HTTPStatus.FORBIDDEN, {'error': 'Cross-origin requests are not allowed'})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {'error': 'Dashboard key required'})
            return
        path = urlparse(self.path).path
        if path == '/api/submit-clip':
            self._submit_clip()
            return
        if path != '/api/workflow':
            self._json(HTTPStatus.NOT_FOUND, {'error': 'Not found'})
            return
        try:
            body = self._body()
            mode = body.get('mode', 'publish')
            count = body.get('count', 1)
            if isinstance(count, bool) or not isinstance(count, int):
                raise BadRequest('count must be an integer')
            overview, client = self._read_model()
            if mode in ('publish', 'scheduled') and (not overview['channel']['enabled'] or overview['health']['uncertain_uploads']):
                self._json(HTTPStatus.CONFLICT, {'error': 'Publishing is disabled or an upload needs review'})
                return
            dispatched = client.dispatch(mode, count)
            self._json(HTTPStatus.ACCEPTED, {
                'ok': True,
                'mode': mode,
                'dispatched': 1,
                'requested_count': dispatched,
                'workflow_url': f'https://github.com/{client.repository}/actions/workflows/{WORKFLOW_FILE}',
            })
        except BadRequest as error:
            self._json(HTTPStatus.BAD_REQUEST, {'error': str(error)})
        except (DashboardError, requests.RequestException, ValueError, TypeError) as error:
            self._json(HTTPStatus.BAD_GATEWAY, {'error': str(error) if isinstance(error, DashboardError) else 'Remote request failed'})

    def _submit_clip(self):
        """Queue an owned/licensed user video and start one publish run.

        The manifest stores only a direct HTTPS media URL; the Actions runner
        downloads and validates it, then uses the normal Quran card, metadata,
        CTA and copyright checks. Nothing is published unless the owner checks
        the licence confirmation in the dashboard.
        """
        try:
            body = self._body()
            url = str(body.get('url') or '').strip()
            theme = str(body.get('theme') or '').strip()
            title = str(body.get('title') or '').strip()
            source_page = str(body.get('source_page') or '').strip()
            if not CLIP_URL.fullmatch(url):
                raise BadRequest('Provide a direct HTTPS MP4/MOV/WebM link')
            if theme not in SUGGESTED_THEMES:
                raise BadRequest('Pick one of the five reviewed themes')
            if not title or len(title) > 120:
                raise BadRequest('A short clip title is required (max 120 characters)')
            if source_page and not source_page.startswith('https://'):
                raise BadRequest('The source page must be an HTTPS link')
            if body.get('license_confirmed') is not True:
                raise BadRequest('Confirm that you own or are licensed to use this video')
            overview, client = self._read_model()
            if not overview['channel']['enabled']:
                raise BadRequest('Publishing is disabled; enable it before submitting videos')
            client.repo_root_sha()  # Confirms token write access early.
            slug_text = re.sub(r'[^A-Za-z0-9_-]+', '-', title.replace(' ', '-')).strip('-')
            slug = CLIP_NAME.fullmatch(slug_text)
            clip_id = (slug.group(0) if slug else 'custom-video') + '-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
            record = {'id': clip_id, 'url': url, 'theme': theme, 'title': title,
                      'source_page': source_page or None,
                      'submitted_at': datetime.now(timezone.utc).isoformat(),
                      'status': 'queued', 'license_confirmed': True,
                      'max_mb': MAX_CLIP_MB}
            client.write_file(f'.bot-state/clip-submissions/{clip_id}.json',
                              json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8'),
                              f'Queue custom video {clip_id} [skip ci]')
            try:
                client.dispatch('publish', 1, custom_id=clip_id)
            except Exception:
                self._json(HTTPStatus.BAD_GATEWAY, {
                    'error': f'Video saved as {clip_id}, but the publish job could not be started. Retry from GitHub Actions.',
                    'id': clip_id})
                return
            self._json(HTTPStatus.ACCEPTED, {'ok': True, 'id': clip_id,
                                             'status': 'queued',
                                             'note': 'ڤیدیۆکە وەرگیرا و بۆ edit و بڵاوکردنەوە نێردرا؛ بەرنامەکە کارت و دەنگی قورئان لەسەری دادەنێت.'})
        except BadRequest as error:
            self._json(HTTPStatus.BAD_REQUEST, {'error': str(error)})
        except (DashboardError, requests.RequestException, ValueError, TypeError) as error:
            self._json(HTTPStatus.BAD_GATEWAY, {'error': str(error) if isinstance(error, DashboardError) else 'Remote request failed'})

    def _serve_static(self, path):
        relative = 'index.html' if path in ('/', '') else path.lstrip('/')
        candidate = (DASHBOARD_ROOT / relative).resolve()
        try:
            candidate.relative_to(DASHBOARD_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_types = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
                         '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml',
                         '.webmanifest': 'application/manifest+json; charset=utf-8'}
        raw = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', content_types.get(candidate.suffix, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        # Keep tokens, request bodies, and remote response bodies out of logs.
        print(f'[dashboard] {self.command} {self.path} {args[1] if len(args) > 1 else ""}')


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, github_token, repository, branch, dashboard_key):
        super().__init__(address, handler)
        self.github_token = github_token
        self.repository = repository
        self.branch = branch
        self.dashboard_key = dashboard_key


def main():
    host = os.environ.get('DASHBOARD_HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', os.environ.get('DASHBOARD_PORT', '8787')))
    key = os.environ.get('DASHBOARD_KEY', '')
    if host not in {'127.0.0.1', 'localhost', '::1'} and not key:
        raise SystemExit('DASHBOARD_KEY is required when exposing the dashboard beyond localhost')
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        raise SystemExit('Set GITHUB_TOKEN before starting the dashboard')
    server = DashboardServer((host, port), DashboardHandler, token, REPOSITORY, BRANCH, key)
    print(f'Dashboard running at http://{host}:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
