self.assertEqual(len(catalog['visual_themes']), 5)import base64
import copy
from datetime import datetime, timedelta, timezone
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
             sha256='a' * 64, duration=35)


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

    def execute(self, mode='publish', upload=None, measured=None):
        with patch.object(cloud, 'download_recording', side_effect=lambda e, p: p), \
             patch.object(cloud, 'make_card', side_effect=lambda e, p: p), \
             patch.object(bot, 'render', side_effect=self.render), \
             patch.object(cloud.time, 'sleep'), \
             patch.object(cloud, 'media_duration', side_effect=measured or [35, 35]), \
             patch.object(bot, 'upload', upload or Mock(return_value='video123')) as uploader:
            cloud.run(SimpleNamespace(mode=mode), self.ledger, self.service)
            return uploader


    def test_short_source_and_short_final_video_never_upload(self):
        for durations in ([6], [29.99], [35, 6], [35, 29.99]):
            with self.subTest(durations=durations):
                uploader = Mock()
                with self.assertRaises(cloud.TooShortRecording):
                    self.execute(upload=uploader, measured=durations)
                uploader.assert_not_called()
                self.ledger.save.assert_not_called()

    def test_measured_short_verse_never_gets_silence_padding(self):
        entry = dict(ENTRY, audio_url='https://example.com/verse.mp3',
                     max_audio_seconds=58, tail_silence_seconds=1)
        response = Mock()
        response.status_code = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [b'audio']
        with patch.object(cloud.requests, 'get', return_value=response), \
             patch.object(cloud, 'media_duration', return_value=6), \
             patch.object(bot, 'run_media') as media:
            with self.assertRaises(cloud.TooShortRecording):
                cloud.download_quran_verse(entry, self.root / 'short.mp3')
        media.assert_not_called()

    def test_exactly_thirty_seconds_is_allowed(self):
        uploader = self.execute(measured=[30, 30])
        uploader.assert_called_once()

    def test_final_frame_metadata_enforces_minimum_without_tolerance(self):
        for seconds in (6, 29.99, 30):
            with self.subTest(seconds=seconds), \
                 patch('imageio_ffmpeg.read_frames', return_value=(x for x in [
                     {'size': (1080, 1920), 'duration': seconds}])), \
                 patch.object(bot, 'run_media'):
                if seconds < 30:
                    with self.assertRaisesRegex(ValueError, 'minimum'):
                        bot.validate_video(self.root / 'video.mp4', 30, minimum=30)
                else:
                    bot.validate_video(self.root / 'video.mp4', 30, minimum=30)

    def test_six_second_api_candidate_is_skipped(self):
        catalog = {'allowed_reciter_ids': [1], 'max_audio_seconds': 58,
                   'tail_silence_seconds': 1, 'max_ayah_characters': 180,
                   'permission_url': 'https://example.com/license', 'attribution': 'test',
                   'rights': 'test', 'visual_style': 'premium_rotating_scenes',
                   'visual_themes': ['forest_rain']}
        def api(url, params=None):
            if 'resources/recitations' in url:
                return {'recitations': [{'id': 1, 'reciter_name': 'Test'}]}
            if url.endswith('/chapters'):
                return {'chapters': [{'id': 1, 'verses_count': 6236,
                                     'name_arabic': 'Test', 'name_simple': 'Test'}]}
            if 'quran/verses/uthmani' in url:
                return {'verses': [{'text_uthmani': 'test text'}]}
            return {'audio_files': [{'duration': 6 if url.endswith('1:1') else 35,
                                     'url': 'test.mp3'}]}
        with patch.object(cloud, 'get_json', side_effect=api):
            entry, cursor = cloud.verse_entry_for_position(catalog, {}, 0)
        self.assertEqual(entry['verse_key'], '1:2')
        self.assertEqual(cursor, 2)
        self.assertEqual(entry['min_audio_seconds'], 30)

    def test_write_ahead_then_upload_then_completion(self):
        events = []
        self.ledger.save.side_effect = lambda: events.append(self.ledger.data['jobs'][ENTRY['id']]['status'])
        upload = Mock(side_effect=lambda *a: events.append('youtube') or 'video123')
        self.execute(upload=upload)
        self.assertEqual(events, ['uploading', 'youtube', 'uploaded', 'uploaded'])
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

    def test_published_video_joins_surah_playlist(self):
        service = self.service
        service.playlists.return_value.list.return_value.execute.return_value = {'items': []}
        service.playlists.return_value.insert.return_value.execute.return_value = {'id': 'PL-run'}
        service.playlistItems.return_value.list.return_value.execute.return_value = {'items': []}
        self.execute()
        body = service.playlistItems.return_value.insert.call_args[1]['body']
        self.assertEqual(body['snippet']['resourceId'],
                         {'kind': 'youtube#video', 'videoId': 'video123'})
        self.assertEqual(self.ledger.data['playlists']['Al-Ikhlas'], 'PL-run')

    def test_playlist_failure_does_not_affect_the_upload(self):
        service = self.service
        service.playlists.return_value.list.return_value.execute.side_effect = RuntimeError('down')
        self.execute()
        row = self.ledger.data['jobs'][ENTRY['id']]
        self.assertEqual(row['status'], 'uploaded')
        self.assertEqual(row['video_id'], 'video123')

    def test_invalid_playlist_privacy_fails_closed(self):
        self.config['playlist_privacy'] = 'weird'
        self.persist()
        with self.assertRaisesRegex(cloud.CloudError, 'playlist privacy'):
            self.execute()

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
            return {'audio_files': [{'duration': 35, 'url': f'Test/{verse}.mp3'}]}
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
            return {'audio_files': [{'duration': 35, 'url': 'safe/test.mp3'}]}
        with patch.object(cloud, 'get_json', side_effect=api):
            chosen = [cloud.verse_entry_for_position(catalog, {}, pos)[0]['recitation_id']
                      for pos in range(8)]
        self.assertNotIn(5, chosen)

    def test_verse_audio_uses_measured_duration_and_tail(self):
        entry = dict(ENTRY, source_type='quran_verse', audio_url='https://verses.quran.foundation/test.mp3',
                     max_audio_seconds=58, tail_silence_seconds=1)
        response = Mock()
        response.status_code = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.raise_for_status = Mock()
        response.iter_content.return_value = [b'complete verse']
        target = self.root / 'recitation.mp3'
        def render(args):
            Path(args[-1]).write_bytes(b'complete verse plus silence')
        with patch.object(cloud.requests, 'get', return_value=response), \
             patch.object(cloud, 'media_duration', side_effect=[30.25, 31.25]), \
             patch.object(bot, 'run_media', side_effect=render):
            cloud.download_quran_verse(entry, target)
        self.assertEqual(entry['duration'], 31.25)
        self.assertTrue(target.is_file())

    def test_overlong_complete_verse_is_skipped_not_trimmed(self):
        entry = dict(ENTRY, source_type='quran_verse', audio_url='https://verses.quran.foundation/test.mp3',
                     max_audio_seconds=58, tail_silence_seconds=1)
        response = Mock()
        response.status_code = 200
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

    def test_ayah_range_is_used_in_title_description_and_metadata(self):
        entry = dict(ENTRY, verse_number=141, verse_key='7:141-142',
                     ayah_text='test range text', reciter_en='Test Reciter',
                     style='Murattal', recitation_id=1)
        job = cloud.item_for(entry, self.root / 'audio.mp3', self.root / 'card.png')
        self.assertIn('الآيات 141–142', job['title'])
        self.assertIn('Ayahs 141–142', job['title'])
        self.assertIn('Ayahs 141–142', job['description'])
        self.assertEqual((job['verse_start'], job['verse_end']), (141, 142))

    def test_all_catalog_themes_have_checked_in_filmed_assets(self):
        repository_root = Path(__file__).resolve().parents[1]
        catalog = bot.read_json(repository_root / 'catalog.json')
        video_root = repository_root / 'assets' / 'backgrounds' / 'video'
        self.assertEqual(len(catalog['visual_themes']), 5)
        for theme in catalog['visual_themes']:
            with self.subTest(theme=theme):
                self.assertTrue((video_root / f'{theme}.mp4').is_file())

    def test_missing_reviewed_theme_fails_closed(self):
        entry = dict(ENTRY, visual_theme='starry_night', visual_style='real_video_assets')
        with self.assertRaisesRegex(FileNotFoundError, 'background missing'):
            cloud.item_for(entry, self.root / 'audio.mp3', self.root / 'card.png')

    def test_card_is_transparent_arabic_overlay(self):
        from PIL import Image
        font_source = Path(__file__).resolve().parents[1] / 'assets' / 'Amiri-Regular.ttf'
        font_target = self.root / 'assets' / 'Amiri-Regular.ttf'
        font_target.parent.mkdir(parents=True)
        font_target.write_bytes(font_source.read_bytes())
        entry = dict(ENTRY, verse_number=3, ayah_text='لَمْ يَلِدْ وَلَمْ يُولَدْ')
        card = cloud.make_card(entry, self.root / 'card.png')
        image = Image.open(card)
        self.assertEqual(image.mode, 'RGBA')
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertGreater(image.getpixel((100, 600))[3], 0)

    def test_real_video_background_is_selected_for_theme(self):
        with tempfile.TemporaryDirectory() as temp:
            original = cloud.ROOT
            try:
                cloud.ROOT = Path(temp)
                asset = Path(temp) / 'assets' / 'backgrounds' / 'video'
                asset.mkdir(parents=True)
                (asset / 'forest_rain.mp4').write_bytes(b'video')
                entry = dict(ENTRY, visual_theme='forest_rain', duration=35)
                job = cloud.item_for(entry, Path(temp) / 'audio.mp3', Path(temp) / 'card.png')
                self.assertEqual(job['background_motion'], 'real_video')
                self.assertTrue(job['background_video'].endswith('forest_rain.mp4'))
                self.assertNotIn('motion_overlay', job)
            finally:
                cloud.ROOT = original

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

    def test_metrics_collection_appends_history_and_averages(self):
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        self.ledger.data = {'schema': 1, 'jobs': {
            'job': {'status': 'uploaded', 'video_id': 'video123', 'reciter_id': 4,
                    'reciter_name': 'Abu Bakr al-Shatri', 'visual_theme': 'forest_rain',
                    'uploaded_at': (now - timedelta(hours=2)).isoformat()}}}
        self.service.videos.return_value.list.return_value.execute.return_value = {
            'items': [{'id': 'video123', 'statistics': {'viewCount': '120', 'likeCount': '14',
                                                        'commentCount': '3'},
                       'status': {'privacyStatus': 'public'}}]}
        report = cloud.collect_performance_metrics(self.service, self.ledger, 'public', now=now)
        snapshot = self.ledger.data['metrics_history']['video123'][0]
        self.assertEqual(snapshot['view_count'], 120)
        self.assertEqual(snapshot['like_count'], 14)
        self.assertEqual(report['averages']['reciters']['Abu Bakr al-Shatri'], 120.0)
        self.assertEqual(report['averages']['visual_themes']['forest_rain'], 120.0)
        self.ledger.save.assert_called_once()

    def test_no_traction_and_visibility_flags(self):
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        self.ledger.data = {'schema': 1, 'jobs': {
            'job': {'status': 'uploaded', 'video_id': 'quiet',
                    'uploaded_at': (now - timedelta(hours=49)).isoformat()}}}
        self.service.videos.return_value.list.return_value.execute.return_value = {
            'items': [{'id': 'quiet', 'statistics': {'viewCount': '0'},
                       'status': {'privacyStatus': 'private'}}]}
        report = cloud.collect_performance_metrics(self.service, self.ledger, 'public', now=now)
        flags = {row['flag'] for row in report['new_flags']}
        self.assertEqual(flags, {'no traction', 'visibility problem'})
        self.assertEqual(set(self.ledger.data['health_flags']['quiet']), flags)

    def test_performance_summary_is_readable(self):
        report = {'timestamp': '2026-09-18T00:00:00+00:00', 'tracked': 2,
                  'new_flags': [{'video_id': 'abc', 'flag': 'no traction'}],
                  'averages': {'reciters': {'Reader': 42.5},
                               'visual_themes': {'starry_night': 55.0}},
                  'schedule': {'window_days': 7, 'heartbeat_runs': 20, 'uploads': 3,
                               'unresolved': 0, 'average_offset_minutes': 18.0,
                               'max_offset_minutes': 42.0, 'delayed_uploads': 1}}
        text = cloud.performance_summary(report)
        self.assertIn('Total uploaded videos tracked: 2', text)
        self.assertIn('https://www.youtube.com/watch?v=abc', text)
        self.assertIn('Reader: 42.5', text)
        self.assertIn('starry_night: 55.0', text)
        self.assertIn('Average target-to-upload offset: 18.0 minutes', text)

    def test_failed_statistics_api_is_nonfatal(self):
        self.ledger.data = {'schema': 1, 'jobs': {
            'job': {'status': 'uploaded', 'video_id': 'video123'}}}
        self.service.videos.return_value.list.return_value.execute.side_effect = RuntimeError('temporary')
        with patch.object(cloud, 'ROOT', self.root):
            text = cloud.run_performance_report(self.service, self.ledger)
        self.assertIn('temporarily unavailable', text)




