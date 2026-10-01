import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import tiktok_draft


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self.payload = payload or {}

    def json(self):
        return self.payload


class TikTokDraftTests(unittest.TestCase):
    def test_chunk_plan_respects_tiktok_size_rules(self):
        self.assertEqual(tiktok_draft.chunk_plan(3 * 1024 * 1024), (3 * 1024 * 1024, 1))
        size = 70 * 1024 * 1024 + 17
        chunk_size, total = tiktok_draft.chunk_plan(size)
        self.assertEqual(total, 2)
        self.assertGreaterEqual(chunk_size, 5 * 1024 * 1024)
        self.assertLessEqual(chunk_size, 64 * 1024 * 1024)
        self.assertEqual(total, (size + chunk_size - 1) // chunk_size)
        with self.assertRaises(tiktok_draft.TikTokDraftError):
            tiktok_draft.chunk_plan(tiktok_draft.MAX_VIDEO_BYTES + 1)

    def test_upload_uses_inbox_endpoint_and_sends_file_without_publishing(self):
        session = Mock()
        session.post.side_effect = [
            Response(200, {'data': {'publish_id': 'draft~id',
                                    'upload_url': 'https://open-upload.tiktokapis.com/video?upload_id=x'},
                           'error': {'code': 'ok'}}),
            Response(200, {'data': {'status': 'SEND_TO_USER_INBOX'}, 'error': {'code': 'ok'}}),
        ]
        session.put.return_value = Response(201)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.write_bytes(b'video-bytes')
            initialized = []
            client = tiktok_draft.TikTokClient('private-access-token', session=session, sleep=lambda _: None)
            result = client.upload(path, on_initialized=initialized.append)
        self.assertEqual(result, ('draft~id', 'SEND_TO_USER_INBOX'))
        self.assertEqual(initialized, ['draft~id'])
        init_call = session.post.call_args_list[0]
        self.assertTrue(init_call.args[0].endswith('/post/publish/inbox/video/init/'))
        self.assertEqual(init_call.kwargs['json']['source_info']['source'], 'FILE_UPLOAD')
        upload = session.put.call_args
        self.assertEqual(upload.kwargs['headers']['Content-Range'], 'bytes 0-10/11')
        self.assertNotIn('video.publish', repr(init_call.kwargs))
        self.assertFalse(any('/post/publish/content/init/' in call.args[0] for call in session.post.call_args_list))

    def test_upload_rejects_untrusted_destination_without_sending_media(self):
        session = Mock()
        session.post.return_value = Response(200, {'data': {'publish_id': 'draft~id',
            'upload_url': 'https://attacker.example/upload'}, 'error': {'code': 'ok'}})
        client = tiktok_draft.TikTokClient('private-access-token', session=session, sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.write_bytes(b'video')
            with self.assertRaisesRegex(tiktok_draft.TikTokDraftError, 'invalid upload destination'):
                client.upload(path)
        session.put.assert_not_called()

    def test_upload_waits_for_async_processing_before_reporting_inbox_delivery(self):
        session = Mock()
        session.post.side_effect = [
            Response(200, {'data': {'publish_id': 'draft~id',
                                    'upload_url': 'https://open-upload.tiktokapis.com/video?upload_id=x'},
                           'error': {'code': 'ok'}}),
            *[Response(200, {'data': {'status': 'PROCESSING_UPLOAD'}, 'error': {'code': 'ok'}})
              for _ in range(5)],
            Response(200, {'data': {'status': 'SEND_TO_USER_INBOX'}, 'error': {'code': 'ok'}}),
        ]
        session.put.return_value = Response(201)
        sleeps = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.write_bytes(b'video-bytes')
            client = tiktok_draft.TikTokClient('private-access-token', session=session, sleep=sleeps.append)
            result = client.upload(path)
        self.assertEqual(result, ('draft~id', 'SEND_TO_USER_INBOX'))
        self.assertEqual(sleeps, [tiktok_draft.STATUS_POLL_INTERVAL_SECONDS] * 5)
        self.assertEqual(session.post.call_count, 7)

    def test_duplicate_workflow_run_is_blocked_before_second_upload(self):
        session = Mock()
        state = {'schema': 1, 'runs': {'123': {'status': 'send_to_user_inbox', 'publish_id': 'draft~id'}}}
        session.get.return_value = Response(200, {'content': __import__('base64').b64encode(
            json.dumps(state).encode()).decode(), 'sha': 'sha-1'})
        ledger = tiktok_draft.GitHubDraftLedger('github-token', 'owner/repo', 'main', session=session)
        with self.assertRaisesRegex(tiktok_draft.TikTokDraftError, 'prevent a duplicate'):
            ledger.claim('123', 'abc123', 'now')
        session.put.assert_not_called()

    def test_duplicate_scheduled_slot_is_blocked_across_workflow_runs(self):
        session = Mock()
        state = {'schema': 1, 'runs': {'123': {'status': 'send_to_user_inbox',
            'schedule_slot': '2026-10-01/06:00'}}}
        session.get.return_value = Response(200, {'content': __import__('base64').b64encode(
            json.dumps(state).encode()).decode(), 'sha': 'sha-1'})
        ledger = tiktok_draft.GitHubDraftLedger('github-token', 'owner/repo', 'main', session=session)
        with self.assertRaisesRegex(tiktok_draft.TikTokDraftError, 'schedule slot already has an upload attempt'):
            ledger.claim('124', 'video-hash', 'now', schedule_slot='2026-10-01/06:00')
        session.put.assert_not_called()

    def test_generated_video_duration_is_checked_before_auth_or_upload(self):
        session = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.write_bytes(b'video')
            with unittest.mock.patch.object(tiktok_draft, 'latest_video', return_value=path):
                with self.assertRaisesRegex(tiktok_draft.TikTokDraftError, 'at least 61 seconds'):
                    tiktok_draft.upload_latest(session=session, env={'GITHUB_RUN_ID': '123'},
                                               duration_reader=lambda _: 60.9)
        session.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
