"""Regression tests for publication durability, monitoring and input boundaries."""
import copy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import bot
import cloud_runner as cloud
import test_cloud as fixtures

ENTRY = fixtures.ENTRY


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.CloudTests.setUp
    persist = fixtures.CloudTests.persist
    render = fixtures.CloudTests.render
    execute = fixtures.CloudTests.execute

    def test_deleted_video_does_not_blacklist_a_reciter(self):
        self.ledger.data['jobs'] = {'a': {'status': 'uploaded', 'video_id': 'deleted', 'reciter_id': 1}}
        self.service.videos.return_value.list.return_value.execute.return_value = {
            'items': [{'id': 'deleted', 'status': {'uploadStatus': 'deleted'}}]}
        incidents = cloud.check_upload_restrictions(self.service, self.ledger, {})
        self.assertEqual(incidents[0]['reason'], 'deleted')
        self.assertFalse(incidents[0]['reciter_blocked'])
        self.ledger.block_reciter.assert_not_called()

    def test_restriction_check_failure_blocks_new_publication(self):
        with patch.object(cloud, 'load_catalog', return_value={'schema': 3}), \
             patch.object(cloud, 'check_upload_restrictions', side_effect=RuntimeError('unavailable')), \
             patch.object(bot, 'upload') as upload:
            with self.assertRaisesRegex(cloud.CloudError, 'Restriction verification unavailable'):
                cloud.run(SimpleNamespace(mode='publish'), self.ledger, self.service)
        upload.assert_not_called()

    def test_video_id_is_durable_before_optional_comment(self):
        saved = []
        self.ledger.save.side_effect = lambda: saved.append(copy.deepcopy(self.ledger.data))

        def comment(*args):
            self.assertEqual(saved[-1]['jobs'][ENTRY['id']]['video_id'], 'video123')
            self.assertEqual(saved[-1]['jobs'][ENTRY['id']]['status'], 'uploaded')
            raise RuntimeError('comment disconnected')

        with patch.object(bot, 'post_cta_comment', side_effect=comment):
            self.execute()
        self.assertEqual(saved[-1]['jobs'][ENTRY['id']]['status'], 'uploaded')

    def test_missing_video_is_flagged_and_metrics_history_is_bounded(self):
        now = datetime.now(timezone.utc)
        self.ledger.data = {'jobs': {
            'a': {'status': 'uploaded', 'video_id': 'visible', 'uploaded_at': now.isoformat()},
            'b': {'status': 'uploaded', 'video_id': 'missing', 'uploaded_at': now.isoformat()}},
            'metrics_history': {'visible': [{'view_count': 1}] * 40}}
        self.service.videos.return_value.list.return_value.execute.return_value = {'items': [
            {'id': 'visible', 'statistics': {'viewCount': '5'}, 'status': {'privacyStatus': 'public'}}]}
        cloud.collect_performance_metrics(self.service, self.ledger, 'public', now)
        self.assertEqual(self.ledger.data['health_flags']['missing'], ['video unavailable'])
        self.assertEqual(len(self.ledger.data['metrics_history']['visible']), 30)

    def test_preview_does_not_advance_remote_cursor_for_rejected_audio(self):
        entry = dict(ENTRY)
        self.ledger.data['cursor'] = 10
        with patch.object(cloud, 'load_catalog', return_value={'schema': 3}), \
             patch.object(cloud, 'verse_entry_for_position', side_effect=[(entry, 11), (entry, 12)]), \
             patch.object(cloud, 'download_recording', side_effect=[cloud.TooShortRecording(), self.root / 'audio.mp3']), \
             patch.object(cloud, 'media_duration', return_value=35), \
             patch.object(cloud, 'make_card', return_value=self.root / 'card.png'), \
             patch.object(cloud, 'make_motion_overlay', return_value=self.root / 'rain.png'), \
             patch.object(bot, 'render', return_value=self.root / 'video.mp4'):
            cloud.run(SimpleNamespace(mode='preview'), self.ledger, self.service)
        self.assertEqual(self.ledger.data['cursor'], 10)
        self.ledger.save.assert_not_called()

    def test_bounded_scan_failure_saves_cursor_only_for_publication(self):
        for mode in ('publish', 'preview'):
            self.ledger.reset_mock()
            self.ledger.data['cursor'] = 10
            with patch.object(cloud, 'load_catalog', return_value={'schema': 3}), \
                 patch.object(cloud, 'check_upload_restrictions'), \
                 patch.object(cloud, 'verse_entry_for_position', side_effect=cloud.NoEligibleVerse(130)):
                with self.assertRaises(cloud.NoEligibleVerse):
                    cloud.run(SimpleNamespace(mode=mode), self.ledger, self.service)
            self.assertEqual(self.ledger.data['cursor'], 130 if mode == 'publish' else 10)
            self.assertEqual(self.ledger.save.call_count, 1 if mode == 'publish' else 0)