class ScheduleTests(unittest.TestCase):
    def now(self, hour, minute=0, day=18):
        return datetime(2026, 9, day, hour, minute, tzinfo=cloud.BAGHDAD)

    def row(self, hour=5, slot='2026-09-18/05:00'):
        return {'status': 'uploaded', 'video_id': 'test',
                'uploaded_at': self.now(hour).isoformat(), 'schedule_slot': slot}

    def test_baghdad_boundary_and_utc_conversion(self):
        self.assertIsNone(cloud.next_schedule_slot({}, self.now(4, 59)))
        self.assertEqual(cloud.next_schedule_slot({}, self.now(5).astimezone(timezone.utc)),
                         '2026-09-18/05:00')
        self.assertEqual(cloud.next_schedule_slot({}, self.now(22)),
                         '2026-09-18/05:00')
        self.assertIsNone(cloud.next_schedule_slot({}, self.now(2)))

    def test_late_heartbeat_catches_up_after_final_target(self):
        self.assertEqual(cloud.next_schedule_slot({}, self.now(23, 45)),
                         '2026-09-18/05:00')

    def test_duplicate_triggers_wait_for_next_slot(self):
        jobs = {'a': self.row()}
        self.assertIsNone(cloud.next_schedule_slot(jobs, self.now(8)))
        self.assertEqual(cloud.next_schedule_slot(jobs, self.now(10)), '2026-09-18/09:00')

    def test_delayed_run_catches_oldest_slot_and_daily_cap(self):
        jobs = {}
        for hour in (5, 9, 15):
            slot = f'2026-09-18/{hour:02d}:00'
            self.assertEqual(cloud.next_schedule_slot(jobs, self.now(21)), slot)
            jobs[str(hour)] = self.row(hour, slot)
        self.assertIsNone(cloud.next_schedule_slot(jobs, self.now(21)))
        self.assertEqual(cloud.next_schedule_slot(jobs, self.now(11, day=19)),
                         '2026-09-19/05:00')

    def test_legacy_daytime_upload_counts_but_overnight_test_does_not(self):
        self.assertIsNone(cloud.next_schedule_slot({'a': self.row(slot='')}, self.now(8)))
        self.assertEqual(cloud.next_schedule_slot({'a': self.row(3, '')}, self.now(8)),
                         '2026-09-18/05:00')

    def test_backlog_uploads_have_spacing(self):
        jobs = {'a': self.row(15)}
        self.assertIsNone(cloud.next_schedule_slot(jobs, self.now(15, 19)))
        self.assertEqual(cloud.next_schedule_slot(jobs, self.now(15, 20)),
                         '2026-09-18/09:00')

    def test_malformed_timestamp_fails_closed(self):
        row = self.row()
        row['uploaded_at'] = 'not-a-date'
        with self.assertRaises(cloud.CloudError):
            cloud.next_schedule_slot({'a': row}, self.now(14))

    def test_schedule_timing_summary_reports_recent_offset(self):
        target = self.now(5)
        triggered = target + timedelta(minutes=7)
        uploaded = target + timedelta(minutes=19)
        history = [{'triggered_at': triggered.astimezone(timezone.utc).isoformat(),
                    'target_slot': '2026-09-18/05:00', 'outcome': 'uploaded',
                    'uploaded_at': uploaded.astimezone(timezone.utc).isoformat(),
                    'upload_offset_minutes': 19.0}]
        report = cloud.schedule_timing_summary(history, now=self.now(18))
        self.assertEqual(report['heartbeat_runs'], 1)
        self.assertEqual(report['uploads'], 1)
        self.assertEqual(report['average_offset_minutes'], 19.0)
        self.assertEqual(report['delayed_uploads'], 0)

    def test_record_schedule_event_is_trimmed_and_durable(self):
        ledger = Mock(data={'schema': 1, 'jobs': {}})
        event = cloud.record_schedule_event(ledger, '2026-09-18/05:00',
                                            self.now(5).astimezone(timezone.utc), 'selected')
        self.assertEqual(event['trigger_offset_minutes'], 0.0)
        self.assertEqual(ledger.data['schedule_history'][0]['outcome'], 'selected')
        ledger.save.assert_called_once()


