from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

import bot
import cloud_runner as cloud


class _FrameReader:
    def __init__(self, metadata):
        self.metadata = metadata

    def __iter__(self):
        return self

    def __next__(self):
        return self.metadata

    def close(self):
        pass


class HealthAndSelectionTests(unittest.TestCase):
    def test_overdue_slots_wait_for_grace_and_report_uncertain_uploads(self):
        before_grace = datetime(2026, 9, 27, 6, 44, tzinfo=cloud.BAGHDAD)
        at_grace = datetime(2026, 9, 27, 6, 45, tzinfo=cloud.BAGHDAD)
        self.assertEqual(cloud.overdue_publication_slots({}, before_grace), [])
        overdue = cloud.overdue_publication_slots({}, at_grace)
        self.assertEqual(overdue, [{
            'slot': '2026-09-27/06:00',
            'minutes_overdue': 45,
            'upload_uncertain': False,
        }])

        uncertain = {'pending': {
            'status': 'uploading', 'schedule_slot': '2026-09-27/06:00'}}
        overdue = cloud.overdue_publication_slots(uncertain, at_grace)
        self.assertTrue(overdue[0]['upload_uncertain'])

    def test_uploaded_slot_is_not_reported_as_overdue(self):
        now = datetime(2026, 9, 27, 7, 0, tzinfo=cloud.BAGHDAD)
        jobs = {'published': {
            'status': 'uploaded', 'video_id': 'abc',
            'uploaded_at': now.isoformat(),
            'schedule_slot': '2026-09-27/06:00',
        }}
        self.assertEqual(cloud.overdue_publication_slots(jobs, now), [])

    def test_averages_count_each_video_once_using_latest_snapshot(self):
        jobs = {
            'one': {'status': 'uploaded', 'video_id': 'v1',
                    'reciter_id': 1, 'reciter_name': 'Reader',
                    'visual_theme': 'forest'},
            'two': {'status': 'uploaded', 'video_id': 'v2',
                    'reciter_id': 1, 'reciter_name': 'Reader',
                    'visual_theme': 'forest'},
        }
        history = {
            'v1': [{'view_count': 10}, {'view_count': 40}],
            'v2': [{'view_count': 80}],
        }
        self.assertEqual(
            cloud.performance_averages(history, jobs),
            {'reciters': {'Reader': 60.0},
             'visual_themes': {'forest': 60.0}},
        )
        self.assertEqual(
            cloud.performance_sample_counts(history, jobs),
            {'reciters': {'Reader': 2},
             'visual_themes': {'forest': 2}},
        )

    def test_performance_preference_needs_three_videos_and_keeps_all_reciters(self):
        jobs, history, reciters = {}, {}, []
        for reciter_id, view_count in ((1, 10), (2, 100)):
            reciters.append({'id': reciter_id, 'reciter_name': str(reciter_id)})
            for index in range(3):
                video_id = f'{reciter_id}-{index}'
                jobs[video_id] = {
                    'status': 'uploaded', 'video_id': video_id,
                    'reciter_id': reciter_id, 'visual_theme': f'theme-{reciter_id}',
                }
                history[video_id] = [{'view_count': view_count}]
        self.assertEqual(
            cloud._performance_preferences(history, jobs, 'reciter_id', 3),
            {1: 0.0, 2: 1.0},
        )
        ranked = cloud._reciter_order(
            reciters, jobs, 12,
            {'performance_bias_enabled': True,
             'performance_bias_min_videos': 3,
             'performance_bias_strength': 0.15},
            history,
        )
        self.assertEqual({row['id'] for row in ranked}, {1, 2})

    def test_theme_rotation_prefers_less_used_but_never_excludes_a_theme(self):
        themes = ['forest', 'rain', 'coast']
        jobs = {'used': {'status': 'uploaded', 'visual_theme': 'forest'}}
        order = cloud._visual_theme_order(themes, jobs, 0, {})
        self.assertEqual(set(order), set(themes))
        self.assertEqual(order[0], 'rain')

    def test_summary_shows_late_slots_and_per_video_sample_counts(self):
        report = {
            'timestamp': 'now', 'tracked': 1, 'new_flags': [],
            'averages': {'reciters': {'Reader': 20.0},
                         'visual_themes': {'forest': 20.0}},
            'sample_counts': {'reciters': {'Reader': 3},
                              'visual_themes': {'forest': 3}},
            'schedule': {
                'window_days': 7, 'heartbeat_runs': 4, 'uploads': 1,
                'unresolved': 0, 'average_offset_minutes': 2.0,
                'max_offset_minutes': 2.0, 'delayed_uploads': 0,
                'overdue_slots': [{
                    'slot': '2026-09-27/06:00',
                    'minutes_overdue': 55,
                    'upload_uncertain': False,
                }],
            },
        }
        summary = cloud.performance_summary(report)
        self.assertIn('Reader: 20.0 views (3 videos)', summary)
        self.assertIn('forest: 20.0 views (3 videos)', summary)
        self.assertIn('2026-09-27/06:00: 55 minutes overdue', summary)


class RenderQualityGateTests(unittest.TestCase):
    def test_video_quality_gate_requires_full_vertical_frame_and_audio_decode(self):
        metadata = {'size': (1080, 1920), 'duration': 35.0}
        with patch('imageio_ffmpeg.read_frames', return_value=_FrameReader(metadata)), \
             patch.object(bot, 'run_media') as decode:
            bot.validate_video('sample.mp4', 35.0, minimum=30.0)
        self.assertIn('0:a:0', decode.call_args.args[0])

    def test_video_quality_gate_rejects_bad_geometry_or_short_duration(self):
        bad_geometry = {'size': (1080, 1080), 'duration': 35.0}
        short = {'size': (1080, 1920), 'duration': 6.0}
        for metadata in (bad_geometry, short):
            with self.subTest(metadata=metadata):
                with patch('imageio_ffmpeg.read_frames',
                           return_value=_FrameReader(metadata)), \
                     patch.object(bot, 'run_media') as decode:
                    with self.assertRaises(ValueError):
                        bot.validate_video('sample.mp4', 35.0, minimum=30.0)
                    decode.assert_not_called()

    def test_video_quality_gate_rejects_missing_or_unreadable_audio(self):
        metadata = {'size': (1080, 1920), 'duration': 35.0}
        with patch('imageio_ffmpeg.read_frames', return_value=_FrameReader(metadata)), \
             patch.object(bot, 'run_media', side_effect=RuntimeError('missing audio stream')):
            with self.assertRaises(RuntimeError):
                bot.validate_video('sample.mp4', 35.0, minimum=30.0)


if __name__ == '__main__':
    unittest.main()
