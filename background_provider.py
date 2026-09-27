"""Best-effort, licensed Pexels video backgrounds for Quran Shorts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

import requests


SEARCH_URL = "https://api.pexels.com/v1/videos/search"
MIN_SHORT_DIMENSION = 1080
MIN_PORTRAIT_RATIO = 1.55
MIN_DURATION_SECONDS = 6
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
PEXELS_LICENSE_URL = "https://www.pexels.com/license/"


class PexelsUnavailable(RuntimeError):
    """Pexels could not provide enough safe, usable footage."""


def _trusted_pexels_url(value):
    try:
        parsed = urlparse(str(value or ""))
        hostname = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and
                (hostname == "pexels.com" or hostname.endswith(".pexels.com")))
    except (TypeError, ValueError):
        return False


def _theme_query(theme):
    value = re.sub(r"[^a-z0-9]+", " ", str(theme or "").lower()).strip()
    if any(word in value for word in ("coast", "river", "water")):
        return "cinematic vertical ocean coast river nature"
    if "mountain" in value:
        return "cinematic vertical mountain landscape drone nature"
    if any(word in value for word in ("rain", "mist", "forest", "woodland")):
        return "cinematic vertical forest mist rain nature"
    return "cinematic vertical peaceful nature landscape"


def _candidate(video, used_ids=()):
    """Return a normalized high-resolution portrait candidate, or None."""
    if not isinstance(video, dict):
        return None
    try:
        video_id = str(int(video.get("id")))
        duration = float(video.get("duration"))
    except (TypeError, ValueError, OverflowError):
        return None
    if duration < MIN_DURATION_SECONDS or not _trusted_pexels_url(video.get("url")):
        return None
    user = video.get("user") if isinstance(video.get("user"), dict) else {}
    candidates = []
    for item in video.get("video_files") or []:
        if not isinstance(item, dict) or item.get("file_type") not in ("video/mp4", "mp4"):
            continue
        try:
            width, height = int(item.get("width")), int(item.get("height"))
        except (TypeError, ValueError, OverflowError):
            continue
        if (min(width, height) < MIN_SHORT_DIMENSION or width <= 0 or
                height / width < MIN_PORTRAIT_RATIO or
                not _trusted_pexels_url(item.get("link"))):
            continue
        candidates.append((width * height, abs(height / width - 16 / 9), item, width, height))
    if not candidates:
        return None
    # Prefer the smallest source that still fills a 1080x1920 Short cleanly.
    _, _, chosen, width, height = min(candidates, key=lambda row: (row[0], row[1]))
    return {
        "video_id": video_id,
        "page_url": video["url"],
        "creator": str(user.get("name") or "Pexels creator").strip()[:120],
        "creator_url": user.get("url") if _trusted_pexels_url(user.get("url")) else "",
        "download_url": chosen["link"],
        "width": width,
        "height": height,
        "duration": duration,
        "license": "Pexels License",
        "license_url": PEXELS_LICENSE_URL,
        "reused": False,
    }


def download_fresh_backgrounds(api_key, theme, used_ids, destination=None, count=2,
                               request_get=None, max_pages=10):
    """Search and download unique, native portrait clips; never log the API key."""
    if not str(api_key or "").strip():
        raise PexelsUnavailable("Pexels API key is not configured")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("Background count must be a positive integer")
    get = request_get or requests.get
    used = {str(value) for value in (used_ids or ())}
    seen = set()
    fresh = []
    previously_used = []
    try:
        for page in range(1, max_pages + 1):
            response = get(
                SEARCH_URL,
                headers={"Authorization": str(api_key).strip()},
                params={"query": _theme_query(theme), "orientation": "portrait",
                        "size": "large", "per_page": 80, "page": page},
                timeout=(8, 30),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise PexelsUnavailable("Invalid Pexels search response")
            for video in payload.get("videos") or []:
                candidate = _candidate(video, used)
                if not candidate or candidate["video_id"] in seen:
                    continue
                seen.add(candidate["video_id"])
                (previously_used if candidate["video_id"] in used else fresh).append(candidate)
            if len(fresh) >= count:
                break
            if not payload.get("next_page"):
                break
        chosen = fresh[:count]
        # Reuse only after all currently returned eligible clips are exhausted.
        for row in previously_used:
            if len(chosen) >= count:
                break
            row["reused"] = True
            chosen.append(row)
        if len(chosen) < count:
            raise PexelsUnavailable("Not enough eligible Pexels portrait clips")

        folder = Path(destination) if destination else Path(tempfile.mkdtemp(prefix="quran-pexels-"))
        folder.mkdir(parents=True, exist_ok=True)
        completed = []
        for row in chosen:
            response = get(row["download_url"], timeout=(8, 60), stream=True)
            response.raise_for_status()
            if not _trusted_pexels_url(getattr(response, "url", row["download_url"])):
                raise PexelsUnavailable("Pexels returned an untrusted video download URL")
            content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
            if content_type and ("video/" not in content_type and "octet-stream" not in content_type):
                raise PexelsUnavailable("Pexels returned a non-video download")
            target = folder / ("pexels-" + row["video_id"] + ".mp4")
            partial = target.with_suffix(".mp4.part")
            digest = hashlib.sha256()
            total = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise PexelsUnavailable("Pexels clip exceeded the download size limit")
                    digest.update(chunk)
                    handle.write(chunk)
            if total == 0:
                raise PexelsUnavailable("Pexels returned an empty video")
            os.replace(partial, target)
            row.update(path=str(target.resolve()), sha256=digest.hexdigest())
            completed.append(target)
        return completed and chosen
    except Exception as error:
        # Remove incomplete downloads; retain no partial stock media on failure.
        if "folder" in locals():
            for path in folder.glob("pexels-*"):
                try:
                    path.unlink()
                except OSError:
                    pass
        if isinstance(error, PexelsUnavailable):
            raise
        raise PexelsUnavailable("Pexels search or download failed") from error
