import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

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
        self.assertTrue(overview['integrations']['youtube'])

    def test_dispatch_uses_workflow_input_without_exposing_token(self):
        client = dashboard.GitHubClient('secret-token', 'owner/repo', 'main')
        response = Mock(status_code=204)
        client.session.post = Mock(return_value=response)
        self.assertEqual(client.dispatch('publish', 2), 2)
        self.assertEqual(client.session.post.call_count, 2)
        body = client.session.post.call_args.kwargs['json']
        self.assertEqual(body, {'ref': 'main', 'inputs': {'mode': 'publish'}})
        self.assertNotIn('secret-token', repr(body))

    def test_dispatch_rejects_unknown_mode(self):
        client = dashboard.GitHubClient('secret-token', 'owner/repo')
        with self.assertRaises(dashboard.DashboardError):
            client.dispatch('delete', 1)


if __name__ == '__main__':
    unittest.main()

