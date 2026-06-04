#!/usr/bin/env python3
"""Find trending Quran Shorts globally, process, and upload to your channel."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import yt_dlp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy import VideoFileClip

HOURS_BACK = 24
MAX_SHORT_DURATION = 60
SEARCH_RESULTS_PER_QUERY = 30
FINAL_VIDEO = Path("final_shorts_1.mp4")
SPEED = 1.04

SEARCH_QUERIES = [
    "quran shorts",
    "quran recitation shorts",
    "surah shorts",
    "tilawah shorts",
    "holy quran shorts",
]

QURAN_KEYWORDS = (
    "quran",
    "surah",
    "sura",
    "tilawah",
    "recitation",
    "taraweeh",
    "islamic",
    "قرآن",
    "سورة",
    "تلاوة",
)

CLIENT_SECRETS_NAMES = ("client_secrets.json", "client_secrets.json.json")
TOKEN_FILE = Path("youtube_token.json")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
PRIVACY_STATUS = "public"


def find_client_secrets():
    for name in CLIENT_SECRETS_NAMES:
        path = Path(name)
        if path.exists():
            return path
    raise FileNotFoundError(
        "Place your OAuth file as client_secrets.json in this folder."
    )


def get_youtube_service():
    secrets = find_client_secrets()
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), YOUTUBE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def islamic_metadata(entry=None, index=1):
    """Build Islamic title, description, and tags for a Short."""
    if entry:
        channel = entry.get("channel_name") or entry.get("channel") or "Quran"
        original = entry.get("title") or "Quran Recitation"
        title = f"🌙 {channel} | {original} | Quran Shorts #Shorts"
    else:
        title = (
            f"🌙 Beautiful Quran Recitation #{index} | "
            "Islamic Reminder | Quran Shorts #Shorts"
        )

    if len(title) > 100:
        title = title[:97] + "..."

    channel_tag = ""
    if entry and entry.get("channel_name"):
        channel_tag = entry["channel_name"].replace(" ", "")

    tags = [
        "Quran",
        "Holy Quran",
        "Islam",
        "Muslim",
        "Islamic",
        "Quran Recitation",
        "Tilawah",
        "Quran Shorts",
        "Shorts",
        "Islamic Reminder",
        "Spiritual",
        "Allah",
        "Surah",
        "Ramadan",
    ]
    if channel_tag:
        tags.append(channel_tag)

    description = (
        "Beautiful Quranic recitation — daily Islamic reminder.\n\n"
        "Like, share, and subscribe for more Quran Shorts!\n\n"
        "#Quran #Islam #Muslim #QuranRecitation #Shorts #Islamic "
        "#Allah #Reminder #Tilawah"
    )
    if entry and entry.get("channel_name"):
        description = (
            f"Recitation featured from {entry['channel_name']}.\n\n" + description
        )

    return title, description, tags


def upload_video(youtube, video_path, entry=None, index=1):
    title, description, tags = islamic_metadata(entry, index)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=1024 * 1024,
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            print(f"    Upload {pct}%")

    video_id = response["id"]
    print(f"    Uploaded: https://www.youtube.com/watch?v={video_id}")
    return video_id


def upload_final_shorts(processed_videos):
    """Upload each final_shorts_N.mp4 to YouTube."""
    if not processed_videos:
        print("No videos to upload.")
        return

    print("\nAuthenticating with YouTube...")
    youtube = get_youtube_service()
    print("Uploading final_shorts videos...\n")

    for item in processed_videos:
        path = item["path"]
        if not path.exists():
            print(f"  Skipping missing file: {path}")
            continue
        print(f"  Uploading {path.name}...")
        try:
            upload_video(
                youtube,
                path,
                entry=item.get("entry"),
                index=item["index"],
            )
        except Exception as exc:
            print(f"    Upload failed: {exc}")


def build_base_opts(browser=None, cookies_file=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    elif browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def build_download_opts(browser=None, cookies_file=None, outtmpl="%(id)s.%(ext)s"):
    opts = build_base_opts(browser, cookies_file)
    opts.update(
        {
            "format": (
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=720]+bestaudio/"
                "best[height<=720][ext=mp4]/"
                "best[height<=720]/"
                "best"
            ),
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "overwrites": True,
        }
    )
    return opts


def entry_uploaded_at(entry):
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if ts:
        return datetime.fromtimestamp(ts)
    upload_date = entry.get("upload_date")
    if upload_date:
        return datetime.strptime(upload_date, "%Y%m%d")
    return None


def is_recent(entry, hours=HOURS_BACK):
    if hours is None:
        return True
    uploaded = entry_uploaded_at(entry)
    if uploaded is None:
        return False
    return uploaded >= datetime.now() - timedelta(hours=hours)


def is_short_video(entry):
    duration = entry.get("duration")
    if duration is not None and duration > MAX_SHORT_DURATION:
        return False
    return True


def is_quran_related(entry):
    parts = [
        entry.get("title") or "",
        entry.get("description") or "",
        entry.get("channel") or "",
        entry.get("uploader") or "",
    ]
    text = " ".join(parts).lower()
    return any(keyword in text for keyword in QURAN_KEYWORDS)


def entry_views(entry):
    return entry.get("view_count") or 0


def entry_url(entry):
    if entry.get("url"):
        return entry["url"]
    video_id = entry.get("id")
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def search_query_entries(query, ydl, limit=SEARCH_RESULTS_PER_QUERY):
    search_url = f"ytsearchdate{limit}:{query}"
    info = ydl.extract_info(search_url, download=False)
    if not info:
        return []
    return [e for e in (info.get("entries") or []) if e]


def collect_search_candidates(browser=None, cookies_file=None):
    """Search YouTube globally for recent Quran Shorts candidates."""
    opts = build_base_opts(browser, cookies_file)
    opts["extract_flat"] = "in_playlist"

    seen_ids = set()
    candidates = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        for query in SEARCH_QUERIES:
            print(f"  Searching globally: {query}")
            try:
                for entry in search_query_entries(query, ydl):
                    vid = entry.get("id")
                    if not vid or vid in seen_ids:
                        continue
                    seen_ids.add(vid)
                    candidates.append(entry)
            except Exception as exc:
                print(f"  Search failed for '{query}': {exc}")
    return candidates


def enrich_entries(entries, browser=None, cookies_file=None):
    opts = build_base_opts(browser, cookies_file)
    enriched = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        for entry in entries:
            url = entry_url(entry)
            if not url:
                continue
            try:
                full = ydl.extract_info(url, download=False)
                if full:
                    full["channel_name"] = (
                        full.get("channel")
                        or full.get("uploader")
                        or entry.get("channel")
                        or ""
                    )
                    enriched.append(full)
            except Exception as exc:
                print(f"  Could not load metadata for {url}: {exc}")
    return enriched


def select_best_short(entries):
    """Pick the top Quran Short, trying 24h, then 7 days, then any time."""
    # Step 1: Try last 24 hours
    eligible = [
        e for e in entries
        if is_recent(e, hours=24)
        and is_short_video(e)
        and is_quran_related(e)
        and entry_views(e) > 0
    ]
    if eligible:
        eligible.sort(key=entry_views, reverse=True)
        return eligible[0]

    # Step 2: Fallback to last 7 days (168 hours)
    print("  No videos in last 24h. Expanding search to last 7 days...")
    eligible = [
        e for e in entries
        if is_recent(e, hours=168)
        and is_short_video(e)
        and is_quran_related(e)
        and entry_views(e) > 0
    ]
    if eligible:
        eligible.sort(key=entry_views, reverse=True)
        return eligible[0]

    # Step 3: Ultimate fallback - any qualifying video regardless of date
    print("  No videos in last 7 days. Using ultimate fallback (any timeframe)...")
    eligible = [
        e for e in entries
        if is_short_video(e)
        and is_quran_related(e)
        and entry_views(e) > 0
    ]
    if eligible:
        eligible.sort(key=entry_views, reverse=True)
        return eligible[0]

    return None


def download_video(url, dest_stem, browser=None, cookies_file=None):
    opts = build_download_opts(
        browser, cookies_file, outtmpl=str(dest_stem) + ".%(ext)s"
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    matches = list(Path(".").glob(dest_stem.name + ".*"))
    if not matches:
        raise FileNotFoundError(f"Download failed for {url}")
    return matches[0]


def _adjust_frame(frame):
    adjusted = frame.astype(np.float32)
    adjusted = adjusted * 1.02 + 4
    return np.clip(adjusted, 0, 255).astype(np.uint8)


def process_video(input_path, output_path, speed=SPEED):
    clip = VideoFileClip(str(input_path))
    try:
        clip = clip.image_transform(_adjust_frame).with_speed_scaled(speed)
        clip.write_videofile(
            str(output_path),
            codec="libx264",
            audio_codec="aac",
            logger=None,
        )
    finally:
        clip.close()
    print(f"  Saved {output_path}")


def collect_existing_final_shorts():
    paths = sorted(Path(".").glob("final_shorts_*.mp4"))
    videos = []
    for path in paths:
        try:
            index = int(path.stem.rsplit("_", 1)[-1])
        except ValueError:
            index = len(videos) + 1
        videos.append({"path": path, "entry": None, "index": index})
    return videos


def parse_args():
    args = [a for a in sys.argv[1:] if a != "--upload-only"]
    upload_only = "--upload-only" in sys.argv

    browser = None
    cookies_file = None
    if args:
        extra = args[0]
        if extra.lower() != "none":
            path = Path(extra)
            if path.suffix.lower() == ".txt":
                cookies_file = path
            else:
                browser = extra
    return browser, cookies_file, upload_only


def main():
    browser, cookies_file, upload_only = parse_args()
    if browser:
        print(f"Using {browser} cookies — close {browser} completely if this fails.")

    processed_videos = []

    if upload_only:
        processed_videos = collect_existing_final_shorts()
        if not processed_videos:
            print("No final_shorts_*.mp4 files found.")
            sys.exit(1)
        print(f"Found {len(processed_videos)} video(s) to upload.\n")
    else:
        print("Searching YouTube globally for trending Quran Shorts...\n")

        candidates = collect_search_candidates(browser, cookies_file)
        print(f"  Found {len(candidates)} candidates. Loading details...\n")
        detailed = enrich_entries(candidates, browser, cookies_file)
        best = select_best_short(detailed)

        if not best:
            print("No qualifying Quran Shorts found at all. Try different search keywords.")
            sys.exit(1)

        title = best.get("title", "Unknown")
        views = entry_views(best)
        channel = best.get("channel_name", best.get("channel", ""))
        print(f"\nBest pick selected successfully:\n")
        print(f"  {title} ({views:,} views) — {channel}")

        url = entry_url(best)
        if not url:
            print("Selected video has no URL.")
            sys.exit(1)

        print("\nDownloading and processing...\n")
        temp_stem = Path("_temp_1")
        print(f"  {best.get('title', url)}")
        try:
            for old in Path(".").glob(f"{temp_stem.name}.*"):
                old.unlink()

            downloaded = download_video(url, temp_stem, browser, cookies_file)
            process_video(downloaded, FINAL_VIDEO)
            downloaded.unlink(missing_ok=True)
            processed_videos.append(
                {"path": FINAL_VIDEO, "entry": best, "index": 1}
            )
        except Exception as exc:
            print(f"  Failed: {exc}")

    upload_final_shorts(processed_videos)
    print("\nDone.")


if __name__ == "__main__":
    main()