"""لقطة | Arabic high-quality video downloader.

Only use with content you own or have permission to download.
Keep this service private unless you add authentication, rate limiting at the edge,
and outbound network isolation. Media is stored temporarily on the server.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(BASE_DIR / "downloads"))).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Only accept video-service links. This is a partial SSRF safeguard, not a substitute
# for container/network isolation when exposing the server to strangers.
DOMAINS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "tiktok.com",
    "instagram.com", "facebook.com", "fb.watch", "fb.com", "x.com",
    "twitter.com", "vimeo.com", "dailymotion.com", "dai.ly", "pinterest.com",
    "pin.it", "reddit.com", "redd.it", "twitch.tv", "clips.twitch.tv",
    "snapchat.com", "soundcloud.com", "streamable.com", "rumble.com",
    "bilibili.com", "b23.tv", "linkedin.com", "threads.net", "bsky.app",
    "vk.com", "ok.ru", "kwai.com", "likee.video", "triller.co",
)
ALLOWED_QUALITIES = {"best", "144", "240", "360", "480", "720", "1080", "1440", "2160", "4320"}
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE_MB", "1024")) * 1024 * 1024
MAX_DURATION = int(os.environ.get("MAX_DURATION_MINUTES", "120")) * 60
TTL_SECONDS = 2 * 60 * 60
MAX_JOBS = 8

app = FastAPI(title="لقطة | تنزيل الفيديوهات", version="1.0.0", docs_url=None, redoc_url=None)
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="laqta")
_lock = threading.Lock()
_jobs: dict[str, dict] = {}


class LinkRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2000)


class DownloadRequest(LinkRequest):
    quality: str = "best"
    format: str = "mp4"


def verified_url(raw: str) -> str:
    url = raw.strip()
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise HTTPException(400, "الرابط غير صالح.")
    if (
        parsed.scheme not in ("https", "http")
        or not hostname
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in url
        or not any(hostname == domain or hostname.endswith("." + domain) for domain in DOMAINS)
    ):
        raise HTTPException(400, "الرجاء إدخال رابط مباشر من منصة مدعومة.")
    return url


def base_ydl_options() -> dict:
    def check_duration(info, *, incomplete=False):
        duration = info.get("duration")
        if duration and duration > MAX_DURATION:
            return f"Video exceeds {MAX_DURATION // 60} minutes"
        if info.get("_type") in {"playlist", "multi_video"}:
            return "Playlists are not supported"
        return None

    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "match_filter": check_duration,
        "max_filesize": MAX_FILE_SIZE,
        "ignoreerrors": False,
        "cachedir": False,
        "nocheckcertificate": False,
    }


def get_info(url: str) -> dict:
    with YoutubeDL({**base_ydl_options(), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get("_type") in {"playlist", "multi_video"}:
        raise ValueError("تعذّر قراءة الفيديو الفردي؛ روابط قوائم التشغيل غير مدعومة.")
    duration = info.get("duration")
    if duration and duration > MAX_DURATION:
        raise ValueError("الفيديو أطول من الحد المسموح به.")
    resolutions = set()
    for item in info.get("formats", []):
        if item.get("vcodec") == "none":
            continue
        height = item.get("height")
        if isinstance(height, int) and height > 0:
            # Display exact resolutions reported by the service.
            resolutions.add(height)
    if not resolutions and isinstance(info.get("height"), int):
        resolutions.add(info["height"])
    thumbnail = info.get("thumbnail") or ""
    if not isinstance(thumbnail, str) or not thumbnail.startswith("https://"):
        thumbnail = ""
    return {
        "title": str(info.get("title") or "فيديو بدون عنوان")[:200],
        "uploader": str(info.get("uploader") or info.get("channel") or "")[:100],
        "duration": int(duration) if duration else None,
        "thumbnail": thumbnail,
        "qualities": sorted(resolutions, reverse=True),
        "platform": str(info.get("extractor_key") or info.get("extractor") or "Video")[:60],
    }


def quality_format(quality: str) -> str:
    # A high-resolution video can have separate audio and video streams.
    # FFmpeg merges them. Fallback b handles platforms with a single stream.
    if quality == "best":
        return "bv*+ba/b"
    maximum = int(quality)
    return f"bv*[height<={maximum}]+ba/b[height<={maximum}]"


def _update(job_id: str, **changes) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(changes)


def _progress(job_id: str, status: dict) -> None:
    state = status.get("status")
    if state == "finished":
        _update(job_id, status="processing", progress=98)
        return
    if state == "downloading":
        received = status.get("downloaded_bytes") or 0
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        if total > 0:
            progress = round(min(97, max(1, received * 97 / total)), 1)
            _update(job_id, status="downloading", progress=progress)
        else:
            _update(job_id, status="downloading")


def _run_download(job_id: str, url: str, quality: str, output_format: str) -> None:
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    opts = base_ydl_options()
    opts.update({
        "outtmpl": str(job_dir / "video.%(ext)s"),
        "progress_hooks": [lambda progress: _progress(job_id, progress)],
        "postprocessor_hooks": [lambda hook: _update(job_id, status="processing", progress=98) if hook.get("status") == "started" else None],
        "max_downloads": 1,
        "overwrites": True,
        "windowsfilenames": True,
    })
    if output_format == "mp3":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        })
    else:
        opts.update({"format": quality_format(quality), "merge_output_format": "mp4"})
    try:
        _update(job_id, status="downloading")
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
        if output_format == "mp3":
            priority = (".mp3",)
        else:
            priority = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
        files = [f for f in job_dir.iterdir() if f.is_file() and f.suffix.lower() in priority]
        files.sort(key=lambda f: (priority.index(f.suffix.lower()), -f.stat().st_size))
        if not files:
            raise RuntimeError("لم يتم العثور على ملف الفيديو النهائي.")
        file = files[0]
        if file.stat().st_size > MAX_FILE_SIZE:
            raise RuntimeError("حجم الفيديو يتجاوز الحد المسموح به.")
        _update(job_id, status="ready", progress=100, filename=file.name, size=file.stat().st_size)
    except Exception as exc:
        # Avoid sending raw extractor/stream URLs and environment details to the browser.
        error = "تعذّر تنزيل الفيديو. قد يكون خاصًا أو محميًا أو أن المنصة لا تسمح بتنزيله حاليًا."
        if "exceeds" in str(exc).lower() or "larger than" in str(exc).lower():
            error = "حجم الفيديو أو مدته يتجاوز الحد المسموح به."
        _update(job_id, status="error", error=error, progress=0)
        shutil.rmtree(job_dir, ignore_errors=True)
        print(f"[download {job_id}] {type(exc).__name__}: {str(exc)[:400]}", flush=True)


def _cleanup() -> None:
    now = time.monotonic()
    stale = []
    with _lock:
        for job_id, job in list(_jobs.items()):
            if job["status"] not in ("queued", "downloading", "processing") and now - job["created"] > TTL_SECONDS:
                stale.append(job_id)
                del _jobs[job_id]
    for job_id in stale:
        shutil.rmtree(DOWNLOAD_DIR / job_id, ignore_errors=True)


@app.get("/api/health")
def health():
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None, "deno": shutil.which("deno") is not None}


@app.post("/api/info")
async def analyze(link: LinkRequest):
    url = verified_url(link.url)
    try:
        return await asyncio.wait_for(asyncio.to_thread(get_info, url), timeout=75)
    except asyncio.TimeoutError:
        raise HTTPException(504, "استغرق فحص الرابط وقتًا طويلًا، حاول مجددًا.")
    except (DownloadError, ValueError, KeyError):
        raise HTTPException(422, "تعذّر قراءة هذا الرابط. تأكد من أن الفيديو عام ومنصة الفيديو مدعومة.")
    except Exception as exc:
        print(f"[info] {type(exc).__name__}: {str(exc)[:400]}", flush=True)
        raise HTTPException(422, "تعذّر تحليل رابط الفيديو.")


@app.post("/api/download", status_code=202)
def begin(payload: DownloadRequest):
    url = verified_url(payload.url)
    if payload.quality not in ALLOWED_QUALITIES:
        raise HTTPException(400, "جودة غير مدعومة.")
    if payload.format not in {"mp4", "mp3"}:
        raise HTTPException(400, "صيغة غير مدعومة.")
    _cleanup()
    with _lock:
        waiting = sum(job["status"] in ("queued", "downloading", "processing") for job in _jobs.values())
        if waiting >= MAX_JOBS:
            raise HTTPException(429, "هناك تنزيلات كثيرة حاليًا؛ أعد المحاولة لاحقًا.")
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {
            "status": "queued", "progress": 0, "created": time.monotonic(), "format": payload.format,
            "filename": None, "error": None, "size": None,
        }
    executor.submit(_run_download, job_id, url, payload.quality, payload.format)
    return {"id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    _cleanup()
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(404, "طلب التنزيل غير موجود.")
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "طلب التنزيل غير موجود أو انتهت صلاحيته.")
        result = {key: job[key] for key in ("status", "progress", "error", "size", "format")}
    if result["status"] == "ready":
        result["file_url"] = f"/api/jobs/{job_id}/file"
    return result


@app.get("/api/jobs/{job_id}/file")
def get_file(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(404)
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] != "ready" or not job.get("filename"):
            raise HTTPException(404, "الملف غير موجود أو انتهت صلاحيته.")
        filename = job["filename"]
        kind = job["format"]
    # All filenames are created by this app, never taken from a URL/request.
    file = DOWNLOAD_DIR / job_id / filename
    if not file.is_file() or file.parent.resolve() != (DOWNLOAD_DIR / job_id).resolve():
        raise HTTPException(404, "الملف غير موجود.")
    media_type = "audio/mpeg" if kind == "mp3" else "application/octet-stream"
    return FileResponse(file, media_type=media_type, filename=f"Laqtah-{job_id[:8]}{file.suffix}", headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
