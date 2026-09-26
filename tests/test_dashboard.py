import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import http.client
import json
import threading
import tempfile
from pathlib import Path

import dashboard_server as dashboard


class DashboardTests(unittest.TestCase):
    def test_overview_is_derived_from_uploaded_ledger(self):
        now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
        automation = {'enabled': True, 'channel_id': 'channel', 'privacy': 'public'}
        ledger = {
            'jobs': {
                'job-1': {
                    'status': 'uploaded', 'video_id': 'abc123',
                    'uploaded_at': '2026-09-21T09:30:00+00:00',
                    'reciter_name': 'Reader', 'visual_theme': 'forest_rain',
                    'cta_comment_succeeded': True,
                },
                'job-2': {'status': 'uploading', 'video_id': None},
            },
            'metrics_history': {'abc123': [{'view_count': 42}]},
            'health_flags': {},
        }
        overview = dashboard.build_overview(automation, {}, ledger, now=now)
        self.assertEqual(overview['schedule']['today_count'], 1)
        self.assertEqual(overview['health']['tracked_videos'], 1)
        self.assertEqual(overview['health']['average_latest_views'], 42.0)
        self.assertEqual(overview['recent'][0]['url'], 'https://www.youtube.com/shorts/abc123')
        self.assertFalse(overview['integrations']['youtube'])
        self.assertEqual(overview['health']['state'], 'attention')
        self.assertEqual(overview['health']['uncertain_uploads'][0]['id'], 'job-2')

    def test_dispatch_uses_workflow_input_without_exposing_token(self):
        client = dashboard.GitHubClient('secret-token', 'owner/repo', 'main')
        response = Mock(status_code=204)
        client.session.post = Mock(return_value=response)
        self.assertEqual(client.dispatch('publish', 2), 2)
        self.assertEqual(client.session.post.call_count, 1)
        self.assertTrue(client.session.post.call_args.args[0].endswith('/workflows/daily.yml/dispatches'))
        body = client.session.post.call_args.kwargs['json']
        self.assertEqual(body, {'ref': 'main', 'inputs': {'mode': 'publish', 'count': '2'}})
        self.assertNotIn('secret-token', repr(body))

    def test_dispatch_404_explains_vercel_token_permissions(self):
        client = dashboard.GitHubClient('secret-token', 'owner/repo', 'main')
        client.session.post = Mock(return_value=Mock(status_code=404))
        with self.assertRaisesRegex(dashboard.DashboardError, 'Actions: write'):
            client.dispatch('publish', 1)

    def test_dispatch_rejects_unknown_mode(self):
        client = dashboard.GitHubClient('secret-token', 'owner/repo')
        with self.assertRaises(dashboard.DashboardError):
            client.dispatch('delete', 1)

    def test_dispatch_rejects_bad_counts_without_network(self):
        client = dashboard.GitHubClient('secret', 'owner/repo')
        client.session.post = Mock()
        for count in (True, 0, 6, '2', 1.5):
            with self.subTest(count=count), self.assertRaises(dashboard.BadRequest):
                client.dispatch('publish', count)
        with self.assertRaises(dashboard.BadRequest):
            client.dispatch('preview', 2)
        client.session.post.assert_not_called()

    def test_connection_requires_recent_matching_success(self):
        now = datetime(2026, 9, 22, tzinfo=timezone.utc)
        config = {'enabled': True, 'channel_id': 'expected'}
        for age, channel, ok, expected in ((1, 'expected', True, True),
                (25, 'expected', True, False), (1, 'wrong', True, False),
                (1, 'expected', False, False), (-1, 'expected', True, False)):
            ledger = {'jobs': {}, 'youtube_check': {'checked_at': (now - timedelta(hours=age)).isoformat(),
                       'ok': ok, 'channel_id': channel}}
            result = dashboard.build_overview(config, {}, ledger, now, {'status': 'completed', 'conclusion': 'success'})
            self.assertEqual(result['integrations']['youtube'], expected)

    def test_schedule_uses_actual_slots_not_total_posts(self):
        now = datetime(2026, 9, 22, 18, tzinfo=timezone.utc)
        jobs = {'a': {'status': 'uploaded', 'video_id': 'a', 'uploaded_at': '2026-09-22T12:00:00Z',
                      'schedule_slot': '2026-09-22/15:00'}}
        result = dashboard.build_overview({'enabled': True}, {}, {'jobs': jobs}, now)
        self.assertEqual([slot['completed'] for slot in result['schedule']['slot_states']], [False, False, True])

    def test_failed_workflow_never_reports_healthy(self):
        result = dashboard.build_overview({'enabled': True}, {}, {'jobs': {}},
                                         workflow={'status': 'completed', 'conclusion': 'failure'})
        self.assertEqual(result['health']['state'], 'attention')
        self.assertIn('workflow_failed', result['health']['issues'])

    def test_invalid_ledger_is_not_presented_as_healthy(self):
        with self.assertRaises(dashboard.DashboardError):
            dashboard.build_overview({}, {}, {'jobs': []})

    def test_analytics_section_surfaces_retention_cta_and_playlists(self):
        now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
        automation = {'enabled': True, 'channel_id': 'channel', 'privacy': 'public'}
        ledger = {
            'jobs': {
                'job-1': {'status': 'uploaded', 'video_id': 'abc123',
                          'uploaded_at': '2026-09-21T09:30:00+00:00',
                          'reciter_name': 'Strong', 'analytics': {
                              'average_view_percentage': 61.2, 'views': 400}},
                'job-2': {'status': 'uploaded', 'video_id': 'def456',
                          'uploaded_at': '2026-09-20T09:30:00+00:00',
                          'reciter_name': 'Weak', 'analytics': {
                              'average_view_percentage': 42.0, 'views': 900}},
            },
            'playlists': {'Al-Ikhlas': 'PLgood123456', 'broken': 'bad id with spaces'},
            'cta_performance': {'0': {'videos': 5, 'avg_view_percentage': 61.3, 'avg_views': 150.5},
                                '9': {'videos': 1, 'avg_view_percentage': 10, 'avg_views': 5},
                                'bogus': {'videos': 2}},
        }
        result = dashboard.build_overview(automation, {}, ledger, now=now)
        retention = result['analytics']['retention']
        self.assertEqual([row['reciter'] for row in retention], ['Strong', 'Weak'])
        self.assertEqual(retention[0]['percentage'], 61)
        playlists = result['analytics']['playlists']
        self.assertEqual(len(playlists), 2)
        self.assertIsNotNone(playlists[0]['url'])
        self.assertIsNone(playlists[1]['url'])
        cta = result['analytics']['cta_variants']
        self.assertEqual([row['variant'] for row in cta], [0])
        self.assertEqual(cta[0]['videos'], 5)

    def test_analytics_section_is_safe_when_ledger_has_no_new_fields(self):
        result = dashboard.build_overview({'enabled': True}, {},
                                          {'jobs': {'j': {'status': 'uploaded', 'video_id': 'x'}}})
        self.assertEqual(result['analytics'], {'retention': [], 'cta_variants': [], 'playlists': []})


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.server = dashboard.DashboardServer(('127.0.0.1', 0), dashboard.DashboardHandler,
                                                 'never-expose-this', 'owner/repo', 'main', 'test-key')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_api_requires_key_and_config_never_exposes_token(self):
        self.assertEqual(self.request('GET', '/api/config')[0], 401)
        status, _, raw = self.request('GET', '/api/config', headers={'X-Dashboard-Key': 'test-key'})
        self.assertEqual(status, 200)
        self.assertNotIn(b'never-expose-this', raw)
        self.assertFalse(json.loads(raw)['features']['tiktok'])

    def test_invalid_json_shapes_lengths_and_types_do_not_dispatch(self):
        for body, extra in (('[]', {}), ('null', {}), ('{', {}), ('{}', {'Content-Length': '-1'}),
                            ('{}', {'Content-Length': '65537'}), ('{}', {'Content-Type': 'text/plain'})):
            headers = {'X-Dashboard-Key': 'test-key', 'Content-Type': 'application/json', **extra}
            with patch.object(dashboard.GitHubClient, 'dispatch') as dispatch:
                self.assertEqual(self.request('POST', '/api/workflow', body, headers)[0], 400)
                dispatch.assert_not_called()

    def test_cross_site_post_is_rejected_even_without_key(self):
        self.server.dashboard_key = ''
        status, _, _ = self.request('POST', '/api/workflow', '{}',
                                  {'Content-Type': 'application/json', 'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)

    def test_rewritten_dashboard_static_path_resolves_to_dashboard_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'index.html').write_text('<main>dashboard</main>', encoding='utf-8')
            with patch.object(dashboard, 'DASHBOARD_ROOT', root):
                status, _, raw = self.request('GET', '/dashboard/index.html')
        self.assertEqual(status, 200)
        self.assertIn(b'<main>dashboard</main>', raw)

    def test_static_ui_has_csp_and_external_service_worker_registration(self):
        status, headers, raw = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn(b'<script>', raw)
        self.assertEqual(self.request('GET', '/../automation.json')[0], 404)

    def test_dispatch_uses_one_batch_and_blocks_uncertain_upload(self):
        overview = {'channel': {'enabled': True}, 'health': {'uncertain_uploads': []}}
        client = Mock(repository='owner/repo')
        client.dispatch.return_value = 3
        with patch.object(dashboard.DashboardHandler, '_read_model', return_value=(overview, client)):
            status, _, raw = self.request('POST', '/api/workflow', '{"mode":"publish","count":3}',
                {'Content-Type': 'application/json', 'X-Dashboard-Key': 'test-key'})
            self.assertEqual(status, 202)
            self.assertEqual(json.loads(raw)['requested_count'], 3)
            self.assertEqual(json.loads(raw)['dispatched'], 1)
            client.dispatch.assert_called_once_with('publish', 3)
            overview['health']['uncertain_uploads'] = [{'id': 'uncertain'}]
            self.assertEqual(self.request('POST', '/api/workflow', '{"mode":"publish"}',
                {'Content-Type': 'application/json', 'X-Dashboard-Key': 'test-key'})[0], 409)
            self.assertEqual(client.dispatch.call_count, 1)

    def test_clip_submission_requires_direct_media_link_and_theme(self):
        overview = {'channel': {'enabled': True}, 'health': {'uncertain_uploads': []}}
        client = Mock(repository='owner/repo')
        client.write_file.return_value = None
        headers = {'Content-Type': 'application/json', 'X-Dashboard-Key': 'test-key'}
        with patch.object(dashboard.DashboardHandler, '_read_model', return_value=(overview, client)):
            for body in ('{"url":"https://evil.example/page","theme":"forest_rain","title":"T"}',
                         '{"url":"https://x.example/a.mp4","theme":"unknown","title":"T"}',
                         '{"url":"https://x.example/a.mp4","theme":"forest_rain","title":""}'):
                status, _, _ = self.request('POST', '/api/submit-clip', body, headers)
                self.assertEqual(status, 400)
                client.write_file.assert_not_called()
            status, _, raw = self.request('POST', '/api/submit-clip',
                '{"url":"https://videos.pexels.com/video-files/x/y.mp4","theme":"forest_rain","title":"Ocean waves","license_confirmed":true}',
                headers)
            self.assertEqual(status, 202)
            result = json.loads(raw)
            self.assertTrue(result['ok'])
            self.assertEqual(result['status'], 'queued')
            path = client.write_file.call_args[0][0]
            self.assertTrue(path.startswith('.bot-state/clip-submissions/'))
            record = json.loads(client.write_file.call_args[0][1])
            self.assertTrue(record['license_confirmed'])
            client.dispatch.assert_called_once()
            self.assertEqual(record['theme'], 'forest_rain')

    def test_clip_submission_blocked_when_publishing_disabled(self):
        overview = {'channel': {'enabled': False}, 'health': {'uncertain_uploads': []}}
        client = Mock(repository='owner/repo')
        headers = {'Content-Type': 'application/json', 'X-Dashboard-Key': 'test-key'}
        with patch.object(dashboard.DashboardHandler, '_read_model', return_value=(overview, client)):
            status, _, _ = self.request('POST', '/api/submit-clip',
                '{"url":"https://x.example/a.mp4","theme":"forest_rain","title":"T"}', headers)
        self.assertEqual(status, 400)
        client.write_file.assert_not_called()


if __name__ == '__main__':
    unittest.main()
