import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, HttpUrl
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("social-video-downloader")

APP_DIR = Path(__file__).parent
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "social-video-downloader"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
# The free Render instance has only 512 MiB RAM; never run two FFmpeg jobs at once.
DOWNLOAD_LOCK = threading.Lock()

ALLOWED_HOSTS = {
    "tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch",
}
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "3600"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "1024"))

app = FastAPI(title="Social Video Downloader", version="1.1.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


class ResolveRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    quality: str


def validate_url(raw: str) -> str:
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in ALLOWED_HOSTS:
        raise HTTPException(400, "目前只支持 TikTok、Instagram 和 Facebook 的公开链接。")
    if parsed.username or parsed.password:
        raise HTTPException(400, "链接格式无效。")
    return raw


@contextmanager
def temporary_instagram_cookies(directory: Path):
    """Write an optional Netscape-format cookie secret to a private temporary file."""
    raw = os.getenv("INSTAGRAM_COOKIES", "").strip()
    if not raw:
        yield None
        return
    cookie_rows = [
        line for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not any(line.count("\t") >= 6 for line in cookie_rows):
        raise RuntimeError("INSTAGRAM_COOKIES must contain Netscape-format cookies.")
    fd, cookie_path = tempfile.mkstemp(prefix=".instagram-cookies-", suffix=".txt", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as cookie_file:
            cookie_file.write(raw + "\n")
        yield cookie_path
    finally:
        try:
            os.unlink(cookie_path)
        except FileNotFoundError:
            pass


def ydl_options(cookiefile=None):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "nocheckcertificate": False,
        "cachedir": False,
    }
    if cookiefile:
        options["cookiefile"] = cookiefile
    return options


def platform_name(url: str) -> str:
    host = urlparse(url).hostname or ""
    if "tiktok" in host:
        return "TikTok"
    if "instagram" in host:
        return "Instagram"
    return "Facebook"


def get_video_heights(info: dict) -> list[int]:
    heights = {
        int(f["height"])
        for f in info.get("formats", [])
        if f.get("height") and f.get("vcodec") not in (None, "none")
    }
    return sorted(heights, reverse=True)


def format_metadata(info: dict) -> dict:
    formats = [f for f in info.get("formats", []) if f.get("vcodec") not in (None, "none")]
    formats.sort(
        key=lambda f: (
            bool(f.get("dynamic_range") and str(f.get("dynamic_range")).upper() != "SDR"),
            f.get("height") or 0,
            f.get("fps") or 0,
        ),
        reverse=True,
    )
    best = formats[0] if formats else {}
    hdr_values = sorted({str(f.get("dynamic_range")) for f in formats if f.get("dynamic_range") and str(f.get("dynamic_range")).upper() != "SDR"})
    codecs = sorted({str(f.get("vcodec")).split(".")[0] for f in formats if f.get("vcodec") not in (None, "none")})
    return {
        "max_resolution": f"{best.get('width')}×{best.get('height')}" if best.get("width") and best.get("height") else (f"最高 {best.get('height')}P" if best.get("height") else "平台未提供分辨率信息"),
        "max_fps": best.get("fps"),
        "video_codecs": codecs,
        "hdr": hdr_values,
    }


@app.get("/health")
def health():
    return {"ok": True, "version": app.version}


@app.get("/")
def home():
    return FileResponse(APP_DIR / "static" / "index.html")


@app.post("/api/resolve")
def resolve(req: ResolveRequest):
    url = validate_url(str(req.url))
    try:
        with temporary_instagram_cookies(DOWNLOAD_ROOT) as cookiefile:
            with yt_dlp.YoutubeDL(ydl_options(cookiefile if platform_name(url) == "Instagram" else None)) as ydl:
                info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("没有取得视频信息")
        if platform_name(url) == "Instagram":
            # Diagnostic: inspect all extractor-exposed renditions without logging signed media URLs.
            available = [
                {
                    "id": item.get("format_id"),
                    "ext": item.get("ext"),
                    "height": item.get("height"),
                    "fps": item.get("fps"),
                    "vcodec": item.get("vcodec"),
                    "dynamic_range": item.get("dynamic_range"),
                    "format_note": item.get("format_note"),
                    "protocol": item.get("protocol"),
                }
                for item in info.get("formats", [])
                if item.get("vcodec") not in (None, "none")
            ]
            logger.info("Instagram format inventory video_id=%s formats=%s", info.get("id"), available)
        if info.get("is_live"):
            raise HTTPException(400, "暂不支持直播内容。")
        duration = info.get("duration")
        if duration and duration > MAX_DURATION:
            raise HTTPException(413, "视频时长超过当前服务限制。")

        heights = get_video_heights(info)
        choices = [{"quality": "best", "label": "最高可用画质（自动合并音视频）"}]
        for height in heights:
            choices.append({"quality": f"height:{height}", "label": f"{height}P（自动匹配音频）"})

        return {
            "platform": platform_name(url),
            "title": info.get("title") or "公开视频",
            "thumbnail": info.get("thumbnail"),
            "duration": duration,
            "choices": choices,
            "source": format_metadata(info),
            "note": "优先使用平台实际提供的原始视频流；若画面和声音分开提供，会尝试合并。合并时不主动压缩或提升分辨率。HDR 是否保留取决于平台源流和容器兼容性。",
        }
    except HTTPException:
        raise
    except Exception as exc:
        msg = str(exc).lower()
        if any(word in msg for word in ("private", "login", "sign in", "not available")):
            raise HTTPException(422, "该内容可能不是公开内容，或平台要求登录才能访问。")
        raise HTTPException(422, "解析失败：链接可能失效、内容不可公开访问，或平台暂时限制了服务器请求。")


@app.get("/api/download-file")
def download_file(url: HttpUrl, quality: str):
    """Native browser download path; avoids buffering the whole video in JS Blob memory."""
    return download(DownloadRequest(url=url, quality=quality))


@app.post("/api/download")
def download(req: DownloadRequest):
    url = validate_url(str(req.url))
    if req.quality != "best" and not re.fullmatch(r"height:[0-9]{1,5}", req.quality):
        raise HTTPException(400, "画质选项无效，请重新解析。")
    # Queue a second request instead of returning a JSON 429 file to mobile Safari.
    # The free instance runs one worker; wait for the active conversion to release the lock.
    if not DOWNLOAD_LOCK.acquire(timeout=1800):
        raise HTTPException(503, "服务器繁忙，等待转换超时。请返回页面后再试。")

    # Prefer H.264 + AAC for broad iPhone/iOS playback compatibility.
    # If the source only offers VP9/AV1, the post-download step transcodes it.
    is_instagram = platform_name(url) == "Instagram"
    if req.quality == "best":
        # Instagram HDR plugin formats are selected first when authenticated HDR renditions exist.
        # The second branch deliberately falls back to the best available stream, never fabricating HDR.
        selector = (
            "bv*[dynamic_range!=SDR]+ba/bv*+ba/b"
            if is_instagram else "bestvideo+bestaudio/best"
        )
    else:
        height = int(req.quality.split(":", 1)[1])
        if is_instagram:
            selector = (
                f"bv*[height<={height}][dynamic_range!=SDR]+ba/"
                f"bv*[height<={height}]+ba/b"
            )
        else:
            selector = (
                f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
                f"best[height<={height}][ext=mp4][vcodec^=avc1]/"
                f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
            )

    job_dir = DOWNLOAD_ROOT / uuid.uuid4().hex
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        with temporary_instagram_cookies(job_dir) as cookiefile:
            opts = ydl_options(cookiefile if is_instagram else None)
            opts.update({
                "format": selector,
                # Prefer HDR formats before resolution/codec when the extractor exposes dynamic_range.
                "format_sort": ["hdr", "res", "fps", "vcodec"],
                "format_sort_force": True,
                "outtmpl": str(job_dir / "%(title).80s-%(id)s.%(ext)s"),
                "max_filesize": MAX_FILE_MB * 1024 * 1024,
                "overwrites": True,
            })
            logger.info(
                "download job %s: starting yt-dlp instagram_cookie_auth=%s",
                job_dir.name,
                bool(cookiefile) if is_instagram else False,
            )
            with yt_dlp.YoutubeDL(opts) as downloader:
                info = downloader.extract_info(url, download=True)
        logger.info("download job %s: yt-dlp completed selected_format=%s dynamic_range=%s requested_formats=%s", job_dir.name, info.get("format_id"), info.get("dynamic_range"), [(f.get("format_id"), f.get("dynamic_range"), f.get("vcodec"), f.get("height")) for f in info.get("requested_formats", [])])
        if not info or info.get("is_live"):
            raise HTTPException(422, "无法下载该内容。")
        candidates = [p for p in job_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not candidates:
            raise HTTPException(422, "没有生成可下载文件。")
        path = max(candidates, key=lambda p: p.stat().st_size)
        logger.info("download job %s: media file=%s size=%s", job_dir.name, path.name, path.stat().st_size)
        if path.stat().st_size > MAX_FILE_MB * 1024 * 1024:
            raise HTTPException(413, "文件超过当前服务大小限制。")

        # iOS Photos/Safari do not reliably play VP9-in-MP4. Inspect the actual
        # downloaded stream rather than trusting the filename/container.
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        streams = json.loads(probe.stdout).get("streams", [])
        video_stream = next((st for st in streams if st.get("codec_type") == "video"), None)
        if not video_stream:
            raise HTTPException(422, "下载文件中没有可识别的视频画面轨道。")

        video_codec = (video_stream.get("codec_name") or "").lower()
        transfer = (video_stream.get("color_transfer") or "").lower()
        is_hdr = transfer in ("smpte2084", "arib-std-b67")
        logger.info("download job %s: ffprobe codec=%s transfer=%s hdr=%s", job_dir.name, video_codec, transfer, is_hdr)
        audio_stream = next((st for st in streams if st.get("codec_type") == "audio"), None)
        audio_codec = (audio_stream.get("codec_name") or "").lower() if audio_stream else ""
        logger.info("download job %s: audio codec=%s", job_dir.name, audio_codec)

        # iPhone Photos/Safari need broadly compatible MP4 video/audio.
        # Limit decoder AND encoder threads: decoder thread limits must be set
        # before -i, otherwise VP9 decoding can still allocate excessive memory.
        # Preserve native HDR HEVC + AAC streams as-is. Re-encoding them wastes RAM
        # and can strip HDR metadata; this is already a Photos-friendly iPhone format.
        native_hdr_hevc = is_hdr and video_codec in ("hevc", "h265") and audio_codec in ("aac", "")
        needs_compat = (video_codec != "h264" or audio_codec not in ("aac", "")) and not native_hdr_hevc
        if needs_compat:
            fixed_path = job_dir / f"{path.stem}-ios.mp4"
            if is_hdr:
                video_encoder = "libx265"
                pixel_format = "yuv420p10le"
                extra_video_args = ["-tag:v", "hvc1", "-x265-params", "pools=1:frame-threads=1"]
            else:
                video_encoder = "libx264"
                pixel_format = "yuv420p"
                extra_video_args = ["-profile:v", "high", "-level:v", "4.2"]
            command = [
                "ffmpeg", "-y", "-threads", "1", "-filter_threads", "1",
                "-i", str(path),
                "-map", "0:v:0", "-map", "0:a?",
                "-c:v", video_encoder, "-preset", "ultrafast",
                "-crf", "18", "-pix_fmt", pixel_format, "-threads:v", "1",
                *extra_video_args,
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(fixed_path),
            ]
            logger.info("download job %s: starting low-memory %s compatibility transcode", job_dir.name, video_encoder)
            try:
                subprocess.run(command, capture_output=True, text=True, timeout=1800, check=True)
            except subprocess.CalledProcessError as exc:
                logger.error("download job %s: ffmpeg failed rc=%s stderr=%s", job_dir.name, exc.returncode, (exc.stderr or "")[-4000:])
                raise HTTPException(422, "视频兼容转换失败。请稍后重试；原始视频编码暂不适合 iPhone 照片播放。")
            if not fixed_path.exists() or fixed_path.stat().st_size == 0:
                raise HTTPException(422, "视频兼容转换没有生成有效文件。")
            path = fixed_path
            logger.info("download job %s: compatibility transcode completed size=%s", job_dir.name, path.stat().st_size)

        response = FileResponse(
            path,
            filename=path.name,
            media_type="video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream",
            background=BackgroundTask(shutil.rmtree, job_dir, ignore_errors=True),
        )
        DOWNLOAD_LOCK.release()
        return response
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        DOWNLOAD_LOCK.release()
        raise
    except Exception:
        logger.exception("download job %s: unhandled download pipeline failure", job_dir.name)
        shutil.rmtree(job_dir, ignore_errors=True)
        DOWNLOAD_LOCK.release()
        raise HTTPException(422, "下载失败：服务器处理媒体文件时发生错误，详细原因已记录。")