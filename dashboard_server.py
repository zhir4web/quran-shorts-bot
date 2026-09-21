"""Small, dependency-light dashboard for the Quran Shorts bot.

The browser never receives GitHub or YouTube credentials.  The server reads the
remote ledger and dispatches the existing, audited GitHub workflow on request.
Run locally with ``python dashboard_server.py``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests


ROOT = Path(__file__).resolve().parent
DASHBOARD_ROOT = ROOT / 'dashboard'
STATE_PATH = '.bot-state/published.json'
REPOSITORY = os.environ.get('GITHUB_REPOSITORY', 'zhir4web/quran-shorts-bot')
BRANCH = os.environ.get('GITHUB_BRANCH', 'main')
WORKFLOW_PATH = '.github/workflows/daily.yml'
# Baghdad stays at UTC+3; use a fixed offset so the dashboard needs no extra
# tzdata package on the small hosted runner or a fresh local Python install.
BAGHDAD = timezone(timedelta(hours=3), 'Asia/Baghdad')
DEFAULT_SLOTS = (11, 16, 20)
MAX_REQUEST_BYTES = 64 * 1024


class DashboardError(RuntimeError):
    """Safe error message for the dashboard API."""


def read_local_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return default


def parse_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def local_day(value):
    parsed = parse_iso(value)
    return parsed.astimezone(BAGHDAD).date() if parsed else None


def video_url(video_id):
    return f'https://www.youtube.com/shorts/{video_id}' if video_id else None


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_overview(automation, catalog, ledger, now=None):
    """Convert the durable ledger into a small, UI-friendly read model."""
    now = now or datetime.now(timezone.utc)
    jobs = ledger.get('jobs', {}) if isinstance(ledger, dict) else {}
    today = now.astimezone(BAGHDAD).date()
    uploaded = []
    for job_id, row in jobs.items():
        if not isinstance(row, dict) or row.get('status') != 'uploaded' or not row.get('video_id'):
            continue
        uploaded_at = parse_iso(row.get('uploaded_at'))
        uploaded.append({
            'id': job_id,
            'video_id': row['video_id'],
            'url': video_url(row['video_id']),
            'uploaded_at': uploaded_at.isoformat() if uploaded_at else row.get('uploaded_at'),
            'uploaded_today': bool(uploaded_at and uploaded_at.astimezone(BAGHDAD).date() == today),
            'reciter': row.get('reciter_name') or 'Unknown reciter',
            'theme': row.get('visual_theme') or 'Unknown theme',
            'schedule_slot': row.get('schedule_slot'),
            'cta_ok': row.get('cta_comment_succeeded'),
        })
    uploaded.sort(key=lambda row: row.get('uploaded_at') or '', reverse=True)
    today_count = sum(row['uploaded_today'] for row in uploaded)
    slots = automation.get('publication_hours', list(DEFAULT_SLOTS)) if isinstance(automation, dict) else list(DEFAULT_SLOTS)
    if not isinstance(slots, list) or not all(isinstance(hour, int) and 0 <= hour <= 23 for hour in slots):
        slots = list(DEFAULT_SLOTS)
    slots = sorted(set(slots))
    target = safe_int(automation.get('max_posts_per_day', len(slots)), len(slots)) if isinstance(automation, dict) else len(slots)
    target = max(target, len(slots))
    metrics = ledger.get('metrics_history', {}) if isinstance(ledger, dict) else {}
    views = []
    for history in metrics.values() if isinstance(metrics, dict) else []:
        if isinstance(history, list) and history:
            views.append(safe_int(history[-1].get('view_count')))
    flags = ledger.get('health_flags', {}) if isinstance(ledger, dict) else {}
    flagged = sum(1 for value in flags.values() if value) if isinstance(flags, dict) else 0
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
            'remaining': max(target - today_count, 0),
        },
        'health': {
            'state': 'attention' if flagged else 'healthy',
            'flagged_videos': flagged,
            'tracked_videos': len(uploaded),
            'average_latest_views': round(sum(views) / len(views), 1) if views else 0,
        },
        'integrations': {
            'youtube': bool(automation.get('channel_id')) if isinstance(automation, dict) else False,
            'tiktok': bool(os.environ.get('TIKTOK_ACCESS_TOKEN') and os.environ.get('TIKTOK_OPEN_ID')),
        },
        'latest_upload': latest_run,
        'recent': uploaded[:12],
    }


class GitHubClient:
    def __init__(self, token, repository=REPOSITORY, branch=BRANCH):
        if not token:
            raise DashboardError('GITHUB_TOKEN is not configured on the dashboard server')
        if '/' not in repository or any(char in repository for char in '\r\n '):
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

    def file_json(self, path, default):
        response = self.session.get(f'{self.base}/contents/{path}', params={'ref': self.branch}, timeout=20)
        if response.status_code == HTTPStatus.NOT_FOUND:
            return default
        if response.status_code != HTTPStatus.OK:
            raise DashboardError(f'GitHub ledger read failed (HTTP {response.status_code})')
        try:
            payload = response.json()
            content = base64.b64decode(payload['content']).decode('utf-8')
            return json.loads(content)
        except (KeyError, ValueError, TypeError) as error:
            raise DashboardError('GitHub returned an invalid state file') from error

    def dispatch(self, mode, count=1):
        if mode not in {'publish', 'preview', 'scheduled'}:
            raise DashboardError('Unsupported workflow mode')
        count = max(1, min(int(count), 5))
        url = f'{self.base}/actions/workflows/{WORKFLOW_PATH}/dispatches'
        for _ in range(count):
            response = self.session.post(url, json={
                'ref': self.branch,
                'inputs': {'mode': mode},
            }, timeout=20)
            if response.status_code != HTTPStatus.NO_CONTENT:
                raise DashboardError(f'Workflow dispatch failed (HTTP {response.status_code})')
        return count


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = 'QuranShortsDashboard/1.0'

    def _json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        expected = self.server.dashboard_key
        if not expected:
            return True
        actual = self.headers.get('X-Dashboard-Key', '')
        return hmac.compare_digest(actual, expected)

    def _body(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise DashboardError('Invalid request length')
        if length > MAX_REQUEST_BYTES:
            raise DashboardError('Request is too large')
        try:
            return json.loads(self.rfile.read(length) or b'{}')
        except ValueError as error:
            raise DashboardError('Request must contain valid JSON') from error

    def _read_model(self):
        client = GitHubClient(self.server.github_token, self.server.repository, self.server.branch)
        automation = client.file_json('automation.json', read_local_json(ROOT / 'automation.json', {}))
        catalog = client.file_json('catalog.json', read_local_json(ROOT / 'catalog.json', {}))
        ledger = client.file_json(STATE_PATH, {'schema': 1, 'jobs': {}})
        return build_overview(automation, catalog, ledger), client

    def do_GET(self):  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path.startswith('/api/') and not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {'error': 'Dashboard key required'})
            return
        if path == '/api/overview':
            try:
                overview, _ = self._read_model()
                self._json(HTTPStatus.OK, overview)
            except (DashboardError, requests.RequestException) as error:
                self._json(HTTPStatus.BAD_GATEWAY, {'error': str(error)})
            return
        if path == '/api/config':
            self._json(HTTPStatus.OK, {
                'repository': self.server.repository,
                'branch': self.server.branch,
                'key_required': bool(self.server.dashboard_key),
                'workflow': WORKFLOW_PATH,
                'features': {'youtube': True, 'tiktok': bool(os.environ.get('TIKTOK_ACCESS_TOKEN'))},
            })
            return
        self._serve_static(path)

    def do_POST(self):  # noqa: N802 - stdlib handler API
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {'error': 'Dashboard key required'})
            return
        path = urlparse(self.path).path
        if path != '/api/workflow':
            self._json(HTTPStatus.NOT_FOUND, {'error': 'Not found'})
            return
        try:
            body = self._body()
            mode = body.get('mode', 'publish')
            count = body.get('count', 1)
            if isinstance(count, bool) or not isinstance(count, int):
                raise DashboardError('count must be an integer')
            _, client = self._read_model()
            dispatched = client.dispatch(mode, count)
            self._json(HTTPStatus.ACCEPTED, {
                'ok': True,
                'mode': mode,
                'dispatched': dispatched,
                'workflow_url': f'https://github.com/{client.repository}/actions/workflows/{WORKFLOW_PATH}',
            })
        except (DashboardError, requests.RequestException, ValueError) as error:
            self._json(HTTPStatus.BAD_GATEWAY, {'error': str(error)})

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
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:")
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
    port = int(os.environ.get('DASHBOARD_PORT', '8787'))
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

