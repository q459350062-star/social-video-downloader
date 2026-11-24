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
from urllib.parse import urlparse

from .checksums import calculate_file_hashes
from .runtime import (
    DOWNLOAD_ROOT, DOWNLOAD_LOCK, cleanup_job_dir, cleanup_stale_jobs, create_job_dir,
)

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
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
}
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "3600"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "1024"))

app = FastAPI(title="Social Video Downloader", version="1.3.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


@app.on_event("startup")
def remove_abandoned_job_files():
    """Clean temporary workspaces left behind by a previous process restart."""
    cleanup_stale_jobs()


class ResolveRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    quality: str
    output_mode: str = "ios"


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
    hdr_values = sorted({
        str(f.get("dynamic_range")) for f in formats
        if f.get("dynamic_range") and str(f.get("dynamic_range")).upper() != "SDR"
    })
    codecs = sorted({
        str(f.get("vcodec")).split(".")[0] for f in formats
        if f.get("vcodec") not in (None, "none")
    })
    known_ranges = {str(f.get("dynamic_range")).upper() for f in formats if f.get("dynamic_range")}
    hdr_status = "HDR可用" if hdr_values else ("仅报告SDR" if known_ranges and known_ranges <= {"SDR"} else "源站未提供明确HDR标记")
    return {
        "max_resolution": f"{best.get('width')}×{best.get('height')}" if best.get("width") and best.get("height") else (f"最高 {best.get('height')}P" if best.get("height") else "平台未提供分辨率信息"),
        "max_fps": best.get("fps"),
        "video_codecs": codecs,
        "best_video_codec": str(best.get("vcodec") or "").split(".")[0] or None,
        "best_dynamic_range": best.get("dynamic_range"),
        "hdr": hdr_values,
        "hdr_available": bool(hdr_values),
        "hdr_status": hdr_status,
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
        msg = str(exc).lower()
        if any(word in msg for word in ("private", "login", "sign in", "not available")):
            raise HTTPException(422, "该内容可能不是公开内容，或平台要求登录才能访问。")
        raise HTTPException(422, "解析失败：链接可能失效、内容不可公开访问，或平台暂时限制了服务器请求。")


@app.get("/api/download-file")
def download_file(url: HttpUrl, quality: str, output_mode: str = "ios"):
    """Native browser download path; avoids buffering the whole video in JS Blob memory."""
    return download(DownloadRequest(url=url, quality=quality, output_mode=output_mode))


@app.post("/api/download")
def download(req: DownloadRequest):
    url = validate_url(str(req.url))
    if req.quality != "best" and not re.fullmatch(r"height:[0-9]{1,5}", req.quality):
        raise HTTPException(400, "画质选项无效，请重新解析。")
    if req.output_mode not in ("ios", "source"):
        raise HTTPException(400, "输出模式无效，请重新选择。")
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

    job_dir = None
    try:
        job_dir = create_job_dir()
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
        start = min(float(trim_start), duration)
        end = duration - float(trim_end) if trim_end > 0 else duration
        if end <= start or end - start < 0.5:
            raise HTTPException(400, "剪辑后时长至少需要 0.5 秒。")

        hdr_transfer = (video_stream.get("color_transfer") or "").lower()
        is_hdr = hdr_transfer in ("smpte2084", "arib-std-b67")
        output = job_dir / "edited.mp4"
        command = ["ffmpeg", "-y", "-threads", "1", "-filter_threads", "1", "-ss", str(start), "-i", str(input_path)]
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
        command += ["-movflags", "+faststart", str(output)]

        logger.info("editor job %s: export start hdr_input=%s trim=%s..%s", job_dir.name, is_hdr, start, end)
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=1800, check=True)
        except subprocess.CalledProcessError as exc:
            logger.error("editor job %s: ffmpeg failed rc=%s stderr=%s", job_dir.name, exc.returncode, (exc.stderr or "")[-3000:])
            raise HTTPException(422, "剪辑导出失败。请尝试缩短视频或降低素材规格。")
        if not output.exists() or output.stat().st_size == 0:
            raise HTTPException(422, "没有生成有效的导出文件。")
        logger.info("editor job %s: export complete size=%s hdr_source=%s", job_dir.name, output.stat().st_size, is_hdr)
        return FileResponse(
            output,
            filename="VideoDrop-Edited.mp4",
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