class ScheduledFlowTests(unittest.TestCase):
    setUp = CloudTests.setUp
    persist = CloudTests.persist
    render = CloudTests.render
    execute = CloudTests.execute

    def test_scheduled_noop_never_renders_or_uploads(self):
        with patch.object(cloud, 'next_schedule_slot', return_value=None), \
             patch.object(bot, 'render') as render:
            uploader = self.execute(mode='scheduled')
        render.assert_not_called()
        uploader.assert_not_called()
        self.ledger.save.assert_called_once()

    def test_slot_is_saved_before_upload_and_survives_completion(self):
        slot = '2026-09-18/05:00'
        upload = Mock(side_effect=lambda *a: self.assertEqual(
            self.ledger.data['jobs'][ENTRY['id']]['schedule_slot'], slot) or 'video123')
        with patch.object(cloud, 'next_schedule_slot', return_value=slot):
            self.execute(mode='scheduled', upload=upload)
        self.assertEqual(self.ledger.data['jobs'][ENTRY['id']]['schedule_slot'], slot)
        self.assertEqual(self.ledger.data['jobs'][ENTRY['id']]['status'], 'uploaded')

    def test_uncertain_upload_is_not_retried_by_heartbeat(self):
        self.ledger.data['jobs']['uncertain'] = {'status': 'uploading'}
        with self.assertRaises(cloud.CloudError):
            self.execute(mode='scheduled')

