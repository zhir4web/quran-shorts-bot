"""Quran Shorts: explicit licensed queue, local rendering, durable upload ledger."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("quran-bot")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/youtube.force-ssl",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]

CTA_COMMENTS = (
    "اذكر الله بكلمة طيبة في التعليقات، واشترك ليصلك المزيد من القرآن. 🤍\nLeave a short dhikr below and subscribe for more Quran recitations.",
    "ما الدعاء الذي تحب أن تردده اليوم؟ اكتبه في التعليقات واشترك للمزيد.\nShare a short dua for today, and subscribe for more peaceful recitations.",
    "سبحان الله، والحمد لله، والله أكبر. أضف ذكرك في التعليقات واشترك.\nShare a brief dhikr in the comments and subscribe to keep the Quran close.",
    "اكتب دعاءً قصيرًا لمن يقرأ تعليقك، واشترك ليصلك كل جديد.\nLeave a short dua for everyone reading, and subscribe for upcoming recitations.",
    "اجعل تعليقك ذكرًا نافعًا، واشترك لتستمع إلى آيات جديدة.\nMake your comment a beautiful dhikr, and subscribe for new Quran verses.",
    "اللهم اجعل القرآن نور قلوبنا. شارك دعاءك واشترك للمزيد.\nShare a heartfelt dua below and subscribe for more Quran recitation.",
    "أي ذكر يطمئن قلبك؟ اكتبه في التعليقات واشترك لتتابع التلاوات.\nWhich dhikr brings you peace? Comment it below and subscribe for more.",
    "اترك كلمة طيبة أو دعاءً قصيرًا، واشترك حتى لا تفوتك التلاوة القادمة.\nLeave a kind dua or dhikr, and subscribe so you do not miss the next recitation.",
)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def positive(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"{name} must be greater than zero and at most {maximum}")
    return value


def load_queue(path):
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Queue must contain an items array")
    seen = set()
    for item in data["items"]:
        if not isinstance(item, dict):
            raise ValueError("Each queue item must be an object")
        key = item.get("id", "")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key) or key in seen:
            raise ValueError("Each item needs a unique id using letters, digits, - or _")
        seen.add(key)
        if item.get("mode") not in ("compose", "video"):
            raise ValueError(f"{key}: mode must be compose or video")
        for field in ("title", "attribution", "rights"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"{key}: {field} is required")
        if len(item["title"]) > 100 or any(c in item["title"] for c in "<>"):
            raise ValueError(f"{key}: invalid YouTube title")
        if item.get("rights_confirmed") is not True:
            raise ValueError(f"{key}: confirm permission covering every audio, video and image asset")
        if not isinstance(item.get("made_for_kids"), bool):
            raise ValueError(f"{key}: made_for_kids must be true or false")
        positive(item.get("duration"), f"{key} duration", 60)
        minimum = item.get('min_duration_seconds', 0)
        if (isinstance(minimum, bool) or not isinstance(minimum, (int, float))
                or not math.isfinite(minimum) or not 0 <= minimum <= item['duration']):
            raise ValueError(f'{key}: invalid minimum duration')
        start = item.get("start", 0)
        if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0:
            raise ValueError(f"{key}: start must be a nonnegative number")
        if not isinstance(item.get("source"), str) or not item["source"].strip():
            raise ValueError(f"{key}: source file or HTTPS URL is required")
        if not isinstance(item.get("description", ""), str):
            raise ValueError(f"{key}: description must be text")
        if item.get("background") is not None and not isinstance(item["background"], str):
            raise ValueError(f"{key}: background must be an image path")
        if item.get("background_motion") not in (None, "calm_rain", "premium_motion", "real_video"):
            raise ValueError(f"{key}: unsupported background motion")
        if item.get("background_video") is not None:
            if not isinstance(item["background_video"], str) or not item["background_video"].strip():
                raise ValueError(f"{key}: background video must be a file path")
        playlist = item.get("background_playlist")
        if playlist is not None:
            if (not isinstance(playlist, list) or not playlist or len(playlist) > 8
                    or any(not isinstance(clip, str) or not clip.strip() for clip in playlist)):
                raise ValueError(f"{key}: background_playlist must be a short list of clip paths")
            if not item.get("background_video"):
                raise ValueError(f"{key}: background_playlist requires background_video")
        if item.get("motion_overlay") is not None and not isinstance(item["motion_overlay"], str):
            raise ValueError(f"{key}: motion overlay must be an image path")
        if item['mode'] == 'video' and any(item.get(field) for field in
                ('background', 'background_video', 'background_motion', 'motion_overlay')):
            raise ValueError(f'{key}: background options require compose mode')
        if item.get('background_video') and (item.get('motion_overlay') or
                item.get('background_motion') in ('calm_rain', 'premium_motion')):
            raise ValueError(f'{key}: filmed backgrounds cannot use synthetic motion overlays')
        if item.get('background_motion') == 'real_video' and not item.get('background_video'):
            raise ValueError(f'{key}: real_video requires a background video')
        if len(description(item)) > 5000:
            raise ValueError(f"{key}: description is too long")
    return data["items"]


def description(item):
    if item.get("metadata_complete") is True:
        return item.get("description", "").strip()
    return f"{item.get('description', '')}\n\n{item['attribution']}\n\n#Quran #Shorts".strip()


def dynamic_tags(item):
    candidates = ["Quran", "Quran recitation", "Islamic shorts",
                  item.get("surah_ar"), item.get("surah_en"), item.get("reciter_ar"),
                  item.get("reciter_en"), "quran verses", "quran audio", "quran shorts", "islam"]
    tags, seen, total = [], set(), 0
    for value in candidates:
        tag = str(value or "").strip()
        key = tag.casefold()
        if not tag or key in seen or len(tag) > 100:
            continue
        # YouTube limits the combined tag field to 500 characters. Count commas too.
        addition = len(tag) + (1 if tags else 0)
        if total + addition > 500:
            break
        tags.append(tag)
        seen.add(key)
        total += addition
    return tags


def comment_variant(video_id):
    index = int(hashlib.sha256(video_id.encode("utf-8")).hexdigest(), 16) % len(CTA_COMMENTS)
    return index, CTA_COMMENTS[index]


def post_cta_comment(youtube, video_id):
    index, text = comment_variant(video_id)
    try:
        youtube.commentThreads().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id,
                               "topLevelComment": {"snippet": {"textOriginal": text}}}},
        ).execute()
        return index, True
    except Exception:
        LOG.warning("CTA comment could not be posted; the uploaded video remains valid")
        return index, False


def fingerprint(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Ledger:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, video_id TEXT, output_hash TEXT)")
        self.db.commit()

    def row(self, key):
        return self.db.execute("SELECT fingerprint,status,video_id,output_hash FROM jobs WHERE id=?", (key,)).fetchone()

    def record(self, item, status, video_id=None, output_hash=None):
        self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, video_id=excluded.video_id, output_hash=excluded.output_hash", (item["id"], fingerprint(item), status, video_id, output_hash))
        self.db.commit()

    def close(self):
        self.db.close()


@contextmanager
def process_lock(path):
    # OS locks release even after a crash. Keep the lock file, never unlink it.
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another bot run is active") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_media(args):
    result = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if result.returncode:
        raise RuntimeError("Media processing failed: " + result.stderr[-1500:])


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_file(item, base, folder):
    source = item["source"]
    if source.startswith("https://"):
        import yt_dlp
        options = {"outtmpl": str(folder / "source.%(ext)s"), "noplaylist": True, "quiet": True,
                   "socket_timeout": 30, "retries": 3, "ffmpeg_location": ffmpeg(),
                   "format": "bestaudio/best" if item["mode"] == "compose" else "bestvideo+bestaudio/best",
                   "merge_output_format": "mp4"}
        with yt_dlp.YoutubeDL(options) as client:
            info = client.extract_info(source, download=True)
            if not info or info.get("_type") == "playlist":
                raise ValueError("Expected one media source")
            path = Path(info.get("filepath") or client.prepare_filename(info))
            if not path.exists():
                path = path.with_suffix(".mp4")
    else:
        path = (base / source).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Source missing: {path}")
    return path


def theme_clips(theme, base):
    """List only manifest-reviewed clips for a theme, deterministic order."""
    if not isinstance(theme, str) or not re.fullmatch(r"[a-z0-9_]+", theme):
        return []
    folder = Path(base) / "assets" / "backgrounds" / "video"
    manifest = folder / "LICENSES.md"
    try:
        text = manifest.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, OSError):
        # Fail closed for production assets: an absent manifest cannot prove rights.
        return []
    licensed = set(re.findall(r"(?<![A-Za-z0-9_-])\x60([A-Za-z0-9_-]+\.mp4)\x60", text, flags=re.IGNORECASE))
    primary = folder / f"{theme}.mp4"
    if primary.name not in licensed or not primary.is_file():
        return []
    clips = [primary]
    for extra in sorted(folder.glob(f"{theme}_*.mp4")):
        if extra.is_file() and extra.name in licensed:
            clips.append(extra)
    return clips


def split_background_segments(clips, total_seconds):
    """Assign each reviewed clip an equal share of the recitation duration.

    Every clip is used before any clip repeats, so a Short built from three
    clips shows three different scenes instead of one clip looping.
    """
    if not clips:
        raise ValueError("A background playlist needs at least one reviewed clip")
    total = float(total_seconds)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Background segments need a positive duration")
    share = total / len(clips)
    return [(clip, share) for clip in clips]


def submit_video(source, theme, queue_path, title, attribution, rights, *, min_duration=30,
                 made_for_kids=False, base=None, state=None):
    """Register a user-provided clip for review, rendering and publication.

    The clip is copied into the reviewed theme folder as the next numbered
    clip, its probe metadata is recorded, and a queue item is appended. The
    item stays pending until the operator confirms the licence row, so nothing
    reaches a scheduled render before a human has reviewed the source.
    """
    import shutil
    base = Path(base or ROOT)
    state = Path(state or (base / "state"))
    if not re.fullmatch(r"[a-z_]+", str(theme or "")):
        raise ValueError("Theme must be a lowercase theme name such as forest_rain")
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Clip not found: {source}")
    if source.suffix.lower() != ".mp4":
        raise ValueError("Background clips must be MP4 files")
    destination_dir = base / "assets" / "backgrounds" / "video"
    destination_dir.mkdir(parents=True, exist_ok=True)
    index = 2
    while (destination_dir / f"{theme}_{index}.mp4").is_file():
        index += 1
    destination = destination_dir / f"{theme}_{index}.mp4"
    if destination.exists():
        raise RuntimeError("Clip destination already exists")
    item = {"id": f"clip-{theme}-{index}", "mode": "video", "source": str(source),
            "title": str(title or "").strip(), "attribution": str(attribution or "").strip(),
            "rights": str(rights or "").strip(), "rights_confirmed": False,
            "made_for_kids": bool(made_for_kids), "duration": 60, "min_duration_seconds": min_duration,
            "background_motion": "real_video", "submit_theme": theme}
    # Validate the probe the same way a real render would.
    validate_background_source(source)
    shutil.copyfile(source, destination)
    item["source"] = str(destination)
    pending = state / "submissions"
    pending.mkdir(parents=True, exist_ok=True)
    record = dict(item)
    record["license_confirmed"] = False
    atomic_json(pending / f"{item['id']}.json", record)
    queue_path = Path(queue_path)
    queue = read_json(queue_path) if queue_path.is_file() else {"items": []}
    if not isinstance(queue, dict) or not isinstance(queue.get("items"), list):
        raise ValueError("Queue file must contain an items array")
    queue["items"].append(dict(item))
    atomic_json(queue_path, queue)
    print(f"Submitted: {destination.name} — add a licence row to LICENSES.md and confirm it before publishing")
    return destination


def validate_background_source(path):
    """Reject filmed sources that would need a destructive low-resolution upscale."""
    import imageio_ffmpeg

    try:
        reader = imageio_ffmpeg.read_frames(str(path))
    except RuntimeError:
        raise ValueError(f"Background source {path} is not a readable MP4 video") from None
    try:
        metadata = next(reader)
    except StopIteration:
        raise ValueError(f"Background source {path} has no readable video frames") from None
    finally:
        reader.close()
    width, height = metadata.get("size", (0, 0))
    fps = metadata.get("fps")
    if not width or not height:
        raise ValueError(f"Background source {path} has no readable video dimensions")
    if min(width, height) < 1080:
        raise ValueError(
            f"Background source {path} is only {width}x{height}; "
            "a filmed background must have a shorter dimension of at least 1080px"
        )
    LOG.info("Background source %s: %sx%s at %s fps", path, width, height, fps)


def render(item, base, folder):
    folder.mkdir(parents=True, exist_ok=True)
    source = source_file(item, base, folder)
    start = str(item.get("start", 0))
    minimum = float(item.get("min_duration_seconds", 0))
    if float(item["duration"]) < minimum:
        raise ValueError("Requested segment is below the minimum video duration")
    duration = str(item["duration"])
    scale = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x081b21,setsar=1"
    if item["mode"] == "compose":
        if item.get("background_video"):
            playlist = item.get("background_playlist") or [item["background_video"]]
            inputs = []
            for clip in playlist:
                clip_path = (base / clip).resolve()
                if not clip_path.is_file():
                    raise FileNotFoundError(f"Background clip missing: {clip_path}")
                validate_background_source(clip_path)
                inputs += ["-stream_loop", "-1", "-i", str(clip_path)]
            if item.get("background"):
                card = (base / item["background"]).resolve()
                if not card.is_file():
                    raise FileNotFoundError(f"Card overlay missing: {card}")
                inputs += ["-loop", "1", "-framerate", "30", "-i", str(card)]
        elif item.get("background"):
            background = (base / item["background"]).resolve()
            if not background.is_file():
                raise FileNotFoundError(f"Background missing: {background}")
            inputs = ["-loop", "1", "-framerate", "30", "-i", str(background)]
        else:
            inputs = ["-f", "lavfi", "-i", "color=c=0x081b21:s=1080x1920:r=30"]
        inputs += ["-ss", start, "-i", str(source)]
        mapping = ["-map", "0:v:0", "-map", "1:a:0"]
        if item.get("motion_overlay"):
            overlay = (base / item["motion_overlay"]).resolve()
            if not overlay.is_file():
                raise FileNotFoundError(f"Motion overlay missing: {overlay}")
            inputs += ["-loop", "1", "-framerate", "30", "-i", str(overlay)]
    else:
        inputs = ["-ss", start, "-i", str(source)]
        mapping = ["-map", "0:v:0", "-map", "0:a:0"]
    # Pad preserves existing Quran text; recitation speed and pitch are untouched.
    target = folder / "video.mp4"
    temporary = folder / "rendering.mp4"
    filters = ["-vf", scale]
    if item.get("background_video"):
        # Concatenate every reviewed clip of the theme: each clip covers its
        # share of the recitation, so a Short shows several scenes instead of
        # one clip restarting. The slow crop drift also hides any loop point.
        playlist = item.get("background_playlist") or [item["background_video"]]
        card_index = len(playlist) if item.get("background") else None
        audio_index = len(playlist) + (1 if card_index is not None else 0)
        chains = []
        for index, (clip, share) in enumerate(split_background_segments(playlist, item["duration"])):
            chains.append(
                f"[{index}:v]scale=1120:1992:flags=lanczos:force_original_aspect_ratio=increase,"
                f"crop=1080:1920:x='20+12*sin(t/5)':y='36+10*cos(t/6)',setsar=1,"
                f"trim=duration={float(share):.3f},setpts=PTS-STARTPTS[seg{index}]")
        joined = "".join(f"[seg{index}]" for index in range(len(playlist)))
        if card_index is not None:
            # The card is a transparent RGBA PNG; composite it over the
            # stitched background after the quality-preserving scale/crop.
            chains.append(joined + f"concat=n={len(playlist)}:v=1:a=0[basev]")
            chains.append(f"[{card_index}:v]format=rgba[card]")
            chains.append("[basev][card]overlay=0:0:format=auto,setsar=1[v]")
            mapping = ["-map", "[v]", "-map", f"{audio_index}:a:0"]
        else:
            chains.append(joined + f"concat=n={len(playlist)}:v=1:a=0,setsar=1[v]")
            mapping = ["-map", "[v]", "-map", f"{audio_index}:a:0"]
        filters = ["-filter_complex", ";".join(chains)]
    if item.get("background_motion") in ("calm_rain", "premium_motion") and item.get("motion_overlay"):
        # The 3840px rain sheet travels over a 1920px viewport and loops. This
        # creates clearly visible motion while keeping all artwork project-owned.
        graph = ("[0:v]scale=1120:1992,crop=1080:1920:"
                 "x='20+12*sin(t/5)':y='36+10*cos(t/6)',"
                 "eq=brightness='0.012*sin(t/3)'[base];"
                 "[2:v]format=rgba[rain];"
                 "[base][rain]overlay=x=0:y='-1920+mod(t*620,1920)':shortest=1,setsar=1[v]")
        filters = ["-filter_complex", graph]
        mapping = ["-map", "[v]", "-map", "1:a:0"]
    elif item.get("background_motion") in ("calm_rain", "premium_motion"):
        scale = ("scale=1120:1992,crop=1080:1920:"
                 "x='20+12*sin(t/5)':y='36+10*cos(t/6)',"
                 "noise=alls=5:allf=t+u,eq=brightness='0.008*sin(t/4)',setsar=1")
        filters = ["-vf", scale]
    encode = (["-c:v", "libx264", "-preset", "medium", "-b:v", "10M",
               "-maxrate", "12M", "-bufsize", "24M"]
              if item.get("background_video")
              else ["-c:v", "libx264", "-preset", "fast", "-crf", "20"])
    run_media(["-y", *inputs, *mapping, "-t", duration, *filters,
               "-r", "30", *encode, "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(temporary)])
    validate_video(temporary, item["duration"], minimum=minimum)
    if item.get("background_motion") in ("premium_motion", "real_video"):
        validate_visible_motion(temporary, item["duration"])
    os.replace(temporary, target)
    atomic_json(folder / "metadata.json", {"title": item["title"], "description": description(item), "rights": item["rights"], "item": item})
    return target


def validate_video(path, expected, minimum=0):
    import imageio_ffmpeg
    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        metadata = next(reader)
    finally:
        reader.close()
    if tuple(metadata["size"]) != (1080, 1920):
        raise ValueError("Rendered video must be 1080 by 1920")
    if not math.isfinite(metadata['duration']) or metadata['duration'] <= 0 or metadata['duration'] > 60.1:
        raise ValueError('Rendered video has an invalid duration')
    if metadata["duration"] < minimum:
        raise ValueError("Rendered video is below the minimum video duration")
    if abs(metadata["duration"] - expected) > 0.35:
        raise ValueError("Source is too short for the requested segment; choose complete verse boundaries")
    run_media(["-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])


def validate_visible_motion(path, duration):
    """Fail closed when an intended video background is effectively static."""
    from PIL import Image, ImageChops, ImageStat
    first = Path(path).with_name("motion-check-first.png")
    second = Path(path).with_name("motion-check-second.png")
    later = min(2.5, max(1.0, float(duration) * 0.55))
    try:
        run_media(["-y", "-ss", "0.4", "-i", str(path), "-frames:v", "1", str(first)])
        run_media(["-y", "-ss", str(later), "-i", str(path), "-frames:v", "1", str(second)])
        with Image.open(first) as a, Image.open(second) as b:
            difference = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
            mean = sum(ImageStat.Stat(difference).mean) / 3
        if mean < 2.0:
            raise ValueError("Rendered background is effectively static")
    finally:
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)


def youtube_service(state, interactive=False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    token = state / "youtube_token.json"
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not interactive:
            raise RuntimeError("YouTube login required: run bot.py auth locally first")
        flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / "client_secrets.json"), SCOPES)
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    atomic_json(token, json.loads(creds.to_json()))
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(youtube, item, path, privacy):
    from googleapiclient.http import MediaFileUpload
    body = {"snippet": {"title": item["title"], "description": description(item), "categoryId": "27", "tags": dynamic_tags(item)},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": item["made_for_kids"]}}
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    try:
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            # Google's client retries transient failures on this resumable request.
            progress, response = request.next_chunk(num_retries=5)
            if progress:
                LOG.info("Upload %d%%", progress.progress() * 100)
        if not response.get("id"):
            raise RuntimeError("YouTube did not return a video id")
        return response["id"]
    finally:
        media.stream().close()


def run_queue(args):
    queue = Path(args.queue).resolve()
    items = load_queue(queue)
    state = Path(args.state).resolve()
    with process_lock(state / "run.lock"):
        ledger = Ledger(state / "jobs.sqlite3")
        try:
            if args.command == "resolve":
                item = next((i for i in items if i["id"] == args.id), None)
                if not item or not ledger.row(args.id) or ledger.row(args.id)[1] != "uploading":
                    raise ValueError("Only an uncertain uploading item can be resolved")
                old = ledger.row(args.id)
                if old[0] != fingerprint(item):
                    raise ValueError("Restore the original queue item before resolving")
                ledger.record(item, "uploaded" if args.video_id else "rendered", args.video_id, old[3])
                return
            count = 0
            for item in items:
                previous = ledger.row(item["id"])
                if previous and previous[0] != fingerprint(item):
                    raise ValueError(f"{item['id']}: item changed; restore it or use a new id")
                if previous and previous[1] == "uploaded":
                    continue
                if previous and previous[1] == "uploading":
                    raise RuntimeError(f"{item['id']}: previous upload uncertain. Check YouTube Studio, then use resolve; no automatic duplicate retry")
                folder = state / "renders" / item["id"]
                target = folder / "video.mp4"
                if not (previous and previous[1] == "rendered" and target.is_file() and file_hash(target) == previous[3]):
                    target = render(item, queue.parent, folder)
                    ledger.record(item, "rendered", output_hash=file_hash(target))
                LOG.info("Preview ready: %s", target)
                if args.command == "run":
                    youtube = youtube_service(state)
                    digest = file_hash(target)
                    ledger.record(item, "uploading", output_hash=digest)
                    video_id = upload(youtube, item, target, args.privacy)
                    ledger.record(item, "uploaded", video_id, digest)
                    LOG.info("Uploaded: https://www.youtube.com/watch?v=%s", video_id)
                count += 1
                if count >= args.limit:
                    break
            if count == 0:
                LOG.info("Queue has no pending items")
        finally:
            ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "auth", "preview", "run", "status", "resolve", "submit"])
    parser.add_argument("--queue", default=str(ROOT / "queue.json"))
    parser.add_argument("--state", default=str(ROOT / "state"))
    parser.add_argument("--source")
    parser.add_argument("--theme")
    parser.add_argument("--title")
    parser.add_argument("--attribution", default="")
    parser.add_argument("--rights", default="")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    parser.add_argument("--id")
    resolution = parser.add_mutually_exclusive_group()
    resolution.add_argument("--video-id")
    resolution.add_argument("--confirmed-not-uploaded", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        state = Path(args.state)
        state.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(state / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
        if args.limit < 1:
            raise ValueError("limit must be positive")
        if args.command == "doctor":
            LOG.info("Python: %s", sys.version.split()[0])
            run_media(["-version"])
            for module in ('googleapiclient', 'google_auth_oauthlib', 'yt_dlp', 'arabic_text'):
                importlib.import_module(module)
            LOG.info("Dependencies and FFmpeg OK")
            LOG.info("Queue: %d valid items", len(load_queue(args.queue)))
            LOG.info("YouTube login: %s", "present (not verified online)" if (Path(args.state) / "youtube_token.json").exists() else "required before upload")
        elif args.command == "auth":
            with process_lock(Path(args.state) / "run.lock"):
                youtube_service(Path(args.state), interactive=True)
            LOG.info("YouTube login saved locally")
        elif args.command == "submit":
            if not args.source or not args.theme or not args.title:
                raise ValueError("submit needs --source, --theme and --title")
            submit_video(args.source, args.theme, args.queue, args.title,
                         args.attribution, args.rights, base=ROOT, state=Path(args.state))
        elif args.command == "status":
            ledger = Ledger(Path(args.state) / "jobs.sqlite3")
            try:
                for row in ledger.db.execute("SELECT id,status,video_id FROM jobs ORDER BY id"):
                    print(*row, sep=" | ")
            finally:
                ledger.close()
        else:
            if args.command == "resolve" and (not args.id or not (args.video_id or args.confirmed_not_uploaded)):
                raise ValueError("resolve needs --id and either --video-id or --confirmed-not-uploaded")
            run_queue(args)
        return 0
    except Exception as exc:
        # Avoid writing OAuth response bodies, cookies or download URLs to logs.
        LOG.error("%s", str(exc) if isinstance(exc, (ValueError, FileNotFoundError, RuntimeError)) else type(exc).__name__ + ": operation failed; check credentials/network and run status")
        return 1
    finally:
        if 'handler' in locals():
            LOG.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    sys.exit(main())
