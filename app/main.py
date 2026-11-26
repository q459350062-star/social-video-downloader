import json
import importlib.metadata
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from .mp4 import ensure_mp4_container
from .checksums import calculate_file_hashes
from .media_limits import validate_enhancement_limits
from .runtime import (
    DOWNLOAD_ROOT, DOWNLOAD_LOCK, cleanup_job_dir, cleanup_stale_jobs, create_job_dir,
)

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request as FastAPIRequest
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, HttpUrl
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("social-video-downloader")

APP_DIR = Path(__file__).parent

ALLOWED_HOSTS = {
    "tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch",
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
}
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "3600"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "1024"))

app = FastAPI(title="Social Video Downloader", version="1.5.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


@app.exception_handler(HTTPException)
async def log_http_exception(request: FastAPIRequest, exc: HTTPException):
    if exc.status_code == 413:
        logger.warning(
            "HTTP_413_DETAIL path=%s content_length=%s detail=%s",
            request.url.path,
            request.headers.get("content-length", "unknown"),
            exc.detail,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


@app.middleware("http")
async def log_editor_upload_413(request: FastAPIRequest, call_next):
    if request.url.path not in {"/api/editor/export", "/api/editor/metadata"}:
        return await call_next(request)
    content_length = request.headers.get("content-length", "unknown")
    response = await call_next(request)
    if response.status_code == 413:
        logger.warning(
            "EDITOR_UPLOAD_413 path=%s content_length=%s",
            request.url.path,
            content_length,
        )
    return response


def _youtube_startup_smoke_test():
    """Deprecated: network download tests must not run during web-service startup.

    YouTube/Instagram/Facebook/TikTok checks are performed explicitly after deployment.
    Running multiple full media downloads on every boot consumed the 512 MiB instance's
    memory and bandwidth and could cause OOM restarts.
    """
    logger.info("Remote media smoke tests disabled during startup; use post-deploy tests.")


@app.on_event("startup")
def remove_abandoned_job_files():
    """Clean temporary workspaces and test YouTube extraction after a deploy."""
    cleanup_stale_jobs()
    _youtube_startup_smoke_test()


class ResolveRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    quality: str
    output_mode: str = "ios"
    media_type: str = "video"


def validate_url(raw: str) -> str:
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in ALLOWED_HOSTS:
        raise HTTPException(400, "目前只支持 TikTok、Instagram、Facebook 和 YouTube 的公开链接。")
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
        # Keep yt-dlp's maintained default YouTube clients. Hard-pinning mweb/tv/web_safari
        # became counterproductive after the 2026.11.23 client changes and can trigger
        # LOGIN_REQUIRED before the PO-token provider is reached. BgUtils HTTP remains
        # available for clients that require GVS tokens; trace token requests in diagnostics.
        "extractor_args": {
            # Explicitly request PO tokens for clients that require them. Without
            # fetch_pot=always, recent YouTube client changes can fail with LOGIN_REQUIRED
            # before the configured bgutil HTTP provider is asked to mint a token.
            "youtube": {
                "pot_trace": ["true"],
                "fetch_pot": ["always"],
            },
            "youtubepot-bgutilhttp": {
                "base_url": "http://127.0.0.1:4416"
            },
        },
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
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}:
        return "YouTube"
    return "Facebook"



def cobalt_is_configured() -> bool:
    return bool(os.getenv("COBALT_API_URL", "").strip() and os.getenv("COBALT_API_KEY", "").strip())


def cobalt_api_request(url: str, output_mode: str = "ios", quality: str = "best") -> dict:
    base_url = os.getenv("COBALT_API_URL", "").strip().rstrip("/")
    api_key = os.getenv("COBALT_API_KEY", "").strip()
    if not base_url or not api_key:
        raise RuntimeError("Self-hosted Cobalt is not configured.")
    key_format_ok = bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", api_key))
    logger.info(
        "COBALT_AUTH_DIAG host=%s key_uuid_format=%s key_length=%s",
        urlparse(base_url).netloc, key_format_ok, len(api_key),
    )
    video_quality = "max"
    if quality.startswith("height:"):
        requested = int(quality.split(":", 1)[1])
        allowed = [4320, 2160, 1440, 1080, 720, 480, 360, 240, 144]
        video_quality = str(next((h for h in allowed if h <= requested), 144))
    payload = {
        "url": url,
        "videoQuality": video_quality,
        "downloadMode": "auto",
        "filenameStyle": "pretty",
    }
    if platform_name(url) == "YouTube":
        # Keep the requested source codec/quality, but always ask Cobalt for an MP4
        # container. The download pipeline below also remuxes any non-MP4 response.
        if output_mode == "source":
            payload.update({"youtubeVideoCodec": "av1", "youtubeVideoContainer": "mp4"})
        else:
            payload.update({"youtubeVideoCodec": "h264", "youtubeVideoContainer": "mp4"})
    request = Request(
        base_url + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Api-Key " + api_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=75) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"Cobalt API HTTP {exc.code}: {detail[:500]}") from exc
    status = result.get("status")
    if status == "error":
        error = result.get("error") or {}
        context = error.get("context") or {}
        service = context.get("service") or platform_name(url)
        raise RuntimeError(
            "Cobalt API error: " + str(error.get("code") or "unknown") + " (service=" + str(service) + ")"
        )
    if status in ("tunnel", "redirect") and result.get("url"):
        return result
    if status == "local-processing" and result.get("tunnel"):
        return result
    if status == "picker" and result.get("picker"):
        # For single-file download mode, select the first video/GIF item.
        # Gallery photos are not silently presented as a video download.
        items = result.get("picker") or []
        selected = next((item for item in items if item.get("type") in ("video", "gif") and item.get("url")), None)
        if not selected:
            raise RuntimeError("Cobalt returned a photo-only picker; no video item is available.")
        return {
            **result,
            "selected_url": selected["url"],
            "filename": result.get("filename") or ("cobalt_video.mp4" if selected.get("type") == "video" else "cobalt_media.gif"),
        }
    raise RuntimeError("Cobalt returned an unsupported response status: " + str(status))


def cobalt_download(url: str, job_dir: Path, output_mode: str, quality: str) -> dict:
    result = cobalt_api_request(url, output_mode=output_mode, quality=quality)
    base_url = os.getenv("COBALT_API_URL", "").strip().rstrip("/")
    cobalt_host = urlparse(base_url).netloc

    def fetch_to_file(media_url: str, filename_hint: str) -> Path:
        absolute_url = urljoin(base_url + "/", media_url)
        parsed_media = urlparse(absolute_url)
        headers = {"User-Agent": "SocialVideoDownloader/1.0"}
        if parsed_media.netloc == cobalt_host:
            headers["Authorization"] = "Api-Key " + os.getenv("COBALT_API_KEY", "").strip()
        request = Request(absolute_url, headers=headers)
        with urlopen(request, timeout=180) as response:
            suggested = Path(str(filename_hint or "video.mp4").replace("\\", "/")).name
            filename = re.sub(r"[^0-9A-Za-z._ -]", "_", suggested).strip(" .") or "video.mp4"
            if not Path(filename).suffix:
                content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                extension = {
                    "video/mp4": ".mp4",
                    "video/webm": ".webm",
                    "video/x-matroska": ".mkv",
                    "video/quicktime": ".mov",
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/gif": ".gif",
                }.get(content_type, ".mp4")
                filename += extension
            output_path = job_dir / filename
            total = 0
            with output_path.open("wb") as destination:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_FILE_MB * 1024 * 1024:
                        raise HTTPException(413, "文件超过当前服务大小限制。")
                    destination.write(chunk)
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("Cobalt returned an empty media file.")
        return output_path

    status = result.get("status")
    if status in ("tunnel", "redirect"):
        output_path = fetch_to_file(result["url"], result.get("filename") or "video.mp4")
    elif status == "picker":
        output_path = fetch_to_file(
            result["selected_url"],
            result.get("filename") or "cobalt_video.mp4",
        )
    elif status == "local-processing":
        tunnels = result.get("tunnel") or []
        if not tunnels:
            raise RuntimeError("Cobalt local-processing response contained no tunnels.")
        output_info = result.get("output") or {}
        output_name = output_info.get("filename") or "cobalt-output.mp4"
        downloaded_parts = [
            fetch_to_file(tunnel_url, f"cobalt-part-{index}.mp4")
            for index, tunnel_url in enumerate(tunnels)
        ]
        if len(downloaded_parts) == 1:
            output_path = job_dir / Path(output_name).name
            if output_path.exists():
                output_path.unlink()
            downloaded_parts[0].replace(output_path)
        else:
            output_path = job_dir / Path(output_name).name
            if not output_path.suffix:
                output_path = output_path.with_suffix(".mp4")
            command = [
                "ffmpeg", "-y", "-threads", "1",
                "-i", str(downloaded_parts[0]), "-i", str(downloaded_parts[1]),
                "-map", "0:v:0", "-map", "1:a:0?",
                "-c", "copy", "-movflags", "+faststart", str(output_path),
            ]
            try:
                subprocess.run(command, capture_output=True, text=True, timeout=600, check=True)
            except subprocess.CalledProcessError as exc:
                logger.error("Cobalt local merge failed stderr=%s", (exc.stderr or "")[-2500:])
                raise RuntimeError("Cobalt local-processing merge failed.") from exc
            for part in downloaded_parts:
                try:
                    part.unlink()
                except OSError:
                    pass
    else:
        raise RuntimeError("Cobalt returned unsupported download status: " + str(status))

    return {
        "id": "cobalt",
        "title": Path(output_path.name).stem,
        "format_id": "cobalt",
        "requested_formats": [],
        "webpage_url": url,
    }

def get_video_heights(info: dict) -> list[int]:
    heights = {
        int(f["height"])
        for f in info.get("formats", [])
        if f.get("height") and f.get("vcodec") not in (None, "none")
    }
    return sorted(heights, reverse=True)


def has_4k60(info: dict, sdr_only: bool = False) -> bool:
    for item in info.get("formats", []):
        if item.get("vcodec") in (None, "none"):
            continue
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        fps = float(item.get("fps") or 0)
        dynamic_range = str(item.get("dynamic_range") or "").upper()
        if max(width, height) >= 3840 and min(width, height) >= 2160 and fps >= 59:
            if not sdr_only or dynamic_range == "SDR":
                return True
    return False


def format_metadata(info: dict) -> dict:
    formats = [
        item for item in info.get("formats", [])
        if item.get("vcodec") not in (None, "none")
    ]

    def quality_rank(item: dict) -> tuple:
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        fps = float(item.get("fps") or 0)
        bitrate = float(item.get("vbr") or item.get("tbr") or 0)
        dynamic_range = str(item.get("dynamic_range") or "").upper()
        codec = str(item.get("vcodec") or "").lower()
        # Resolution and frame rate lead; HDR, measured bitrate and codec efficiency
        # only break ties. Never let a codec label alone outrank a larger rendition.
        codec_rank = (
            5 if codec.startswith("av01") else
            4 if codec.startswith("vp9") else
            3 if codec.startswith(("hevc", "h265")) else
            2 if codec.startswith("avc1") else 1
        )
        return (
            min(width, height), max(width, height), fps,
            dynamic_range not in ("", "SDR"), bitrate, codec_rank,
        )

    best = max(formats, key=quality_rank, default={})
    hdr_values = sorted({
        str(item.get("dynamic_range")) for item in formats
        if item.get("dynamic_range") and str(item.get("dynamic_range")).upper() != "SDR"
    })
    codecs = sorted({
        str(item.get("vcodec")).split(".")[0] for item in formats
        if item.get("vcodec") not in (None, "none")
    })
    known_ranges = {
        str(item.get("dynamic_range")).upper()
        for item in formats if item.get("dynamic_range")
    }
    hdr_status = (
        "HDR可用" if hdr_values else
        "仅报告SDR" if known_ranges and known_ranges <= {"SDR"} else
        "源站未提供明确HDR标记"
    )
    max_fps = max((float(item.get("fps") or 0) for item in formats), default=0) or None
    max_bitrate = max(
        (float(item.get("vbr") or item.get("tbr") or 0) for item in formats),
        default=0,
    ) or None
    width, height = int(best.get("width") or 0), int(best.get("height") or 0)
    return {
        "max_resolution": f"{width}×{height}" if width and height else (f"最高 {height}P" if height else "平台未提供分辨率信息"),
        "max_fps": max_fps,
        "max_video_bitrate_kbps": round(max_bitrate, 1) if max_bitrate else None,
        "best_video_codec": str(best.get("vcodec") or "").split(".")[0] or None,
        "best_dynamic_range": best.get("dynamic_range"),
        "video_codecs": codecs,
        "format_count": len(formats),
        "hdr": hdr_values,
        "hdr_available": bool(hdr_values),
        "hdr_status": hdr_status,
        "native_4k60_available": has_4k60(info),
        "native_4k60_sdr_available": has_4k60(info, sdr_only=True),
    }


@app.get("/health")
def health():
    return {"ok": True, "version": app.version}


@app.get("/api/diagnostics")
def diagnostics():
    """Expose non-secret runtime checks; never return cookie contents or environment values."""
    try:
        plugin_version = importlib.metadata.version("yt-dlp-instagram-hdr")
    except importlib.metadata.PackageNotFoundError:
        plugin_version = None
    memory_limit = None
    for memory_file in (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            value = memory_file.read_text().strip()
            if value != "max":
                memory_limit = int(value)
                break
        except (OSError, ValueError):
            continue
    return {
        "version": app.version,
        "yt_dlp_version": yt_dlp.version.__version__,
        "instagram_hdr_plugin_version": plugin_version,
        "instagram_cookie_auth_configured": bool(os.getenv("INSTAGRAM_COOKIES", "").strip()),
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "ffprobe_available": bool(shutil.which("ffprobe")),
        "memory_limit_bytes": memory_limit,
    }


@app.api_route("/", methods=["GET", "HEAD"])
def home():
    # Render and uptime probes periodically issue HEAD /; accept them without 405 noise.
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
        if has_4k60(info):
            choices.insert(1, {"quality": "4k60", "label": "原生 4K 60FPS（3840×2160级，源站有提供时）"})

        return {
            "platform": platform_name(url),
            "title": info.get("title") or "公开视频",
            "thumbnail": info.get("thumbnail"),
            "duration": duration,
            "choices": choices,
            "creator": info.get("uploader") or info.get("channel") or info.get("creator") or "源站未提供",
            "creator_id": info.get("uploader_id") or info.get("channel_id") or "",
            "source_url": info.get("webpage_url") or url,
            "source_post_id": info.get("id") or "",
            "source_date": info.get("upload_date") or "",
            "source": format_metadata(info),
            "note": (
                "当前解析结果未发现明确的 HDR 标记；这不等于已确认源视频是 SDR。请以下载后的媒体检测结果为准，转码也不能把 SDR 真实转换成 HDR。"
                if platform_name(url) == "Instagram" and not format_metadata(info)["hdr_available"]
                else "优先使用平台实际提供的原始视频流；若画面和声音分开提供，会尝试合并。HDR 是否保留取决于平台源流和容器兼容性。"
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        if cobalt_is_configured():
            try:
                fallback = cobalt_api_request(url, output_mode="ios", quality="best")
                filename = Path(str(fallback.get("filename") or "公开视频")).stem
                return {
                    "platform": platform_name(url),
                    "title": filename or "公开视频",
                    "thumbnail": None,
                    "duration": None,
                    "choices": [{"quality": "best", "label": "Cobalt 源站最高可用画质（实际规格以文件检测为准）"}],
                    "creator": "Cobalt 备用解析",
                    "creator_id": "",
                    "source_url": url,
                    "source_post_id": "",
                    "source_date": "",
                    "source": {
                        "max_resolution": "下载后检测实际分辨率",
                        "max_fps": None,
                        "video_codecs": [],
                        "best_video_codec": None,
                        "best_dynamic_range": None,
                        "hdr": [],
                        "hdr_available": False,
                        "hdr_status": "Cobalt 未提供可验证的 HDR 元数据",
                        "native_4k60_available": False,
                        "native_4k60_sdr_available": False,
                    },
                    "note": "已切换自建 Cobalt 备用解析。当前接口未提供可验证的分辨率、帧率或 HDR 信息；不会虚报 4K60，实际文件规格以下载后检测为准。",
                }
            except Exception:
                logger.exception("Cobalt resolve fallback failed")
        msg = str(exc).lower()
        if any(word in msg for word in ("private", "login", "sign in", "not available")):
            raise HTTPException(422, "该内容可能不是公开内容，或平台要求登录才能访问。")
        raise HTTPException(422, "解析失败：链接可能失效、内容不可公开访问，或平台暂时限制了服务器请求。")


@app.get("/api/download-file")
def download_file(url: HttpUrl, quality: str, output_mode: str = "ios", media_type: str = "video"):
    """Native browser download path; avoids buffering the whole video in JS Blob memory."""
    return download(DownloadRequest(url=url, quality=quality, output_mode=output_mode, media_type=media_type))


@app.post("/api/download")
def download(req: DownloadRequest):
    url = validate_url(str(req.url))
    if req.quality not in ("best", "4k60") and not re.fullmatch(r"height:[0-9]{1,5}", req.quality):
        raise HTTPException(400, "画质选项无效，请重新解析。")
    if req.output_mode not in ("ios", "source", "sdr"):
        raise HTTPException(400, "输出模式无效，请重新选择。")
    if req.media_type not in ("video", "audio"):
        raise HTTPException(400, "媒体类型无效，请重新选择。")
    # Queue a second request instead of returning a JSON 429 file to mobile Safari.
    # The free instance runs one worker; wait for the active conversion to release the lock.
    if not DOWNLOAD_LOCK.acquire(timeout=1800):
        raise HTTPException(503, "服务器繁忙，等待转换超时。请返回页面后再试。")

    # source = highest platform-provided streams without re-encoding.
    # sdr = highest explicitly SDR stream; convert only if iPhone compatibility requires it.
    # ios = prioritize H.264/AAC for broad iPhone playback.
    is_instagram = platform_name(url) == "Instagram"
    if req.media_type == "audio":
        selector = "bestaudio/best"
    elif req.quality == "4k60":
        four_k60_pair = (
            "bestvideo[height>=2160][width>=3840][fps>=59]+bestaudio/"
            "bestvideo[width>=2160][height>=3840][fps>=59]+bestaudio"
        )
        four_k60_combined = (
            "best[height>=2160][width>=3840][fps>=59]/"
            "best[width>=2160][height>=3840][fps>=59]"
        )
        if req.output_mode == "sdr":
            selector = (
                "bestvideo[height>=2160][width>=3840][fps>=59][dynamic_range=SDR]+bestaudio/"
                "bestvideo[width>=2160][height>=3840][fps>=59][dynamic_range=SDR]+bestaudio/"
                "best[height>=2160][width>=3840][fps>=59][dynamic_range=SDR]/"
                "best[width>=2160][height>=3840][fps>=59][dynamic_range=SDR]"
            )
        else:
            selector = f"{four_k60_pair}/{four_k60_combined}"
    elif req.output_mode == "source":
        selector = (
            "bestvideo+bestaudio/best" if req.quality == "best" else
            f"bestvideo[height<={int(req.quality.split(chr(58), 1)[1])}]+bestaudio/"
            f"best[height<={int(req.quality.split(chr(58), 1)[1])}]"
        )
    elif req.output_mode == "sdr":
        selector = (
            "bestvideo[dynamic_range=SDR]+bestaudio/best[dynamic_range=SDR]"
            if req.quality == "best" else
            f"bestvideo[height<={int(req.quality.split(chr(58), 1)[1])}][dynamic_range=SDR]+bestaudio/"
            f"best[height<={int(req.quality.split(chr(58), 1)[1])}][dynamic_range=SDR]"
        )
    elif req.quality == "best":
        # Quality-first: do not force H.264/MP4 at extraction time. That can silently
        # downgrade 4K/60fps/HDR sources when a platform offers AV1/VP9/HEVC.
        # The post-download probe decides whether a compatibility transcode is needed.
        selector = "bestvideo+bestaudio/best"
    else:
        height = int(req.quality.split(":", 1)[1])
        # Keep the requested resolution ceiling, but choose the best source codec/bitrate
        # within it instead of preferring a lower-resolution H.264 rendition.
        selector = (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        )

    job_dir = None
    try:
        job_dir = create_job_dir()
        with temporary_instagram_cookies(job_dir) as cookiefile:
            opts = ydl_options(cookiefile if is_instagram else None)
            opts.update({
                "format": selector,
                # Use yt-dlp's maintained quality ordering (resolution, fps, HDR,
                # codec and bitrate) rather than forcing an incomplete custom sort.
                "outtmpl": str(job_dir / "%(title).80s-%(id)s.%(ext)s"),
                "max_filesize": MAX_FILE_MB * 1024 * 1024,
                "overwrites": True,
                # Never deliberately emit MKV: source mode keeps the best codec but
                # uses MP4 as the output container. A post-download remux handles
                # single-stream WebM/MKV results that bypass yt-dlp's merger.
                "merge_output_format": "mp4",
            })
            logger.info(
                "download job %s: starting mode=%s instagram_cookie_auth=%s",
                job_dir.name, req.output_mode,
                bool(cookiefile) if is_instagram else False,
            )
            try:
                with yt_dlp.YoutubeDL(opts) as downloader:
                    info = downloader.extract_info(url, download=True)
            except Exception as yt_error:
                if not cobalt_is_configured() or req.media_type == "audio":
                    raise
                logger.warning(
                    "yt-dlp download failed; trying self-hosted Cobalt fallback: %s",
                    str(yt_error)[:300],
                )
                info = cobalt_download(url, job_dir, req.output_mode, req.quality)
                logger.info("download job %s: self-hosted Cobalt fallback completed", job_dir.name)
        logger.info("download job %s: yt-dlp completed selected_format=%s dynamic_range=%s requested_formats=%s", job_dir.name, info.get("format_id"), info.get("dynamic_range"), [(f.get("format_id"), f.get("dynamic_range"), f.get("vcodec"), f.get("height")) for f in info.get("requested_formats", [])])
        if not info or info.get("is_live"):
            raise HTTPException(422, "无法下载该内容。")
        candidates = [p for p in job_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not candidates:
            raise HTTPException(422, "没有生成可下载文件。")
        path = max(candidates, key=lambda p: p.stat().st_size)
        logger.info("download job %s: media file=%s size=%s", job_dir.name, path.name, path.stat().st_size)
        if req.media_type == "audio":
            audio_path = job_dir / (path.stem + ".mp3")
            try:
                subprocess.run([
                    "ffmpeg", "-y", "-threads", "1", "-i", str(path),
                    "-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "0",
                    "-id3v2_version", "3", str(audio_path),
                ], capture_output=True, text=True, timeout=600, check=True)
            except subprocess.CalledProcessError as exc:
                logger.error("MP3 extraction failed stderr=%s", (exc.stderr or "")[-2000:])
                raise HTTPException(422, "音频提取失败：该内容可能没有可提取的音轨。")
            if not audio_path.exists() or audio_path.stat().st_size == 0:
                raise HTTPException(422, "没有生成有效的 MP3 音频文件。")
            response = FileResponse(
                audio_path, filename=audio_path.name, media_type="audio/mpeg",
                background=BackgroundTask(cleanup_job_dir, job_dir),
            )
            DOWNLOAD_LOCK.release()
            return response
        try:
            path = ensure_mp4_container(path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
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
        actual_width = int(video_stream.get("width") or 0)
        actual_height = int(video_stream.get("height") or 0)

        def parse_frame_rate(value):
            try:
                numerator, denominator = str(value).split("/", 1)
                denominator = float(denominator)
                return float(numerator) / denominator if denominator else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                try:
                    return float(value or 0)
                except (TypeError, ValueError):
                    return 0.0

        actual_fps = parse_frame_rate(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        )
        logger.info(
            "download job %s: ffprobe resolution=%sx%s fps=%.3f codec=%s transfer=%s hdr=%s",
            job_dir.name, actual_width, actual_height, actual_fps, video_codec, transfer, is_hdr,
        )
        if req.quality == "4k60" and (
            min(actual_width, actual_height) < 2160
            or max(actual_width, actual_height) < 3840
            or actual_fps < 59
        ):
            raise HTTPException(
                422,
                "源站实际返回的文件未达到原生 4K 60FPS，已拦截低规格结果；请重新解析或选择最高可用画质。",
            )
        audio_stream = next((st for st in streams if st.get("codec_type") == "audio"), None)
        audio_codec = (audio_stream.get("codec_name") or "").lower() if audio_stream else ""
        logger.info("download job %s: audio codec=%s", job_dir.name, audio_codec)

        # iPhone Photos/Safari need broadly compatible MP4 video/audio.
        # Limit decoder AND encoder threads: decoder thread limits must be set
        # before -i, otherwise VP9 decoding can still allocate excessive memory.
        # Preserve native HDR HEVC + AAC streams as-is. Re-encoding them wastes RAM
        # and can strip HDR metadata; this is already a Photos-friendly iPhone format.
        native_hdr_hevc = is_hdr and video_codec in ("hevc", "h265") and audio_codec in ("aac", "")
        needs_compat = req.output_mode != "source" and (video_codec != "h264" or audio_codec not in ("aac", "")) and not native_hdr_hevc
        # A "source" download may still be unplayable on iPhone (for example VP9 in MP4).
        # Preserve the original streams only when they are already broadly iOS-compatible;
        # otherwise transcode so the downloaded file contains visible video as well as audio.
        if needs_compat:
            logger.info(
                "download job %s: source mode=%s requires iPhone compatibility conversion codec=%s audio=%s",
                job_dir.name, req.output_mode, video_codec, audio_codec,
            )
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
            media_type=("video/mp4" if path.suffix.lower() == ".mp4" else
                        "video/webm" if path.suffix.lower() == ".webm" else
                        "video/quicktime" if path.suffix.lower() == ".mov" else
                        "video/x-matroska" if path.suffix.lower() == ".mkv" else
                        "application/octet-stream"),
            background=BackgroundTask(cleanup_job_dir, job_dir),
        )
        DOWNLOAD_LOCK.release()
        return response
    except HTTPException:
        if job_dir is not None:
            cleanup_job_dir(job_dir)
        DOWNLOAD_LOCK.release()
        raise
    except Exception:
        logger.exception("download job %s: unhandled download pipeline failure", job_dir.name if job_dir else "unallocated")
        if job_dir is not None:
            cleanup_job_dir(job_dir)
        DOWNLOAD_LOCK.release()
        raise HTTPException(422, "下载失败：服务器处理媒体文件时发生错误，详细原因已记录。")


@app.post("/api/editor/checksum")
def editor_checksum(video: UploadFile = File(...)):
    """Calculate integrity hashes for an uploaded file; never modify or re-encode it."""
    filename = Path(video.filename or "video").name
    try:
        result = calculate_file_hashes(video.file)
        return {
            "filename": filename,
            **result,
            "note": "仅计算文件完整性哈希；未修改文件内容、视频画面、音频或 HDR。",
        }
    except ValueError as exc:
        if "大小限制" in str(exc):
            raise HTTPException(413, "单个文件最大支持 300 MB。")
        raise HTTPException(400, "请选择有效的视频文件。")
    finally:
        video.file.close()


@app.post("/api/editor/metadata")
def editor_metadata(
    video: UploadFile = File(...),
    personal_name: str = Form(...),
    personal_role: str = Form("剪辑者/解说者"),
    source_creator: str = Form(""),
    source_url: str = Form(""),
    original_rightsholder: str = Form(""),
):
    """Write truthful attribution metadata without changing the underlying video streams."""
    if not personal_name.strip() or len(personal_name.strip()) > 120:
        raise HTTPException(400, "请填写有效的个人名称（最多 120 个字符）。")
    if len(personal_role) > 80 or len(source_creator) > 200 or len(source_url) > 2048 or len(original_rightsholder) > 200:
        raise HTTPException(400, "版权信息字段过长。")
    if source_url.strip():
        parsed = urlparse(source_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(400, "来源链接格式无效。")
    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v"}:
        raise HTTPException(400, "元数据写入目前支持 MP4、MOV、M4V。")
    if not DOWNLOAD_LOCK.acquire(timeout=1):
        raise HTTPException(503, "服务器正在处理另一个视频任务，请稍后再试。")
    job_dir = None
    try:
        job_dir = create_job_dir("metadata-")
        input_path = job_dir / ("source" + suffix)
        size = 0
        with input_path.open("wb") as dst:
            while True:
                chunk = video.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 300 * 1024 * 1024:
                    raise HTTPException(413, "单个视频最大支持 300 MB。")
                dst.write(chunk)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(input_path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        streams = json.loads(probe.stdout).get("streams", [])
        if not any(item.get("codec_type") == "video" for item in streams):
            raise HTTPException(422, "文件中没有可识别的视频轨道。")
        output = job_dir / "VideoDrop-Metadata.mp4"
        attribution = [
            f"剪辑/解说：{personal_name.strip()}（{personal_role.strip() or '剪辑者/解说者'}）",
            f"原作者：{source_creator.strip() or '未填写'}",
            f"原版权人：{original_rightsholder.strip() or '未填写'}",
            f"来源：{source_url.strip() or '未填写'}",
            "说明：此元数据记录创作参与和来源，不代表版权转让或原作者授权。",
        ]
        command = [
            "ffmpeg", "-y", "-i", str(input_path), "-map", "0", "-map_metadata", "0",
            "-c", "copy",
            "-metadata", f"comment={'；'.join(attribution)}",
            "-metadata", f"description={'；'.join(attribution)}",
            "-movflags", "+faststart", "-f", "mp4", str(output),
        ]
        if original_rightsholder.strip():
            pos = command.index("-movflags")
            command[pos:pos] = ["-metadata", f"copyright={original_rightsholder.strip()}"]
        subprocess.run(command, capture_output=True, text=True, timeout=1800, check=True)
        if not output.exists() or output.stat().st_size == 0:
            raise HTTPException(422, "元数据写入没有生成有效文件。")
        return FileResponse(
            output, filename="VideoDrop-Metadata.mp4", media_type="video/mp4",
            background=BackgroundTask(cleanup_job_dir, job_dir),
        )
    except subprocess.CalledProcessError as exc:
        logger.error("metadata job %s: ffmpeg failed stderr=%s", job_dir.name, (exc.stderr or "")[-3000:])
        if job_dir is not None:
            cleanup_job_dir(job_dir)
        raise HTTPException(422, "元数据写入失败；原始编码或容器可能不兼容。原文件未被修改。")
    except HTTPException:
        if job_dir is not None:
            cleanup_job_dir(job_dir)
        raise
    except Exception:
        logger.exception("metadata job %s: unexpected failure", job_dir.name if job_dir else "unallocated")
        if job_dir is not None:
            cleanup_job_dir(job_dir)
        raise HTTPException(422, "元数据处理失败，原文件未被修改。")
    finally:
        DOWNLOAD_LOCK.release()


@app.post("/api/editor/export")
def editor_export(
    video: UploadFile = File(...),
    narration: UploadFile | None = File(None),
    subtitles: UploadFile | None = File(None),
    trim_start: float = Form(0),
    trim_end: float = Form(0),
    keep_original_audio: bool = Form(True),
    rights_confirm: bool = Form(False),
    upscale_to: str = Form("original"),
    target_fps: str = Form("original"),
):
    """Small, single-clip mobile editor export. Files are temporary and removed after response."""
    if not rights_confirm:
        raise HTTPException(403, "导出前请确认素材由你创作、已获明确授权，或有其他适用的合法使用依据。")
    if trim_start < 0 or trim_end < 0 or trim_start > 3600 or trim_end > 3600:
        raise HTTPException(400, "剪辑时间无效。")
    if not video.filename:
        raise HTTPException(400, "请先选择视频文件。")
    allowed_video = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
    if Path(video.filename).suffix.lower() not in allowed_video:
        raise HTTPException(400, "视频格式仅支持 MP4、MOV、M4V、MKV、WebM。")
    if narration and narration.filename and Path(narration.filename).suffix.lower() not in {".mp3", ".m4a", ".aac", ".wav", ".caf", ".mp4", ".mov"}:
        raise HTTPException(400, "配音格式暂不支持。")
    if subtitles and subtitles.filename and Path(subtitles.filename).suffix.lower() != ".srt":
        raise HTTPException(400, "字幕文件请使用 SRT 格式。")

    if not DOWNLOAD_LOCK.acquire(timeout=1):
        raise HTTPException(503, "服务器正在处理另一个视频任务，请稍后再试。")

    job_dir = None
    try:
        job_dir = create_job_dir("edit-")
        input_path = job_dir / "source" / ("input" + Path(video.filename).suffix.lower())
        input_path.parent.mkdir(parents=True, exist_ok=True)
        video_size = 0
        with input_path.open("wb") as dst:
            while True:
                chunk = video.file.read(1024 * 1024)
                if not chunk:
                    break
                video_size += len(chunk)
                if video_size > 300 * 1024 * 1024:
                    raise HTTPException(413, "单个视频最大支持 300 MB；请先压缩或缩短素材。")
                dst.write(chunk)

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(input_path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        probe_data = json.loads(probe.stdout)
        streams = probe_data.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not video_stream:
            raise HTTPException(422, "文件中没有可识别的视频轨道。")
        duration = float((probe_data.get("format") or {}).get("duration") or 0)
        if duration <= 0 or duration > 600:
            raise HTTPException(413, "当前手机编辑器单条素材限制为 10 分钟。")
        if upscale_to not in {"original", "1080", "1440", "2160"}:
            raise HTTPException(400, "超分辨率档位无效。")
        if target_fps not in {"original", "30", "60"}:
            raise HTTPException(400, "目标帧率无效。")
        source_fps_text = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
        try:
            source_fps_num, source_fps_den = source_fps_text.split("/", 1)
            source_fps = float(source_fps_num) / max(float(source_fps_den), 1e-9)
        except (ValueError, TypeError):
            source_fps = 0.0
        requested_fps = float(target_fps) if target_fps != "original" else 0.0
        if upscale_to == "2160" and duration > 180:
            raise HTTPException(413, "免费 CPU 实例的 4K 增强限制为 3 分钟；请缩短视频后再试。")
        start = min(float(trim_start), duration)
        end = duration - float(trim_end) if trim_end > 0 else duration
        if end <= start or end - start < 0.5:
            raise HTTPException(400, "剪辑后时长至少需要 0.5 秒。")

        hdr_transfer = (video_stream.get("color_transfer") or "").lower()
        is_hdr = hdr_transfer in ("smpte2084", "arib-std-b67")
        output = job_dir / "edited-enhanced.mp4"
        filters = []
        interpolate_fps = (
            requested_fps > 0 and source_fps > 0
            and source_fps < requested_fps - 0.5
        )
        source_width = int(video_stream.get("width") or 0)
        source_height = int(video_stream.get("height") or 0)
        staged_4k_interpolation = upscale_to == "2160" and interpolate_fps
        if staged_4k_interpolation:
            # The 512 MiB instance cannot hold 4K frames and motion-estimation buffers
            # together. Process in two sequential FFmpeg runs: 720p interpolation,
            # then 4K scaling. Keep the original upload untouched.
            if source_width <= 0 or source_height <= 0 or source_width * source_height > 1920 * 1080:
                raise HTTPException(422, "4K 分阶段增强仅支持 1080p 及以下原片；原片未修改。")
            if end - start > 30:
                raise HTTPException(422, "免费实例的 4K 分阶段增强单次最多 30 秒；原片未修改。")
            try:
                validate_enhancement_limits(
                    width=1280, height=720, duration=end - start,
                    upscale_to="original", target_fps=requested_fps,
                    source_fps=source_fps, interpolate=True,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc))
        else:
            try:
                validate_enhancement_limits(
                    width=source_width, height=source_height,
                    duration=end - start, upscale_to=upscale_to,
                    target_fps=requested_fps, source_fps=source_fps,
                    interpolate=interpolate_fps,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc))
        # Interpolate before scaling so motion estimation works on the smaller source
        # frames. Clone a short boundary at both ends so MCI has enough context, then
        # trim to the exact target frame count to avoid dropping the final frames.
        if staged_4k_interpolation:
            filters.append("scale='if(gte(iw,ih),-2,720)':'if(gte(iw,ih),720,-2)':flags=lanczos")
        if interpolate_fps:
            pad_seconds = 0.2
            pad_frames = int(round(pad_seconds * requested_fps))
            expected_frames = max(1, int((end - start) * requested_fps + 0.999999))
            filters.append(
                f"tpad=start_mode=clone:start_duration={pad_seconds}:"
                f"stop_mode=clone:stop_duration={pad_seconds},"
                f"minterpolate=fps={requested_fps:g}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,"
                f"trim=start_frame={pad_frames}:end_frame={pad_frames + expected_frames},"
                f"setpts=N/({requested_fps:g}*TB)"
            )
        if upscale_to != "original" and not staged_4k_interpolation:
            target_edge = int(upscale_to)
            # Treat 1080/1440/2160 as the conventional short-edge target
            # (1080p / 1440p / 2160p), preserving portrait and landscape orientation.
            if min(int(video_stream.get("width") or 0), int(video_stream.get("height") or 0)) < target_edge:
                filters.append(
                    f"scale='if(gte(iw,ih),-2,{target_edge})':'if(gte(iw,ih),{target_edge},-2)':flags=lanczos"
                )
        if filters:
            command_filter = ",".join(filters)
        else:
            command_filter = ""
        command = ["ffmpeg", "-y", "-threads", "1", "-filter_threads", "1", "-ss", str(start), "-i", str(input_path)]
        if command_filter:
            command += ["-vf", command_filter]
        audio_path = None
        subtitle_path = None
        if narration and narration.filename:
            audio_path = job_dir / ("voice" + Path(narration.filename).suffix.lower())
            audio_size = 0
            with audio_path.open("wb") as dst:
                while True:
                    chunk = narration.file.read(1024 * 1024)
                    if not chunk:
                        break
                    audio_size += len(chunk)
                    if audio_size > 100 * 1024 * 1024:
                        raise HTTPException(413, "配音文件最大支持 100 MB。")
                    dst.write(chunk)
            command += ["-i", str(audio_path)]
        if subtitles and subtitles.filename:
            subtitle_path = job_dir / "captions.srt"
            subtitle_size = 0
            with subtitle_path.open("wb") as dst:
                while True:
                    chunk = subtitles.file.read(1024 * 1024)
                    if not chunk:
                        break
                    subtitle_size += len(chunk)
                    if subtitle_size > 5 * 1024 * 1024:
                        raise HTTPException(413, "字幕文件最大支持 5 MB。")
                    dst.write(chunk)
            command += ["-i", str(subtitle_path)]

        command += ["-t", str(end - start), "-map", "0:v:0"]
        if interpolate_fps:
            command += ["-r", f"{requested_fps:g}", "-fps_mode", "cfr"]
        if narration and narration.filename:
            command += ["-map", "1:a:0"]
        elif keep_original_audio:
            command += ["-map", "0:a?"]
        else:
            command += ["-an"]

        if is_hdr:
            command += [
                "-c:v", "libx265", "-preset", "ultrafast", "-crf", "20",
                "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1", "-threads:v", "1",
                "-x265-params", "pools=1:frame-threads=1",
                "-color_primaries", video_stream.get("color_primaries", "bt2020"),
                "-color_trc", video_stream.get("color_transfer", "smpte2084"),
                "-colorspace", video_stream.get("color_space", "bt2020nc"),
            ]
        else:
            command += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads:v", "1"]
        if narration and narration.filename:
            command += ["-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-c:a", "aac", "-b:a", "192k"]
        if subtitle_path:
            # MP4 text subtitle track; keep it selectable instead of burning it into pixels.
            subtitle_input_index = 2 if narration and narration.filename else 1
            command += ["-map", f"{subtitle_input_index}:0", "-c:s", "mov_text"]
        stage1_output = job_dir / "stage1-720p-30fps.mp4" if staged_4k_interpolation else output
        command += ["-movflags", "+faststart", str(stage1_output)]

        logger.info(
            "editor job %s: export start hdr_input=%s trim=%s..%s upscale=%s target_fps=%s source_fps=%.3f filters=%s",
            job_dir.name, is_hdr, start, end, upscale_to, target_fps, source_fps, command_filter,
        )
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=1800, check=True)
        except subprocess.CalledProcessError as exc:
            logger.error("editor job %s: ffmpeg failed rc=%s stderr=%s", job_dir.name, exc.returncode, (exc.stderr or "")[-3000:])
            raise HTTPException(422, "剪辑导出失败。请尝试缩短视频或降低素材规格。")
        if staged_4k_interpolation:
            # Stage 2: upscale the completed 720p intermediate in a separate process.
            # Copy audio/subtitle streams so they are not re-encoded a second time.
            scale_filter = "scale='if(gte(iw,ih),-2,2160)':'if(gte(iw,ih),2160,-2)':flags=lanczos"
            stage2 = [
                "ffmpeg", "-y", "-threads", "1", "-filter_threads", "1",
                "-i", str(stage1_output), "-map", "0", "-vf", scale_filter,
            ]
            if is_hdr:
                stage2 += [
                    "-c:v", "libx265", "-preset", "ultrafast", "-crf", "20",
                    "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1", "-threads:v", "1",
                    "-x265-params", "pools=1:frame-threads=1",
                    "-color_primaries", video_stream.get("color_primaries", "bt2020"),
                    "-color_trc", video_stream.get("color_transfer", "smpte2084"),
                    "-colorspace", video_stream.get("color_space", "bt2020nc"),
                ]
            else:
                stage2 += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                           "-pix_fmt", "yuv420p", "-threads:v", "1"]
            stage2 += ["-c:a", "copy", "-c:s", "copy", "-movflags", "+faststart", str(output)]
            logger.info("editor job %s: stage 2/2 upscale 720p intermediate to 4K", job_dir.name)
            try:
                subprocess.run(stage2, capture_output=True, text=True, timeout=1800, check=True)
            except subprocess.CalledProcessError as exc:
                logger.error("editor job %s: 4K upscale stage failed rc=%s stderr=%s", job_dir.name, exc.returncode, (exc.stderr or "")[-3000:])
                raise HTTPException(422, "720p/30FPS 阶段已完成，但 4K 放大阶段失败。原始上传文件未修改，请稍后重试。")
        if not output.exists() or output.stat().st_size == 0:
            raise HTTPException(422, "没有生成有效的导出文件。")
        logger.info("editor job %s: export complete size=%s hdr_source=%s staged_4k=%s", job_dir.name, output.stat().st_size, is_hdr, staged_4k_interpolation)
        return FileResponse(
            output,
            filename="VideoDrop-Enhanced.mp4",
            media_type="video/mp4",
            background=BackgroundTask(cleanup_job_dir, job_dir),
        )
    except HTTPException:
        if job_dir is not None:
            cleanup_job_dir(job_dir)
        raise
    except Exception:
        logger.exception("editor job %s: unexpected failure", job_dir.name if job_dir else "unallocated")
        if job_dir is not None:
            cleanup_job_dir(job_dir)
        raise HTTPException(422, "剪辑处理失败，详细原因已记录。")
    finally:
        DOWNLOAD_LOCK.release()
