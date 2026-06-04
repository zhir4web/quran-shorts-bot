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

import importlib

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
        # Try all major client types to maximize the chance of finding one not blocked by CI IP detection
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "tv_embedded", "web_embedded", "android", "tv", "web"],
            }
        },
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
    }

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

    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    elif browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def build_download_opts(browser=None, cookies_file=None, outtmpl="%(id)s.%(ext)s"):
    opts = build_base_opts(browser, cookies_file)
    opts.update(
        {
            # Broad fallback chain: prefer mp4, fall back to any best available
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "overwrites": True,
        }
    )
    return opts


def entry_url(entry):
    if entry.get("url"):
        return entry["url"]
    video_id = entry.get("id")
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def search_query_entries(query, ydl, limit=SEARCH_RESULTS_PER_QUERY):
    search_url = f"ytsearch{limit}:{query}"
    info = ydl.extract_info(search_url, download=False)
    if not info:
        return []
    return [e for e in (info.get("entries") or []) if e]


def collect_search_candidates(browser=None, cookies_file=None):
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
        # لێرەدا خۆکارانە فۆرماتەکە ڕێکدەخات بەپێی جۆری وەشانی moviepy
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
        print("Searching YouTube globally for trending Quran Shorts...\n")

        candidates = collect_search_candidates(browser, cookies_file)
        if not candidates:
            print("No candidates found during search. Check internet or queries.")
            sys.exit(1)

        print("\nDownloading and processing...\n")
        temp_stem = Path("_temp_1")
        MAX_ATTEMPTS = 10  # try up to 10 candidates before giving up
        success = False

        for attempt, best in enumerate(candidates[:MAX_ATTEMPTS], start=1):
            best["channel_name"] = (
                best.get("uploader") or best.get("channel") or "Quran Recitation"
            )
            url = entry_url(best)
            if not url:
                continue

            title = best.get("title", "Unknown")
            channel = best.get("channel_name", "")
            print(f"  [{attempt}/{MAX_ATTEMPTS}] Trying: {title} — {channel}")

            try:
                for old in Path(".").glob(f"{temp_stem.name}.*"):
                    old.unlink()

                try:
                    downloaded = download_video(url, temp_stem, browser, cookies_file)
                except Exception as e:
                    # If downloading with cookies fails, try downloading without cookies as fallback
                    if cookies_file:
                        print(f"    Download failed with cookies. Retrying WITHOUT cookies...")
                        downloaded = download_video(url, temp_stem, browser, cookies_file=None)
                    else:
                        raise e

                process_video(downloaded, FINAL_VIDEO)
                downloaded.unlink(missing_ok=True)
                processed_videos.append(
                    {"path": FINAL_VIDEO, "entry": best, "index": 1}
                )
                success = True
                break  # done — stop trying more candidates
            except Exception as exc:
                print(f"    Failed ({exc}), trying next candidate...")
                continue

        if not success:
            print(f"All {MAX_ATTEMPTS} candidates failed to download.")
            sys.exit(1)

    upload_final_shorts(processed_videos)
    print("\nDone.")


if __name__ == "__main__":
    main()