import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("social-video-downloader")


def ensure_mp4_container(path: Path) -> Path:
    """Return an MP4 container without re-encoding compatible video streams.

    MKV/WebM is not a usable final format for this mobile downloader. Remux video
    losslessly; only transcode audio when the source audio codec is not suitable
    for a broadly compatible MP4/iPhone file.
    """
    if path.suffix.lower() == ".mp4":
        return path

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video_stream:
        raise ValueError("下载文件没有视频画面轨道，无法封装为 MP4。")

    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
    audio_codec = (audio_stream.get("codec_name") or "").lower() if audio_stream else ""
    output_path = path.with_name(path.stem + "-mp4.mp4")
    command = [
        "ffmpeg", "-y", "-threads", "1", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
        "-c:v", "copy",
    ]
    if (video_stream.get("codec_name") or "").lower() in ("hevc", "h265"):
        command += ["-tag:v", "hvc1"]
    if audio_stream and audio_codec not in ("aac", "alac", "mp3", "ac3", "eac3"):
        command += ["-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-c:a", "copy"]
    command += ["-movflags", "+faststart", "-f", "mp4", str(output_path)]

    try:
        subprocess.run(command, capture_output=True, text=True, timeout=600, check=True)
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not create a non-empty MP4 file.")
        verify = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output_path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        verified_streams = json.loads(verify.stdout).get("streams", [])
        if not any(item.get("codec_type") == "video" for item in verified_streams):
            raise RuntimeError("Remuxed MP4 has no video stream.")
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        try:
            output_path.unlink()
        except OSError:
            pass
        logger.error("MP4 remux failed source=%s error=%s", path.name, str(exc)[:1000])
        raise ValueError(
            "源视频编码无法直接封装为 MP4；原始下载文件未交付，避免产生只有声音或无法播放的文件。"
        ) from exc

    try:
        path.unlink()
    except OSError:
        logger.warning("Could not remove original non-MP4 temporary file: %s", path.name)
    logger.info("MP4 remux complete source=%s output=%s", path.name, output_path.name)
    return output_path
