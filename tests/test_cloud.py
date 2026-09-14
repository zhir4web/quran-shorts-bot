import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import bot
import cloud_runner as cloud


ENTRY = dict(id='sample-112', verified=True, whole_recording=True,
             audio_url='https://example.com/112.mp3', permission_url='https://example.com/license',
             surah_ar='الإخلاص', surah_en='Al-Ikhlas', reciter_ar='اسم القارئ',
             attribution='Test fixture only', rights='Test fixture only',
             sha256='a' * 64, duration=20)


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.config = {'enabled': True, 'channel_id': 'expected', 'privacy': 'private'}
        self.catalog = {'schema': 1, 'recordings': [copy.deepcopy(ENTRY)]}
        self.persist()
        self.root_patch = patch.object(cloud, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.ledger = Mock(data={'schema': 1, 'jobs': {}})
        self.service = Mock()
        self.service.channels.return_value.list.return_value.execute.return_value = {'items': [{'id': 'expected'}]}

    def persist(self):
        bot.atomic_json(self.root / 'automation.json', self.config)
        bot.atomic_json(self.root / 'catalog.json', self.catalog)

    def render(self, job, root, folder):
        target = folder / 'video.mp4'
        target.write_bytes(b'test media')
        return target

    def execute(self, mode='publish', upload=None):
        with patch.object(cloud, 'download_recording', side_effect=lambda e, p: p), \
             patch.object(cloud, 'make_card', side_effect=lambda e, p: p), \
             patch.object(bot, 'render', side_effect=self.render), \
             patch.object(bot, 'upload', upload or Mock(return_value='video123')) as uploader:
            cloud.run(SimpleNamespace(mode=mode), self.ledger, self.service)
            return uploader

    def test_write_ahead_then_upload_then_completion(self):
        events = []
        self.ledger.save.side_effect = lambda: events.append(self.ledger.data['jobs'][ENTRY['id']]['status'])
        upload = Mock(side_effect=lambda *a: events.append('youtube') or 'video123')
        self.execute(upload=upload)
        self.assertEqual(events, ['uploading', 'youtube', 'uploaded'])
        self.assertEqual(self.ledger.data['jobs'][ENTRY['id']]['video_id'], 'video123')

    def test_failed_initial_save_never_uploads(self):
        self.ledger.save.side_effect = RuntimeError('unavailable')
        uploader = Mock()
        with self.assertRaises(RuntimeError):
            self.execute(upload=uploader)
        uploader.assert_not_called()

    def test_ambiguous_upload_stops_next_run(self):
        with self.assertRaises(TimeoutError):
            self.execute(upload=Mock(side_effect=TimeoutError()))
        uploader = Mock()
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            self.execute(upload=uploader)
        uploader.assert_not_called()

    def test_completed_recording_is_skipped(self):
        self.execute()
        uploader = self.execute()
        uploader.assert_not_called()

    def test_wrong_channel_never_uploads_or_reserves(self):
        self.service.channels.return_value.list.return_value.execute.return_value = {'items': [{'id': 'wrong'}]}
        uploader = Mock()
        with self.assertRaisesRegex(RuntimeError, 'channel'):
            self.execute(upload=uploader)
        uploader.assert_not_called()
        self.ledger.save.assert_not_called()

    def test_preview_never_uploads_or_reserves(self):
        uploader = self.execute(mode='preview')
        uploader.assert_not_called()
        self.ledger.save.assert_not_called()

    def test_disabled_configuration_blocks_publication(self):
        self.config['enabled'] = False
        self.persist()
        with self.assertRaisesRegex(RuntimeError, 'not activated'):
            self.execute()

    def test_unverified_catalog_rejected(self):
        self.catalog['recordings'][0]['verified'] = False
        self.persist()
        with self.assertRaisesRegex(ValueError, 'verification'):
            self.execute()

    def test_changed_checksum_rejected(self):
        self.execute()
        self.catalog['recordings'][0]['sha256'] = 'b' * 64
        self.persist()
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            self.execute()

    def test_download_rejects_changed_audio(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [b'changed recording']
        with patch.object(cloud.requests, 'get', return_value=response):
            with self.assertRaisesRegex(ValueError, 'Audio changed'):
                cloud.download_recording(ENTRY, self.root / 'audio.mp3')
        self.assertFalse((self.root / 'audio.mp3').exists())

    def test_remote_ledger_rejects_unknown_status(self):
        ledger = cloud.RemoteLedger('owner/repo', 'fake-test-token')
        payload = {'schema': 1, 'jobs': {'x': {'status': 'mystery'}}}
        ledger.session = Mock()
        ledger.session.get.return_value.status_code = 200
        ledger.session.get.return_value.json.return_value = {
            'sha': 'abc', 'content': base64.b64encode(json.dumps(payload).encode()).decode()}
        with self.assertRaisesRegex(ValueError, 'invalid job'):
            ledger.load()


if __name__ == '__main__':
    unittest.main()
