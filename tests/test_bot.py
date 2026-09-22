import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import bot


def item():
    return dict(id="test-1", mode="compose", source="audio.wav", start=0, duration=1,
                title="Test", attribution="Original test tone", rights="Self-created",
                rights_confirmed=True, made_for_kids=False)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = self.root / "queue.json"
        self.job = item()
        self.save()
        self.args = argparse.Namespace(queue=str(self.queue), state=str(self.root / "state"),
                                       command="run", limit=1, privacy="private")

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        bot.atomic_json(self.queue, {"items": [self.job]})

    def fake_render(self, job, base, folder):
        folder.mkdir(parents=True, exist_ok=True)
        result = folder / "video.mp4"
        result.write_bytes(b"test fixture")
        return result

    def test_requires_rights(self):
        self.job["rights_confirmed"] = False
        self.save()
        with self.assertRaises(ValueError):
            bot.load_queue(self.queue)

    def test_rejects_unknown_duration(self):
        for duration in (0, -1, 61, float("nan"), True):
            self.job["duration"] = duration
            self.save()
            with self.assertRaises(ValueError):
                bot.load_queue(self.queue)

    def test_rejects_invalid_minimum_and_incompatible_backgrounds(self):
        for minimum in (float('nan'), True, -1, 2):
            self.job['min_duration_seconds'] = minimum
            self.save()
            with self.assertRaises(ValueError):
                bot.load_queue(self.queue)
        self.job.pop('min_duration_seconds')
        self.job.update(mode='video', background_video='background.mp4')
        self.save()
        with self.assertRaisesRegex(ValueError, 'compose'):
            bot.load_queue(self.queue)
        self.job.update(mode='compose', background_motion='premium_motion')
        self.save()
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            bot.load_queue(self.queue)

    def test_duplicate_and_traversal_ids(self):
        bot.atomic_json(self.queue, {"items": [self.job, self.job]})
        with self.assertRaises(ValueError):
            bot.load_queue(self.queue)
        self.job["id"] = "../escape"
        self.save()
        with self.assertRaises(ValueError):
            bot.load_queue(self.queue)

    def test_preview_never_authenticates(self):
        self.args.command = "preview"
        with patch.object(bot, "render", side_effect=self.fake_render), patch.object(bot, "youtube_service") as auth:
            bot.run_queue(self.args)
            auth.assert_not_called()

    def test_success_not_uploaded_twice(self):
        with patch.object(bot, "render", side_effect=self.fake_render), patch.object(bot, "youtube_service"), patch.object(bot, "upload", return_value="abc123") as upload:
            bot.run_queue(self.args)
            bot.run_queue(self.args)
            self.assertEqual(upload.call_count, 1)

    def test_ambiguous_upload_blocks_retry(self):
        with patch.object(bot, "render", side_effect=self.fake_render), patch.object(bot, "youtube_service"), patch.object(bot, "upload", side_effect=OSError("connection lost")) as upload:
            with self.assertRaises(OSError):
                bot.run_queue(self.args)
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                bot.run_queue(self.args)
            self.assertEqual(upload.call_count, 1)

    def test_modified_item_rejected(self):
        self.args.command = "preview"
        with patch.object(bot, "render", side_effect=self.fake_render):
            bot.run_queue(self.args)
            self.job["title"] = "Changed"
            self.save()
            with self.assertRaisesRegex(ValueError, "changed"):
                bot.run_queue(self.args)

    def test_damaged_render_rebuilt(self):
        self.args.command = "preview"
        with patch.object(bot, "render", side_effect=self.fake_render) as render:
            bot.run_queue(self.args)
            (Path(self.args.state) / "renders/test-1/video.mp4").write_bytes(b"damaged")
            bot.run_queue(self.args)
            self.assertEqual(render.call_count, 2)

    def test_lock_excludes_second_run(self):
        with bot.process_lock(self.root / "run.lock"):
            with self.assertRaises(RuntimeError):
                with bot.process_lock(self.root / "run.lock"):
                    self.fail("Second process acquired lock")

    def test_failure_exit_code(self):
        self.assertEqual(bot.main(["preview", "--queue", str(self.root / "missing.json"), "--state", str(self.root / "state")]), 1)

    def test_upload_metadata_and_resumable_retries(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"upload fixture")
        service = MagicMock()
        request = service.videos.return_value.insert.return_value
        request.next_chunk.side_effect = [(None, None), (None, {"id": "verified-id"})]
        self.assertEqual(bot.upload(service, self.job, video, "private"), "verified-id")
        payload = service.videos.return_value.insert.call_args.kwargs
        self.assertEqual(payload["body"]["status"]["privacyStatus"], "private")
        self.assertFalse(payload["body"]["status"]["selfDeclaredMadeForKids"])
        self.assertIn(self.job["attribution"], payload["body"]["snippet"]["description"])
        self.assertTrue(payload["media_body"].resumable())
        request.next_chunk.assert_called_with(num_retries=5)

    def test_dynamic_tags_are_relevant_and_bounded(self):
        self.job.update(surah_ar='الإخلاص', surah_en='Al-Ikhlas',
                        reciter_ar='عبد الباسط عبد الصمد', reciter_en='Abdul Basit')
        tags = bot.dynamic_tags(self.job)
        self.assertIn('Quran recitation', tags)
        self.assertIn('الإخلاص', tags)
        self.assertIn('Al-Ikhlas', tags)
        self.assertIn('Abdul Basit', tags)
        self.assertLessEqual(len(','.join(tags)), 500)

    def test_comment_variant_is_deterministic_and_varied(self):
        first = bot.comment_variant('video-one')
        self.assertEqual(first, bot.comment_variant('video-one'))
        variants = {bot.comment_variant(f'video-{number}')[0] for number in range(30)}
        self.assertGreaterEqual(len(variants), 6)

    def test_comment_failure_is_nonfatal(self):
        service = MagicMock()
        service.commentThreads.return_value.insert.return_value.execute.side_effect = RuntimeError('disabled')
        index, succeeded = bot.post_cta_comment(service, 'video-id')
        self.assertIn(index, range(len(bot.CTA_COMMENTS)))
        self.assertFalse(succeeded)

    def test_missing_auth_does_not_mark_uploading(self):
        with patch.object(bot, "render", side_effect=self.fake_render), patch.object(bot, "youtube_service", side_effect=RuntimeError("Login required")):
            with self.assertRaisesRegex(RuntimeError, "Login required"):
                bot.run_queue(self.args)
        ledger = bot.Ledger(Path(self.args.state) / "jobs.sqlite3")
        self.assertEqual(ledger.row(self.job["id"])[1], "rendered")
        ledger.close()

    def test_resolution_records_existing_video(self):
        ledger = bot.Ledger(Path(self.args.state) / "jobs.sqlite3")
        ledger.record(self.job, "uploading", output_hash="known-hash")
        ledger.close()
        self.args.command = "resolve"
        self.args.id = self.job["id"]
        self.args.video_id = "existing-id"
        bot.run_queue(self.args)
        ledger = bot.Ledger(Path(self.args.state) / "jobs.sqlite3")
        self.assertEqual(ledger.row(self.job["id"])[1:3], ("uploaded", "existing-id"))
        ledger.close()


