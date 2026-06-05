#!/usr/bin/env python3
"""Multi-Platform Quran Shorts downloader and uploader."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
import importlib

# Ensure UTF-8 output on all platforms (Windows, GitHub Actions, etc.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import yt_dlp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Keep the automatic MoviePy version check supporting both v1 and v2
try:
    # Try MoviePy v2 first (this matches your local installation)
    from moviepy import VideoFileClip
    MOVIEPY_V1 = False
except ImportError:
    # Fall back to MoviePy v1 dynamically to silence static analyzer warnings
    try:
        moviepy_editor = importlib.import_module("moviepy.editor")
        VideoFileClip = moviepy_editor.VideoFileClip
        vfx = importlib.import_module("moviepy.video.fx.all")
        MOVIEPY_V1 = True
    except ImportError:
        print("Error: moviepy is not installed. Please run: pip install moviepy")
        sys.exit(1)

MAX_SHORT_DURATION = 60
FINAL_VIDEO = Path("final_shorts_1.mp4")
SPEED = 1.04
MAX_VIDEOS_TO_UPLOAD = 3

# Dynamic search queries — bot picks fresh Quran Shorts automatically each run
SEARCH_QUERIES = [
    "ytsearch50:quran shorts recitation #shorts",
    "ytsearch50:beautiful quran recitation short",
]

COOKIE_FILE_OPTIONS = ["cookies.txt", "youtube_cookies.txt"]
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
    import os
    creds = None

    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), YOUTUBE_SCOPES)
        except Exception as e:
            print(f"  [Warning] Failed to load credentials from {TOKEN_FILE}: {e}")

    # Check if running in a CI/headless environment (e.g. GitHub Actions)
    is_ci = os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true"

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                print("  [Info] Refreshing YouTube OAuth token...")
                creds.refresh(Request())
                TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
                refreshed = True
                print("  [Info] Token refreshed successfully.")
            except Exception as e:
                print(f"  [Warning] Failed to refresh token: {e}")

        if not refreshed:
            if is_ci:
                print("\n" + "=" * 80)
                print("  [ERROR] Running in a headless/CI environment (GitHub Actions), but")
                print("  the YouTube OAuth token is missing, expired, or invalid and cannot")
                print("  be refreshed automatically.")
                print("  Please re-run this script locally to authenticate and generate a new")
                print("  youtube_token.json file, then update your GITHUB_TOKEN / YOUTUBE_TOKEN secret.")
                print("=" * 80 + "\n")
                sys.exit(1)
            else:
                print("  [Info] Starting local server for authentication...")
                secrets = find_client_secrets()
                flow = InstalledAppFlow.from_client_secrets_file(str(secrets), YOUTUBE_SCOPES)
                creds = flow.run_local_server(port=0)
                TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def islamic_metadata(entry=None, index=1):
    if entry:
        channel = entry.get("uploader") or entry.get("channel") or "Quran"
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
    if entry and entry.get("uploader"):
        channel_tag = entry["uploader"].replace(" ", "")

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
    if entry and entry.get("uploader"):
        description = (
            f"Recitation featured from {entry['uploader']}.\n\n" + description
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


def build_base_opts(browser=None, cookies_file=None, client=None):
    opts = {
        "quiet": False,
        "no_warnings": False,
        "noprogress": True,
        "ignoreerrors": False,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
    }
    
    # Configure specific player client if provided, otherwise default to a resilient list
    target_client = [client] if client else ["tv_embedded", "ios", "web_embedded", "android"]
    opts["extractor_args"] = {
        "youtube": {
            "player_client": target_client,
        }
    }

    if cookies_file == "none":
        return opts

    if not cookies_file and not browser:
        for option in COOKIE_FILE_OPTIONS:
            path = Path(option)
            if path.exists() and path.stat().st_size > 0:
                # Check if it has a valid Netscape cookie file header
                try:
                    content = path.read_text(errors="ignore")
                    if "Netscape" in content or "cookietxt" in content or content.startswith("#"):
                        cookies_file = path
                        print(f"  [Info] Automatically using cookies from: {option}")
                        break
                    else:
                        print(f"  [Warning] Skipping invalid/empty cookies file: {option}")
                except Exception:
                    pass

    if cookies_file and cookies_file != "none":
        opts["cookiefile"] = str(cookies_file)
    elif browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def build_download_opts(browser=None, cookies_file=None, outtmpl="%(id)s.%(ext)s", client=None):
    opts = build_base_opts(browser, cookies_file, client=client)
    opts.update(
        {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "overwrites": True,
        }
    )
    return opts


def download_video(url, dest_stem, browser=None, cookies_file=None):
    # Try clients sequentially so one blocked client doesn't abort the download
    clients_to_try = ["tv_embedded", "ios", "web_embedded", "android"]
    last_exc = None

    for client in clients_to_try:
        try:
            print(f"    [yt-dlp] Downloading with client format: {client}")
            opts = build_download_opts(
                browser, cookies_file, outtmpl=str(dest_stem) + ".%(ext)s", client=client
            )
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            matches = list(Path(".").glob(dest_stem.name + ".*"))
            if matches:
                return matches[0]
        except Exception as e:
            last_exc = e
            # Clean up partial download files before retrying the next client
            for temp_file in Path(".").glob(dest_stem.name + ".*"):
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    if last_exc:
        raise last_exc
    raise FileNotFoundError(f"Download failed for {url}")


def _adjust_frame(frame):
    adjusted = frame.astype(np.float32)
    adjusted = adjusted * 1.02 + 4
    return np.clip(adjusted, 0, 255).astype(np.uint8)


def process_video(input_path, output_path, speed=SPEED):
    clip = VideoFileClip(str(input_path))
    try:
        if MOVIEPY_V1:
            clip = clip.fl_image(_adjust_frame)
            clip = clip.fx(vfx.speedx, speed)
        else:
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


def get_video_info(url, browser=None, cookies_file=None):
    opts = build_base_opts(browser, cookies_file)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def search_quran_shorts(browser=None, cookies_file=None):
    """Search YouTube dynamically for fresh Quran Shorts (<=MAX_SHORT_DURATION sec)."""
    found_urls = []
    seen_ids = set()

    search_opts = {
        "quiet": False,
        "no_warnings": False,
        "noprogress": True,
        "ignoreerrors": True,
        "flat_playlist": True,
        "extract_flat": True,
        "socket_timeout": 30,
    }
    if cookies_file and cookies_file != "none" and Path(cookies_file).exists():
        search_opts["cookiefile"] = str(cookies_file)
    elif browser:
        search_opts["cookiesfrombrowser"] = (browser,)
    else:
        # Auto-detect cookie file
        for opt in COOKIE_FILE_OPTIONS:
            p = Path(opt)
            if p.exists() and p.stat().st_size > 0:
                try:
                    content = p.read_text(errors="ignore")
                    if "Netscape" in content or content.startswith("#"):
                        search_opts["cookiefile"] = str(p)
                        break
                except Exception:
                    pass

    for query in SEARCH_QUERIES:
        if len(found_urls) >= MAX_VIDEOS_TO_UPLOAD:
            break
        print(f"  [Search] Running: {query}")
        try:
            with yt_dlp.YoutubeDL(search_opts) as ydl:
                result = ydl.extract_info(query, download=False)

            entries = result.get("entries", []) if result else []
            for entry in entries:
                if len(found_urls) >= MAX_VIDEOS_TO_UPLOAD:
                    break
                if not entry:
                    continue
                video_id = entry.get("id") or entry.get("url", "")
                if video_id in seen_ids:
                    continue
                duration = entry.get("duration") or 0
                if duration and duration > MAX_SHORT_DURATION:
                    continue
                url = entry.get("url") or entry.get("webpage_url", "")
                if not url or "youtube.com" not in url and "youtu.be" not in url:
                    url = f"https://www.youtube.com/watch?v={video_id}"
                seen_ids.add(video_id)
                found_urls.append({"url": url, "entry": entry})
                print(f"    Found: [{duration}s] {entry.get('title', url)}")
        except Exception as e:
            print(f"  [Warning] Search failed for query '{query}': {e}")
            continue

    return found_urls


def main():
    browser, cookies_file, upload_only = parse_args()
    processed_videos = []

    if upload_only:
        processed_videos = collect_existing_final_shorts()
        if not processed_videos:
            print("No final_shorts_*.mp4 files found.")
            sys.exit(1)
        print(f"Found {len(processed_videos)} video(s) to upload.\n")
    else:
        print("Searching YouTube for fresh Quran Shorts...\n")
        video_items = search_quran_shorts(browser, cookies_file)

        if not video_items:
            print("Error: Could not find any Quran Shorts via YouTube search.")
            sys.exit(1)

        print(f"\nFound {len(video_items)} video(s) to download and process.\n")
        temp_stem = Path("_temp_1")
        success_count = 0

        for attempt, item in enumerate(video_items, start=1):
            url = item["url"]
            entry = item["entry"]
            print(f"Processing video [{attempt}/{len(video_items)}]: {url}")
            try:
                for old in Path(".").glob(f"{temp_stem.name}.*"):
                    old.unlink()

                try:
                    downloaded = download_video(url, temp_stem, browser, cookies_file)
                except Exception as e:
                    print(f"    Download failed ({e}). Retrying WITHOUT cookies...")
                    downloaded = download_video(url, temp_stem, browser, cookies_file="none")

                output_name = Path(f"final_shorts_{attempt}.mp4")
                process_video(downloaded, output_name)
                downloaded.unlink(missing_ok=True)

                processed_videos.append(
                    {"path": output_name, "entry": entry, "index": attempt}
                )
                success_count += 1
            except Exception as exc:
                print(f"  Failed to download or process '{url}': {exc}\n")
                continue

        if success_count == 0:
            print("All video links failed to download/process.")
            sys.exit(1)

    upload_final_shorts(processed_videos)
    print("\nDone.")


if __name__ == "__main__":
    main()