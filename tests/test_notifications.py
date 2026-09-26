import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import bot
import cloud_runner as cloud
import notifications
from notifications import Config, heartbeat, main, notify


ENTRY = dict(id='sample-112', verified=True, whole_recording=True,
             audio_url='https://example.com/112.mp3', permission_url='https://example.com/license',
             surah_ar='الإخلاص', surah_en='Al-Ikhlas', reciter_ar='اسم القارئ',
             reciter_en='Test Reciter',
             attribution='Test fixture only', rights='Test fixture only',
             sha256='a' * 64, duration=35)


def env(**overrides):
    base = {'DISCORD_WEBHOOK_URL': 'https://discord.example/hook',
            'HEARTBEAT_URL': 'https://hc.example/ping/uuid'}
    base.update(overrides)
    return base


def session(status=200):
    client = Mock()
    client.post.return_value = Mock(status_code=status)
    client.get.return_value = Mock(status_code=status)
    return client


class NotificationTests(unittest.TestCase):
    def setUp(self):
        notifications._DELIVERED.clear()

    def test_unconfigured_environment_makes_no_requests(self):
        client = session()
        self.assertEqual(notify('hello', env={}, session=client), [])
        client.post.assert_not_called()

    def test_discord_message_uses_webhook_content(self):
        client = session()
        delivered = notify('Render failed', kind='failure', env=env(), session=client)
        self.assertEqual(delivered, ['discord'])
        call = client.post.call_args
        url, body = call[0][0], call[1]['json']
        self.assertEqual(url, 'https://discord.example/hook')
        self.assertIn('Render failed', body['content'])
        self.assertIn('❌', body['content'])

    def test_plain_http_webhook_is_rejected_without_a_request(self):
        client = session()
        delivered = notify('x', env=env(DISCORD_WEBHOOK_URL='http://discord.example/hook'),
                           session=client)
        self.assertEqual(delivered, [])
        client.post.assert_not_called()

    def test_network_failure_is_swallowed(self):
        client = session()
        client.post.side_effect = TimeoutError()
        self.assertEqual(notify('x', env=env(), session=client), [])

    def test_http_error_from_channel_is_swallowed(self):
        client = session(status=500)
        self.assertEqual(notify('x', env=env(), session=client), [])

    def test_keyed_events_are_delivered_once_per_process(self):
        client = session()
        self.assertEqual(notify('x', key='upload:a', env=env(), session=client), ['discord'])
        self.assertEqual(notify('x', key='upload:a', env=env(), session=client), [])
        self.assertEqual(client.post.call_count, 1)

    def test_unkeyed_events_repeat_every_time(self):
        client = session()
        notify('x', env=env(), session=client)
        notify('x', env=env(), session=client)
        self.assertEqual(client.post.call_count, 2)

    def test_config_channel_flags(self):
        self.assertTrue(Config(env=env()).discord_ready)
        self.assertFalse(Config(env=env(DISCORD_WEBHOOK_URL='http://x/hook')).discord_ready)
        self.assertTrue(Config(env=env()).heartbeat_ready)
        self.assertFalse(Config(env={}).heartbeat_ready)

    def test_heartbeat_pings_success_url(self):
        client = session()
        self.assertTrue(heartbeat(ok=True, env=env(), session=client))
        client.get.assert_called_once_with('https://hc.example/ping/uuid',
                                           timeout=notifications.REQUEST_TIMEOUT)

    def test_heartbeat_failure_appends_fail_suffix(self):
        client = session()
        self.assertTrue(heartbeat(ok=False, env=env(), session=client))
        client.get.assert_called_once_with('https://hc.example/ping/uuid/fail',
                                           timeout=notifications.REQUEST_TIMEOUT)

    def test_heartbeat_requires_https(self):
        client = session()
        self.assertFalse(heartbeat(ok=True, env=env(HEARTBEAT_URL='http://hc.example/ping'),
                                   session=client))
        client.get.assert_not_called()

    def test_heartbeat_errors_are_swallowed(self):
        client = session()
        client.get.side_effect = ConnectionError()
        self.assertFalse(heartbeat(ok=True, env=env(), session=client))

    def test_run_status_cli_reports_failures_and_pings_fail(self):
        with patch.object(notifications, 'heartbeat', return_value=True) as beat, \
             patch.object(notifications, 'notify', return_value=[]) as send:
            code = main(['run-status', '--status', 'failure', '--context', 'mode=scheduled'])
        self.assertEqual(code, 0)
        self.assertIn('mode=scheduled', send.call_args[0][0])
        self.assertEqual(send.call_args[1]['kind'], 'failure')
        beat.assert_called_once_with(ok=False)

    def test_run_status_cli_success_only_pings_heartbeat(self):
        with patch.object(notifications, 'heartbeat', return_value=True) as beat, \
             patch.object(notifications, 'notify', return_value=[]) as send:
            code = main(['run-status', '--status', 'success'])
        self.assertEqual(code, 0)
        send.assert_not_called()
        beat.assert_called_once_with(ok=True)

    def test_cli_never_fails_the_workflow(self):
        with patch.object(notifications, 'heartbeat', side_effect=RuntimeError('boom')), \
             patch.object(notifications, 'notify', side_effect=RuntimeError('boom')):
            self.assertEqual(main(['run-status', '--status', 'success']), 0)


