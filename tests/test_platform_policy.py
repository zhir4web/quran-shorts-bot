import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot
import cloud_runner
import platform_policy


class PlatformPolicyTests(unittest.TestCase):
    def test_platforms_have_independent_duration_bounds(self):
        catalog = {
            "min_audio_seconds": 30,
            "max_audio_seconds": 58,
            "platform_duration_policies": {
                "youtube": {"min_audio_seconds": 30, "max_audio_seconds": 58},
                "tiktok": {"min_audio_seconds": 61, "max_audio_seconds": 88},
            },
        }
        self.assertEqual(platform_policy.audio_duration_bounds(catalog, "youtube"), (30, 58))
        self.assertEqual(platform_policy.audio_duration_bounds(catalog, "tiktok"), (61, 88))

    def test_tiktok_requires_more_than_one_minute_and_leaves_room_for_ending(self):
        for policy in (
            {"min_audio_seconds": 60, "max_audio_seconds": 88},
            {"min_audio_seconds": 61, "max_audio_seconds": 89},
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                platform_policy.audio_duration_bounds(
                    {"platform_duration_policies": {"tiktok": policy}}, "tiktok")

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(ValueError):
            platform_policy.audio_duration_bounds({}, "instagram")

    def test_queue_accepts_tiktok_preview_but_keeps_youtube_under_sixty(self):
        def item(platform):
            return {
                "id": f"sample-{platform}", "mode": "compose", "platform": platform,
                "title": "Test recitation", "attribution": "Test", "rights": "Test",
                "rights_confirmed": True, "made_for_kids": False,
                "duration": 70, "min_duration_seconds": 61,
                "source": "source.mp3", "description": "",
            }

        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "queue.json"
            queue.write_text(json.dumps({"items": [item("tiktok")]}), encoding="utf-8")
            self.assertEqual(bot.load_queue(queue)[0]["platform"], "tiktok")
            queue.write_text(json.dumps({"items": [item("youtube")]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "at most 60"):
                bot.load_queue(queue)

    def test_queue_rejects_tiktok_video_outside_supported_length(self):
        item = {
            "id": "sample-tiktok", "mode": "compose", "platform": "tiktok",
            "title": "Test recitation", "attribution": "Test", "rights": "Test",
            "rights_confirmed": True, "made_for_kids": False,
            "duration": 60, "min_duration_seconds": 60,
            "source": "source.mp3", "description": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "queue.json"
            queue.write_text(json.dumps({"items": [item]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "61 to 90"):
                bot.load_queue(queue)
            item.update(duration=91, min_duration_seconds=61)
            queue.write_text(json.dumps({"items": [item]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "at most 90"):
                bot.load_queue(queue)

    def test_video_validation_keeps_youtube_and_tiktok_caps_separate(self):
        class FakeReader:
            def __next__(self):
                return {"size": (1080, 1920), "duration": 70.0}

            def close(self):
                pass

        fake_ffmpeg = SimpleNamespace(read_frames=lambda _path: FakeReader())
        with patch.dict("sys.modules", {"imageio_ffmpeg": fake_ffmpeg}), \
             patch.object(bot, "run_media"):
            bot.validate_video("sample.mp4", 70, minimum=61, maximum=90.1)
            with self.assertRaisesRegex(ValueError, "invalid duration"):
                bot.validate_video("sample.mp4", 70, minimum=30)

    def test_cloud_publish_route_fails_closed_for_tiktok(self):
        with self.assertRaisesRegex(cloud_runner.CloudError, "TikTok publishing is not configured"):
            cloud_runner.run(SimpleNamespace(mode="publish", platform="tiktok"))


if __name__ == "__main__":
    unittest.main()