class BoundaryTests(unittest.TestCase):
    def test_arabic_renderer_preserves_diacritics_and_complete_wrapping(self):
        from arabic_text import ArabicText
        renderer = ArabicText(cloud.ROOT / 'assets' / 'Amiri-Regular.ttf')
        marked = renderer.mask('قُلْ هُوَ اللَّهُ أَحَدٌ', 46)
        plain = renderer.mask('قل هو الله أحد', 46)
        self.assertGreater(marked.height, plain.height)
        verse = 'قُلْ هُوَ اللَّهُ أَحَدٌ ' * 8
        wrapped = renderer.wrap(verse, 46, 400)
        self.assertEqual(' '.join(wrapped), ' '.join(verse.split()))
        self.assertGreater(len(wrapped), 1)
        self.assertTrue(all(renderer.mask(line, 46).width <= 400 for line in wrapped))
        with self.assertRaises(ValueError):
            renderer.mask('قُلْ 🛸', 46)

    def test_audio_paths_cannot_change_host(self):
        for path in ('//evil.example/x.mp3', 'https://evil.example/x.mp3', '\\evil\\x',
                     '../x.mp3', '/x.mp3', 'x\n.mp3'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                cloud.quran_audio_url(path)
        self.assertEqual(cloud.quran_audio_url('reader/001.mp3'), cloud.QURAN_AUDIO + 'reader/001.mp3')

    def test_corrupt_media_does_not_pass_duration_probe(self):
        result = SimpleNamespace(returncode=1, stderr='Duration: 00:00:35.00')
        with patch.object(bot, 'ffmpeg', return_value='ffmpeg'), patch.object(cloud.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                cloud.media_duration(Path('damaged.mp3'))

    def test_missing_previously_loaded_ledger_never_resets_state(self):
        ledger = cloud.RemoteLedger('owner/repo', 'fake-token')
        ledger.sha = 'previous'
        ledger.session.get = Mock(return_value=Mock(status_code=404))
        with self.assertRaises(cloud.CloudError):
            ledger.load()

    def test_batch_stops_on_first_failure_and_does_not_retry(self):
        with patch.object(cloud, 'run', side_effect=['video1', RuntimeError('failed')]) as run:
            with self.assertRaises(RuntimeError):
                cloud.run_batch(SimpleNamespace(mode='publish', count=5), Mock(), Mock())
            self.assertEqual(run.call_count, 2)

    def test_batch_stops_when_catalog_is_exhausted(self):
        with patch.object(cloud, 'run', side_effect=['video1', None]) as run:
            cloud.run_batch(SimpleNamespace(mode='publish', count=5), Mock(), Mock())
            self.assertEqual(run.call_count, 2)

    def test_batch_success_runs_requested_number(self):
        with patch.object(cloud, 'run', return_value='video1') as run:
            cloud.run_batch(SimpleNamespace(mode='publish', count=5), Mock(), Mock())
            self.assertEqual(run.call_count, 5)

    def test_history_and_jobs_do_not_double_count_uploads(self):
        now = datetime(2026, 9, 22, 15, tzinfo=timezone.utc)
        stamp = now.isoformat()
        slot = '2026-09-22/05:00'
        history = [{'triggered_at': stamp, 'outcome': 'uploaded', 'target_slot': slot,
                    'uploaded_at': stamp, 'upload_offset_minutes': 780}]
        jobs = {'a': {'uploaded_at': stamp, 'schedule_slot': slot}}
        report = cloud.schedule_timing_summary(history, jobs, now)
        self.assertEqual(report['uploads'], 1)
        self.assertEqual(report['heartbeat_runs'], 1)

    def test_backfilled_upload_resolves_selected_event(self):
        now = datetime(2026, 9, 22, 15, tzinfo=timezone.utc)
        slot = '2026-09-22/05:00'
        history = [{'triggered_at': now.isoformat(), 'outcome': 'selected', 'target_slot': slot}]
        jobs = {'a': {'uploaded_at': now.isoformat(), 'schedule_slot': slot}}
        report = cloud.schedule_timing_summary(history, jobs, now)
        self.assertEqual(report['uploads'], 1)
        self.assertEqual(report['unresolved'], 0)
        self.assertEqual(report['heartbeat_runs'], 1)

    def test_invalid_tail_and_minimum_are_rejected(self):
        catalog = bot.read_json(cloud.ROOT / 'catalog.json')
        for tail in (float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                cloud.validate_verse_catalog(dict(catalog, tail_silence_seconds=tail))

    def test_unavailable_preferred_reciter_does_not_starve_other_reciters(self):
        catalog = bot.read_json(cloud.ROOT / 'catalog.json')
        catalog['allowed_reciter_ids'] = [1, 2]
        calls = []

        def api(url, params=None):
            if 'resources/recitations' in url:
                return {'recitations': [{'id': 1, 'reciter_name': 'One'}, {'id': 2, 'reciter_name': 'Two'}]}
            if url.endswith('/chapters'):
                return {'chapters': [{'id': 1, 'verses_count': 6236, 'name_arabic': 'الفاتحة', 'name_simple': 'Al-Fatihah'}]}
            if 'quran/verses' in url:
                return {'verses': [{'text_uthmani': 'قُلْ هُوَ اللَّهُ أَحَدٌ'}]}
            calls.append(url)
            return {'audio_files': [{'duration': 6 if '/recitations/1/' in url else 35, 'url': 'safe.mp3'}]}

        jobs = {'a': {'reciter_id': 2}, 'b': {'reciter_id': 2}}
        with patch.object(cloud, 'get_json', side_effect=api):
            entry, _ = cloud.verse_entry_for_position(catalog, jobs, 0)
        self.assertEqual(entry['recitation_id'], 2)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