class CloudHookTests(unittest.TestCase):
    """Notifications fired by the cloud runner's own event points."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        bot.atomic_json(self.root / 'automation.json',
                        {'enabled': True, 'channel_id': 'expected', 'privacy': 'private'})
        bot.atomic_json(self.root / 'catalog.json', {'schema': 1, 'recordings': [dict(ENTRY)]})
        patcher = patch.object(cloud, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ledger = Mock(data={'schema': 1, 'jobs': {}})
        self.service = Mock()
        self.service.channels.return_value.list.return_value.execute.return_value = {
            'items': [{'id': 'expected'}]}

    def render(self, job, root, folder):
        target = folder / 'video.mp4'
        target.write_bytes(b'test media')
        return target

    def execute_publish(self):
        with patch.object(cloud, 'download_recording', side_effect=lambda e, p: p), \
             patch.object(cloud, 'make_card', side_effect=lambda e, p: p), \
             patch.object(cloud, 'make_motion_overlay', side_effect=lambda e, p: p), \
             patch.object(bot, 'render', side_effect=self.render), \
             patch.object(cloud.time, 'sleep'), \
             patch.object(cloud, 'media_duration', side_effect=[35, 35]), \
             patch.object(bot, 'upload', return_value='video123'):
            return cloud.run(SimpleNamespace(mode='publish'), self.ledger, self.service)

    def test_successful_upload_sends_success_notification(self):
        with patch.object(notifications, 'notify') as send:
            self.execute_publish()
        send.assert_called_once()
        kwargs = send.call_args[1]
        self.assertEqual(kwargs['kind'], 'success')
        self.assertEqual(kwargs['key'], 'upload:' + ENTRY['id'])
        self.assertIn('video123', send.call_args[0][0])
        self.assertIn('Al-Ikhlas', send.call_args[0][0])

    def test_copyright_takedown_sends_safety_notification(self):
        self.ledger.data['jobs']['job1'] = {'status': 'uploaded', 'video_id': 'vid1',
                                            'reciter_id': 3}
        self.service.videos.return_value.list.return_value.execute.return_value = {
            'items': [{'id': 'vid1', 'status': {'uploadStatus': 'rejected',
                                                'rejectionReason': 'copyright'}}]}
        catalog = {'allowed_reciter_ids': [1, 2, 3], 'blocked_reciter_ids': []}
        with patch.object(notifications, 'notify') as send:
            incidents = cloud.check_upload_restrictions(self.service, self.ledger, catalog)
        self.assertEqual(len(incidents), 1)
        self.assertTrue(incidents[0]['reciter_blocked'])
        send.assert_called_once()
        self.assertEqual(send.call_args[1]['kind'], 'safety')
        self.assertEqual(send.call_args[1]['key'], 'safety:vid1:copyright')

    def test_health_flags_send_health_notifications(self):
        report = {'tracked': 1,
                  'new_flags': [{'video_id': 'vid9', 'flag': 'video unavailable'}],
                  'averages': {'reciters': {}, 'visual_themes': {}},
                  'schedule': None, 'timestamp': 'now'}
        with patch.object(cloud, 'collect_performance_metrics', return_value=report), \
             patch.object(notifications, 'notify') as send:
            summary = cloud.run_performance_report(self.service, self.ledger)
        self.assertIn('performance report', summary)
        send.assert_called_once()
        self.assertEqual(send.call_args[1]['kind'], 'health')
        self.assertEqual(send.call_args[1]['key'], 'health:vid9:video unavailable')

    def test_cloud_failure_notifies_for_scheduled_mode(self):
        credentials = Mock()
        credentials.valid = True
        token_env = json.dumps({'refresh_token': 'x', 'client_id': 'x', 'client_secret': 'x'})
        with patch.dict(os.environ, {'YOUTUBE_TOKEN': token_env, 'GITHUB_TOKEN': 'g',
                                     'GITHUB_REPOSITORY': 'o/r', 'GITHUB_REF_NAME': 'main'}), \
             patch('google.oauth2.credentials.Credentials') as creds, \
             patch('googleapiclient.discovery.build'), \
             patch.object(cloud, 'RemoteLedger'), \
             patch.object(cloud, 'run', side_effect=cloud.CloudError('An earlier upload is uncertain')), \
             patch.object(notifications, 'notify') as send:
            creds.from_authorized_user_info.return_value = credentials
            code = cloud.main(['scheduled'])
        self.assertEqual(code, 1)
        send.assert_called_once()
        self.assertEqual(send.call_args[1]['kind'], 'failure')
        self.assertEqual(send.call_args[1]['key'], 'run-failure:scheduled')
        self.assertIn('An earlier upload is uncertain', send.call_args[0][0])

    def test_cloud_failure_hides_details_for_unknown_errors(self):
        credentials = Mock()
        credentials.valid = True
        token_env = json.dumps({'refresh_token': 'x', 'client_id': 'x', 'client_secret': 'x'})
        with patch.dict(os.environ, {'YOUTUBE_TOKEN': token_env, 'GITHUB_TOKEN': 'g',
                                     'GITHUB_REPOSITORY': 'o/r', 'GITHUB_REF_NAME': 'main'}), \
             patch('google.oauth2.credentials.Credentials') as creds, \
             patch('googleapiclient.discovery.build'), \
             patch.object(cloud, 'RemoteLedger'), \
             patch.object(cloud, 'run', side_effect=RuntimeError('secret payload')), \
             patch.object(notifications, 'notify') as send:
            creds.from_authorized_user_info.return_value = credentials
            code = cloud.main(['scheduled'])
        self.assertEqual(code, 1)
        self.assertNotIn('secret payload', send.call_args[0][0])
        self.assertIn('RuntimeError', send.call_args[0][0])

    def test_preview_failure_does_not_notify_in_process(self):
        with patch.object(cloud, 'run', side_effect=cloud.CloudError('not due')), \
             patch.object(notifications, 'notify') as send:
            code = cloud.main(['preview'])
        self.assertEqual(code, 1)
        send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
