import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, HttpUrl
import yt_dlp

APP_DIR = Path(__file__).parent
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "social-video-downloader"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

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


def ydl_options():
    return {
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
    formats.sort(key=lambda f: (f.get("height") or 0, f.get("fps") or 0), reverse=True)
    best = formats[0] if formats else {}
    hdr_values = sorted({str(f.get("dynamic_range")) for f in formats if f.get("dynamic_range")})
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
        with yt_dlp.YoutubeDL(ydl_options()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("没有取得视频信息")
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


@app.post("/api/download")
def download(req: DownloadRequest):
    url = validate_url(str(req.url))
    if req.quality != "best" and not re.fullmatch(r"height:[0-9]{1,5}", req.quality):
        raise HTTPException(400, "画质选项无效，请重新解析。")

    if req.quality == "best":
        selector = "bestvideo+bestaudio/best"
    else:
        height = int(req.quality.split(":", 1)[1])
        selector = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"

    job_dir = DOWNLOAD_ROOT / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        opts = ydl_options()
        opts.update({
            "format": selector,
            "outtmpl": str(job_dir / "%(title).80s-%(id)s.%(ext)s"),
            "max_filesize": MAX_FILE_MB * 1024 * 1024,
            "overwrites": True,
        })
        with yt_dlp.YoutubeDL(opts) as downloader:
            info = downloader.extract_info(url, download=True)
        if not info or info.get("is_live"):
            raise HTTPException(422, "无法下载该内容。")
        candidates = [p for p in job_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not candidates:
            raise HTTPException(422, "没有生成可下载文件。")
        path = max(candidates, key=lambda p: p.stat().st_size)
        if path.stat().st_size > MAX_FILE_MB * 1024 * 1024:
            raise HTTPException(413, "文件超过当前服务大小限制。")
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/octet-stream",
            background=BackgroundTask(shutil.rmtree, job_dir, ignore_errors=True),
        )
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(422, "下载失败：内容可能不可访问、格式已变化或超过文件大小限制。")