class MediaTests(unittest.TestCase):
    def test_real_video_render_uses_high_quality_crop_and_encode(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "audio.wav").write_bytes(b"audio")
            background = base / "background.mp4"
            background.write_bytes(b"video")
            card = base / "card.png"
            card.write_bytes(b"card")
            job = item()
            job.update(duration=30, background=str(card), background_video=str(background), background_motion="real_video")
            captured = {}

            def fake_media(args):
                captured["args"] = args
                Path(args[-1]).write_bytes(b"rendered")

            with patch.object(bot, "validate_background_source"), \
                 patch.object(bot, "run_media", side_effect=fake_media), \
                 patch.object(bot, "validate_video"), \
                 patch.object(bot, "validate_visible_motion"):
                result = bot.render(job, base, base / "real-video")

            self.assertTrue(result.is_file())
            args = captured["args"]
            graph = args[args.index("-filter_complex") + 1]
            self.assertIn("scale=1120:1992:flags=lanczos:force_original_aspect_ratio=increase,", graph)
            self.assertIn("crop=1080:1920:x='20+12*sin(t/5)':y='36+10*cos(t/6)',setsar=1", graph)
            self.assertIn("overlay=0:0:format=auto", graph)
            self.assertIn("-b:v", args)
            self.assertEqual(args[args.index("-b:v") + 1], "10M")
            self.assertEqual(args[args.index("-maxrate") + 1], "12M")
            self.assertEqual(args[args.index("-map") + 3], "2:a:0")

    def test_landscape_video_and_missing_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            bot.run_media(["-y", "-f", "lavfi", "-i", "color=blue:s=640x360:r=30:d=1",
                           "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(base / "landscape.mp4")])
            job = item()
            job.update(mode="video", source="landscape.mp4")
            self.assertTrue(bot.render(job, base, base / "portrait").is_file())
            bot.run_media(["-y", "-i", str(base / "landscape.mp4"), "-an", "-c:v", "copy", str(base / "silent.mp4")])
            job["source"] = "silent.mp4"
            with self.assertRaises(RuntimeError):
                bot.render(job, base, base / "silent")

    def test_real_audio_composition_and_video_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            bot.run_media(["-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(base / "audio.wav")])
            job = item()
            first = bot.render(job, base, base / "compose")
            self.assertTrue(first.is_file())
            job.update(mode="video", source=str(first))
            second = bot.render(job, base, base / "video")
            self.assertTrue(second.is_file())
            job.update(duration=3)
            with self.assertRaisesRegex(ValueError, "too short"):
                bot.render(job, base, base / "short")


class StitchingTests(unittest.TestCase):
    def segments(self, count):
        return [Path(f"clip{index}.mp4") for index in range(count)]

    def test_single_clip_keeps_single_input_chain(self):
        playlist = bot.split_background_segments(self.segments(1), 35)
        self.assertEqual([(Path("clip0.mp4"), 35.0)], playlist)

    def test_each_clip_gets_an_equal_share_before_any_repeat(self):
        playlist = bot.split_background_segments(self.segments(3), 36)
        self.assertEqual([path.name for path, _ in playlist], ["clip0.mp4", "clip1.mp4", "clip2.mp4"])
        self.assertEqual([share for _, share in playlist], [12.0, 12.0, 12.0])

    def test_invalid_durations_fail_closed(self):
        for total in (0, -5, float("nan")):
            with self.subTest(total=total):
                with self.assertRaises(ValueError):
                    bot.split_background_segments(self.segments(2), total)

    def test_render_uses_concat_filter_for_multiple_clips(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            bot.run_media(["-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(base / "audio.wav")])
            clips = []
            for index, color in enumerate(("red", "green")):
                clip = base / f"clip{index}.mp4"
                bot.run_media(["-y", "-f", "lavfi", "-i", f"color={color}:s=1080x1920:r=30:d=1",
                               "-c:v", "libx264", str(clip)])
                clips.append(clip.name)
            job = item()
            job.update(duration=2, source="audio.wav", background_video=str(base / clips[0]),
                       background_playlist=[str(base / name) for name in clips])
            captured = {}

            def fake_media(args):
                captured["args"] = args
                Path(args[-1]).write_bytes(b"stitched")

            with patch.object(bot, "validate_background_source"), \
                 patch.object(bot, "validate_video"), \
                 patch.object(bot, "run_media", side_effect=fake_media):
                bot.render(job, base, base / "stitched")
            args = captured["args"]
            graph = args[args.index("-filter_complex") + 1]
            self.assertEqual(args.count("-stream_loop"), 2)
            self.assertIn("concat=n=2:v=1:a=0", graph)
            self.assertIn("trim=duration=1.000", graph)
            self.assertEqual(args[args.index("-map") + 3], "2:a:0")

    def test_playlist_requires_background_video(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            job = item()
            job["background_playlist"] = ["a.mp4"]
            queue = base / "queue.json"
            bot.atomic_json(queue, {"items": [job]})
            with self.assertRaises(ValueError):
                bot.load_queue(queue)


class SubmitTests(unittest.TestCase):
    def test_submit_rejects_non_mp4(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "clip.avi"
            source.write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "MP4"):
                bot.submit_video(source, "forest_rain", base / "queue.json", "T", "A", "R", base=base)

    def test_submit_validates_probe_and_records_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "newclip.mp4"
            source.write_bytes(b"raw")
            queue = base / "queue.json"
            with patch.object(bot, "validate_background_source") as probe:
                destination = bot.submit_video(source, "forest_rain", queue, "Ocean waves",
                                               "Pexels author", "Pexels License", base=base,
                                               state=base / "state")
            probe.assert_called_once()
            self.assertTrue(destination.is_file())
            self.assertTrue(destination.name.startswith("forest_rain_"))
            record = bot.read_json(base / "state" / "submissions" / "clip-forest_rain-2.json")
            self.assertFalse(record["license_confirmed"])
            self.assertEqual(record["submit_theme"], "forest_rain")
            queue_data = bot.read_json(queue)
            self.assertEqual(queue_data["items"][0]["id"], "clip-forest_rain-2")

    def test_submit_skips_existing_clip_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            folder = base / "assets" / "backgrounds" / "video"
            folder.mkdir(parents=True)
            (folder / "forest_rain_2.mp4").write_bytes(b"existing")
            source = base / "new.mp4"
            source.write_bytes(b"raw")
            with patch.object(bot, "validate_background_source"):
                destination = bot.submit_video(source, "forest_rain", base / "queue.json", "T", "A", "R",
                                               base=base, state=base / "state")
            self.assertEqual(destination.name, "forest_rain_3.mp4")


if __name__ == "__main__":
    unittest.main()