class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        bot.atomic_json(self.root / 'automation.json',
                        {'enabled': True, 'channel_id': 'expected', 'privacy': 'public'})
        patcher = patch.object(cloud, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = Mock()
        self.service.channels.return_value.list.return_value.execute.return_value = {
            'items': [{'id': 'expected'}]}
        self.service.videos.return_value.list.return_value.execute.return_value = {'items': []}

    def analytics_service(self, payload=None, error=None, status=None):
        service = Mock()
        query = Mock()
        if error is not None:
            error.resp = SimpleNamespace(status=status)
            query.execute.side_effect = error
        else:
            query.execute.return_value = payload
        service.reports.return_value.query.return_value = query
        return service

    def payload(self, rows):
        return {'columnHeaders': [{'name': name} for name in
                                  ('views', 'estimatedMinutesWatched', 'averageViewDuration',
                                   'averageViewPercentage', 'likes', 'comments', 'shares',
                                   'subscribersGained')],
                'rows': [rows]}

    def ledger_with(self, row):
        ledger = Mock(data={'schema': 1, 'jobs': {'job': row}})
        return ledger

    def recent_row(self, **overrides):
        row = {'status': 'uploaded', 'video_id': 'vid1', 'uploaded_at':
               (datetime(2026, 9, 18, tzinfo=timezone.utc) - timedelta(days=2)).isoformat()}
        row.update(overrides)
        return row

    def test_retention_snapshot_is_recorded_on_the_job(self):
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        ledger = self.ledger_with(self.recent_row(cta_comment_variant=0))
        analytics = self.analytics_service(self.payload([100, 55.5, 33.2, 62.4, 8, 2, 1, 3]))
        report = cloud.collect_retention_metrics(analytics, ledger, now=now)
        self.assertTrue(report['available'])
        self.assertEqual(report['queried'], 1)
        snapshot = ledger.data['jobs']['job']['analytics']
        self.assertEqual(snapshot['views'], 100)
        self.assertEqual(snapshot['average_view_percentage'], 62.4)
        self.assertEqual(snapshot['subscribers_gained'], 3)
        self.assertEqual(snapshot['checked_at'], now.isoformat())
        self.assertEqual(report['variants']['0']['videos'], 1)
        self.assertEqual(report['variants']['0']['avg_view_percentage'], 62.4)
        ledger.save.assert_called_once()

    def test_older_than_window_videos_are_not_queried(self):
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        ledger = self.ledger_with(self.recent_row(
            uploaded_at=(now - timedelta(days=45)).isoformat()))
        analytics = self.analytics_service()
        report = cloud.collect_retention_metrics(analytics, ledger, now=now)
        self.assertEqual(report['queried'], 0)
        analytics.reports.return_value.query.assert_not_called()
        ledger.save.assert_not_called()

    def test_missing_analytics_scope_reports_unavailable(self):
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        ledger = self.ledger_with(self.recent_row())
        error = RuntimeError('insufficient permissions')
        analytics = self.analytics_service(error=error, status=403)
        report = cloud.collect_retention_metrics(analytics, ledger, now=now)
        self.assertFalse(report['available'])
        self.assertIn('yt-analytics.readonly', report['reason'])
        self.assertEqual(report['queried'], 0)

    def test_other_api_errors_are_skipped_without_stopping(self):
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        ledger = self.ledger_with(self.recent_row())
        error = RuntimeError('temporary')
        analytics = self.analytics_service(error=error, status=500)
        report = cloud.collect_retention_metrics(analytics, ledger, now=now)
        self.assertEqual(report['queried'], 0)
        self.assertTrue(report['available'])
        ledger.save.assert_not_called()

    def test_missing_rows_are_tolerated(self):
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        ledger = self.ledger_with(self.recent_row())
        analytics = self.analytics_service({'columnHeaders': [{'name': 'views'}], 'rows': None})
        report = cloud.collect_retention_metrics(analytics, ledger, now=now)
        self.assertEqual(report['queried'], 0)

    def test_invalid_cta_variant_is_ignored(self):
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        ledger = self.ledger_with(self.recent_row(cta_comment_variant=99))
        analytics = self.analytics_service(self.payload([100, 55.5, 33.2, 62.4, 8, 2, 1, 3]))
        report = cloud.collect_retention_metrics(analytics, ledger, now=now)
        self.assertEqual(report['queried'], 1)
        self.assertEqual(report['variants'], {})

    def test_variant_averages_are_aggregated(self):
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        rows = {'jobs': {}}
        for index, variant in enumerate((0, 0, 2)):
            rows['jobs'][f'job{index}'] = self.recent_row(
                video_id=f'vid{index}', cta_comment_variant=variant)
        ledger = Mock(data=rows)
        analytics = self.analytics_service()
        analytics.reports.return_value.query.return_value.execute.side_effect = [
            self.payload([100, 55.5, 33.2, 60.0, 8, 2, 1, 3]),
            self.payload([200, 55.5, 33.2, 70.0, 8, 2, 1, 3]),
            self.payload([50, 55.5, 33.2, 50.0, 8, 2, 1, 3])]
        report = cloud.collect_retention_metrics(analytics, ledger, now=now)
        self.assertEqual(report['variants']['0']['videos'], 2)
        self.assertEqual(report['variants']['0']['avg_view_percentage'], 65.0)
        self.assertEqual(report['variants']['2']['videos'], 1)
        ledger.save.assert_called_once()

    def test_none_service_degrades_cleanly(self):
        report = cloud.collect_retention_metrics(None, self.ledger_with(self.recent_row()))
        self.assertFalse(report['available'])
        self.assertEqual(report['reason'], 'analytics service not configured')

    def test_summary_includes_analytics_section(self):
        report = {'timestamp': 'now', 'tracked': 1, 'new_flags': [],
                  'averages': {'reciters': {}, 'visual_themes': {}},
                  'analytics': {'available': True, 'queried': 2,
                                'variants': {'0': {'videos': 2, 'avg_view_percentage': 61.3,
                                                   'avg_views': 150.5}}}}
        text = cloud.performance_summary(report)
        self.assertIn('Watch-through analytics', text)
        self.assertIn('Videos queried: 2', text)
        self.assertIn('Variant 0: 2 videos, avg retention 61.3%, avg views 150.5', text)

    def test_summary_reports_unavailable_reason(self):
        report = {'timestamp': 'now', 'tracked': 1, 'new_flags': [],
                  'averages': {'reciters': {}, 'visual_themes': {}},
                  'analytics': {'available': False, 'queried': 0,
                                'reason': 'YouTube refused analytics access'}}
        text = cloud.performance_summary(report)
        self.assertIn('Unavailable: YouTube refused analytics access', text)

    def test_report_failure_path_still_works_without_analytics(self):
        ledger = Mock(data={'schema': 1, 'jobs': {
            'job': {'status': 'uploaded', 'video_id': 'video123'}}})
        self.service.videos.return_value.list.return_value.execute.side_effect = RuntimeError('temporary')
        text = cloud.run_performance_report(self.service, ledger, analytics=None)
        self.assertIn('temporarily unavailable', text)


class PlaylistTests(unittest.TestCase):
    def service_with_playlists(self, existing=(), created_id='PL-new'):
        service = Mock()
        service.playlists.return_value.list.return_value.execute.return_value = {'items': list(existing)}
        service.playlists.return_value.insert.return_value.execute.return_value = {'id': created_id}
        return service

    def entry(self):
        return dict(surah_ar='الإخلاص', surah_en='Al-Ikhlas', attribution='Attr')

    def test_playlist_is_created_with_bilingual_title(self):
        ledger = Mock(data={'schema': 1, 'jobs': {}})
        service = self.service_with_playlists()
        playlist_id = cloud.ensure_playlist(service, ledger, self.entry(), 'public')
        self.assertEqual(playlist_id, 'PL-new')
        body = service.playlists.return_value.insert.call_args[1]['body']
        self.assertEqual(body['snippet']['title'], 'سورة الإخلاص | Al-Ikhlas — Quran Shorts')
        self.assertEqual(body['status']['privacyStatus'], 'public')
        self.assertEqual(ledger.data['playlists']['Al-Ikhlas'], 'PL-new')

    def test_existing_playlist_is_matched_by_title(self):
        ledger = Mock(data={'schema': 1, 'jobs': {}})
        existing = [{'id': 'PL-old',
                     'snippet': {'title': 'سورة الإخلاص | Al-Ikhlas — Quran Shorts'}}]
        service = self.service_with_playlists(existing=existing)
        self.assertEqual(cloud.ensure_playlist(service, ledger, self.entry()), 'PL-old')
        service.playlists.return_value.insert.assert_not_called()

    def test_cached_playlist_skips_listing(self):
        ledger = Mock(data={'schema': 1, 'jobs': {},
                            'playlists': {'Al-Ikhlas': 'PL-cached'}})
        service = self.service_with_playlists()
        self.assertEqual(cloud.ensure_playlist(service, ledger, self.entry()), 'PL-cached')
        service.playlists.return_value.list.assert_not_called()

    def test_missing_surah_names_fail_closed(self):
        ledger = Mock(data={'schema': 1, 'jobs': {}})
        with self.assertRaisesRegex(cloud.CloudError, 'surah'):
            cloud.ensure_playlist(Mock(), ledger, {'surah_ar': 'الإخلاص'})

    def test_video_is_added_when_not_a_member(self):
        ledger = Mock(data={'schema': 1, 'jobs': {},
                            'playlists': {'Al-Ikhlas': 'PL-cached'}})
        service = self.service_with_playlists()
        service.playlistItems.return_value.list.return_value.execute.return_value = {'items': []}
        result = cloud.add_video_to_playlist(service, ledger, self.entry(), 'video123')
        self.assertEqual(result, 'PL-cached')
        body = service.playlistItems.return_value.insert.call_args[1]['body']
        self.assertEqual(body['snippet']['resourceId'],
                         {'kind': 'youtube#video', 'videoId': 'video123'})
        ledger.save.assert_called_once()

    def test_existing_membership_is_not_duplicated(self):
        ledger = Mock(data={'schema': 1, 'jobs': {},
                            'playlists': {'Al-Ikhlas': 'PL-cached'}})
        service = self.service_with_playlists()
        service.playlistItems.return_value.list.return_value.execute.return_value = {
            'items': [{'id': 'pi1'}]}
        cloud.add_video_to_playlist(service, ledger, self.entry(), 'video123')
        service.playlistItems.return_value.insert.assert_not_called()

    def test_playlist_failure_never_raises(self):
        ledger = Mock(data={'schema': 1, 'jobs': {}})
        service = self.service_with_playlists()
        service.playlists.return_value.list.return_value.execute.side_effect = RuntimeError('down')
        self.assertIsNone(cloud.add_video_to_playlist(service, ledger, self.entry(), 'video123'))


if __name__ == '__main__':
    unittest.main()
