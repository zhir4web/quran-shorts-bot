import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import background_provider as provider
import cloud_runner as cloud


def pexels_video(video_id, width=1080, height=1920, duration=18):
    return {
        "id": video_id,
        "url": f"https://www.pexels.com/video/nature-{video_id}/",
        "duration": duration,
        "user": {
            "name": f"Creator {video_id}",
            "url": "https://www.pexels.com/@creator/",
        },
        "video_files": [{
            "file_type": "video/mp4",
            "quality": "hd",
            "width": width,
            "height": height,
            "link": f"https://videos.pexels.com/video-files/{video_id}/clip.mp4",
        }],
    }


class FakeResponse:
    def __init__(self, payload=None, url="", chunks=()):
        self.payload = payload
        self.url = url
        self.chunks = chunks
        self.headers = {"Content-Type": "video/mp4"}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield from self.chunks


class BackgroundProviderTests(unittest.TestCase):
    def test_candidate_requires_native_high_resolution_portrait_video(self):
        self.assertIsNone(provider._candidate(pexels_video(1, 720, 1280)))
        self.assertIsNone(provider._candidate(pexels_video(2, 1920, 1080)))
        unsafe = pexels_video(3)
        unsafe["video_files"][0]["link"] = "https://videos.pexels.com.evil.test/clip.mp4"
        self.assertIsNone(provider._candidate(unsafe))
        self.assertEqual(provider._candidate(pexels_video(4))["video_id"], "4")

    def test_download_avoids_used_ids_and_records_license_and_checksum(self):
        payload = {"videos": [
            pexels_video(10), pexels_video(11), pexels_video(11), pexels_video(12)
        ]}
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            if url == provider.SEARCH_URL:
                return FakeResponse(payload=payload, url=url)
            return FakeResponse(url=url, chunks=(b"licensed-video-", url.rsplit("/", 2)[-2].encode()))

        with tempfile.TemporaryDirectory() as directory:
            found = provider.download_fresh_backgrounds(
                "test-api-key", "cinematic_forest_01", {"10"},
                destination=directory, count=2, request_get=fake_get,
            )
            self.assertEqual([row["video_id"] for row in found], ["11", "12"])
            self.assertEqual(len({row["video_id"] for row in found}), 2)
            for row in found:
                self.assertTrue(Path(row["path"]).is_file())
                self.assertEqual(row["license"], "Pexels License")
                self.assertEqual(row["license_url"], provider.PEXELS_LICENSE_URL)
                self.assertEqual(
                    row["sha256"], hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest()
                )
            search = calls[0][1]
            self.assertEqual(search["headers"]["Authorization"], "test-api-key")
            self.assertEqual(search["params"]["orientation"], "portrait")
            self.assertNotIn("test-api-key", calls[0][0])

    def test_api_error_is_sanitized(self):
        def fail(*args, **kwargs):
            raise RuntimeError("secret response body")

        with self.assertRaises(provider.PexelsUnavailable) as caught:
            provider.download_fresh_backgrounds(
                "key", "forest", set(), request_get=fail
            )
        self.assertNotIn("secret response body", str(caught.exception))

    def test_no_api_key_does_not_make_a_request(self):
        with patch("background_provider.requests.get") as get:
            with self.assertRaises(provider.PexelsUnavailable):
                provider.download_fresh_backgrounds("", "forest", set())
            get.assert_not_called()

    def test_optional_api_failure_returns_local_fallback_signal(self):
        with patch.object(
            cloud.background_provider, "download_fresh_backgrounds",
            side_effect=RuntimeError("secret response body"),
        ), patch("builtins.print") as log:
            videos, folder = cloud.best_effort_pexels_backgrounds(
                "api-key", "cinematic_forest_01", set(), 2
            )
        self.assertEqual(videos, [])
        self.assertIsNone(folder)
        self.assertNotIn("secret response body", str(log.call_args_list))

    def test_external_backgrounds_add_credits_and_ledger_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for video_id in ("21", "22"):
                path = Path(directory) / f"pexels-{video_id}.mp4"
                path.write_bytes(b"video")
                paths.append(path)
            sources = [{
                "path": str(path),
                "video_id": video_id,
                "creator": "Verified Creator",
                "creator_url": "https://www.pexels.com/@creator/",
                "page_url": f"https://www.pexels.com/video/{video_id}/",
                "license": "Pexels License",
                "license_url": provider.PEXELS_LICENSE_URL,
                "sha256": "a" * 64,
                "width": 1080,
                "height": 1920,
                "duration": 18,
                "reused": False,
                "download_url": "https://videos.pexels.com/private-download.mp4",
            } for path, video_id in zip(paths, ("21", "22"))]
            job = cloud.item_for(
                dict(id="pexels-sample", surah_ar="الإخلاص", surah_en="Al-Ikhlas",
                     reciter_ar="قارئ", reciter_en="Reciter", duration=35,
                     attribution="Quran Foundation", rights="licensed",
                     permission_url="https://example.com/license"),
                Path(directory) / "audio.mp3", Path(directory) / "card.png",
                external_backgrounds=sources,
            )
        self.assertEqual(len(job["background_playlist"]), 2)
        self.assertEqual([row["video_id"] for row in job["background_sources"]], ["21", "22"])
        self.assertIn("Verified Creator", job["description"])
        self.assertIn(provider.PEXELS_LICENSE_URL, job["description"])
        self.assertNotIn("private-download.mp4", str(job["background_sources"]))
        self.assertEqual(job["visual_theme"], "forest_rain")


if __name__ == "__main__":
    unittest.main()
