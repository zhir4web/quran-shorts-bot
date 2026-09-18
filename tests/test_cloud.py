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
             patch.object(cloud.time, 'sleep'), \
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

    def test_verse_rotation_changes_reciter_and_verse(self):
        catalog = {'schema': 3, 'provider': 'quran_foundation', 'reciters': 'allowlist',
                   'allowed_reciter_ids': [1, 2], 'blocked_reciter_ids': [5],
                   'visual_style': 'premium_rotating_scenes',
                   'visual_themes': ['forest_rain', 'mist_mountains', 'starry_night',
                                     'ocean_moon', 'dawn_mosque'],
                   'show_verified_ayah_text': True, 'max_ayah_characters': 180,
                   'content': 'complete_verses', 'max_audio_seconds': 58,
                   'tail_silence_seconds': 1, 'permission_url': 'https://api-docs.quran.com/legal/developer-terms/',
                   'attribution': 'Quran Foundation', 'rights': 'Test rights'}
        reciters = [{'id': 1, 'reciter_name': 'One', 'style': None,
                     'translated_name': {'name': 'One'}},
                    {'id': 2, 'reciter_name': 'Two', 'style': 'Murattal',
                     'translated_name': {'name': 'Two'}}]
        chapters = [{'id': 1, 'verses_count': 6236, 'name_arabic': 'الفاتحة', 'name_simple': 'Al-Fatihah'}]
        def api(url, params=None):
            if 'resources/recitations' in url:
                return {'recitations': reciters}
            if url.endswith('/chapters'):
                return {'chapters': chapters}
            if 'quran/verses/uthmani' in url:
                return {'verses': [{'text_uthmani': 'بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ'}]}
            verse = url.rsplit('/', 1)[-1]
            return {'audio_files': [{'duration': 10, 'url': f'Test/{verse}.mp3'}]}
        with patch.object(cloud, 'get_json', side_effect=api):
            first, _ = cloud.verse_entry_for_position(catalog, {}, 0)
            second, _ = cloud.verse_entry_for_position(catalog, {}, 1)
        self.assertNotEqual(first['reciter_en'], second['reciter_en'])
        self.assertNotEqual(first['verse_key'], second['verse_key'])
        self.assertNotEqual(first['visual_theme'], second['visual_theme'])
        self.assertTrue(first['ayah_text'])

    def test_blocked_reciter_is_never_selected(self):
        catalog = {'schema': 3, 'provider': 'quran_foundation', 'reciters': 'allowlist',
                   'allowed_reciter_ids': [1, 2], 'blocked_reciter_ids': [5],
                   'visual_style': 'premium_rotating_scenes',
                   'visual_themes': ['forest_rain', 'mist_mountains', 'starry_night',
                                     'ocean_moon', 'dawn_mosque'],
                   'show_verified_ayah_text': True, 'max_ayah_characters': 180,
                   'content': 'complete_verses',
                   'max_audio_seconds': 58, 'tail_silence_seconds': 1,
                   'permission_url': 'https://api-docs.quran.com/legal/developer-terms/',
                   'attribution': 'Quran Foundation', 'rights': 'verified'}
        reciters = [{'id': 1, 'reciter_name': 'Safe One'},
                    {'id': 2, 'reciter_name': 'Safe Two'},
                    {'id': 5, 'reciter_name': 'Blocked'}]
        chapters = [{'id': 1, 'verses_count': 6236, 'name_arabic': 'الفاتحة',
                     'name_simple': 'Al-Fatihah'}]
        def api(url, params=None):
            if 'resources/recitations' in url:
                return {'recitations': reciters}
            if url.endswith('/chapters'):
                return {'chapters': chapters}
            if 'quran/verses/uthmani' in url:
                return {'verses': [{'text_uthmani': 'قُلْ هُوَ اللَّهُ أَحَدٌ'}]}
            return {'audio_files': [{'duration': 10, 'url': 'safe/test.mp3'}]}
        with patch.object(cloud, 'get_json', side_effect=api):
            chosen = [cloud.verse_entry_for_position(catalog, {}, pos)[0]['recitation_id']
                      for pos in range(8)]
        self.assertNotIn(5, chosen)

    def test_verse_audio_uses_measured_duration_and_tail(self):
        entry = dict(ENTRY, source_type='quran_verse', audio_url='https://verses.quran.foundation/test.mp3',
                     max_audio_seconds=58, tail_silence_seconds=1)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.raise_for_status = Mock()
        response.iter_content.return_value = [b'complete verse']
        target = self.root / 'recitation.mp3'
        def render(args):
            Path(args[-1]).write_bytes(b'complete verse plus silence')
        with patch.object(cloud.requests, 'get', return_value=response), \
             patch.object(cloud, 'media_duration', side_effect=[10.25, 11.25]), \
             patch.object(bot, 'run_media', side_effect=render):
            cloud.download_quran_verse(entry, target)
        self.assertEqual(entry['duration'], 11.25)
        self.assertTrue(target.is_file())

    def test_overlong_complete_verse_is_skipped_not_trimmed(self):
        entry = dict(ENTRY, source_type='quran_verse', audio_url='https://verses.quran.foundation/test.mp3',
                     max_audio_seconds=58, tail_silence_seconds=1)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.raise_for_status = Mock()
        response.iter_content.return_value = [b'long verse']
        with patch.object(cloud.requests, 'get', return_value=response), \
             patch.object(cloud, 'media_duration', return_value=59):
            with self.assertRaises(cloud.TooLongRecording):
                cloud.download_quran_verse(entry, self.root / 'recitation.mp3')

    def test_bilingual_metadata_has_verified_order_and_no_kurdish(self):
        entry = dict(ENTRY, verse_number=3, verse_key='112:3', ayah_text='لَمْ يَلِدْ وَلَمْ يُولَدْ',
                     reciter_en='Test Reciter', style='Murattal', recitation_id=1)
        job = cloud.item_for(entry, self.root / 'audio.mp3', self.root / 'card.png')
        self.assertIn('سورة الإخلاص، الآية 3', job['title'])
        self.assertIn('Surah Al-Ikhlas, Ayah 3', job['title'])
        parts = job['description'].split('\n\n')
        self.assertEqual(parts[0], entry['ayah_text'])
        self.assertTrue(parts[1].startswith('Beautiful Quran recitation'))
        self.assertEqual(parts[2], entry['attribution'])
        self.assertEqual(parts[3], entry['permission_url'])
        self.assertNotIn('ک', job['description'])

    def test_claim_detection_blocks_reciter_and_records_incident(self):
        self.ledger.data['jobs'] = {'job': {'status': 'uploaded', 'video_id': 'blocked-video',
                                                   'reciter_id': 12}}
        self.service.videos.return_value.list.return_value.execute.return_value = {
            'items': [{'id': 'blocked-video', 'status': {'uploadStatus': 'rejected',
                                                         'rejectionReason': 'copyright'},
                       'contentDetails': {}}]}
        catalog = {'allowed_reciter_ids': [1, 12], 'blocked_reciter_ids': []}
        incidents = cloud.check_upload_restrictions(self.service, self.ledger, catalog)
        self.assertEqual(incidents[0]['reason'], 'copyright')
        self.ledger.block_reciter.assert_called_once_with(catalog, 12)
        self.assertEqual(self.ledger.data['jobs']['job']['safety_incident']['video_id'], 'blocked-video')

    def test_failed_comment_never_breaks_completed_upload(self):
        with patch.object(bot, 'post_cta_comment', return_value=(4, False)):
            self.execute()
        row = self.ledger.data['jobs'][ENTRY['id']]
        self.assertEqual(row['status'], 'uploaded')
        self.assertEqual(row['video_id'], 'video123')
        self.assertFalse(row['cta_comment_succeeded'])


if __name__ == '__main__':
    unittest.main()

