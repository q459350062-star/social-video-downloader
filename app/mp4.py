import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("social-video-downloader")

# H.264 and HEVC are supported by modern iPhones in MP4. VP9/VP8/AV1
# inside MP4 are not reliably decoded by iPhone Photos, even when audio works.
IPHONE_MP4_VIDEO_CODECS = {"h264", "hevc", "h265"}
MP4_AUDIO_CODECS = {"aac", "alac", "mp3", "ac3", "eac3"}


def ensure_mp4_container(path: Path) -> Path:
    """Produce a playable MP4; transcode unsupported video codecs to H.264.

    Compatible H.264/HEVC video is stream-copied to preserve source quality.
    VP9/VP8/AV1 and other unsupported codecs are transcoded to H.264 while
    retaining source dimensions and frame rate. Audio is copied when compatible,
    otherwise converted to AAC. FFmpeg is deliberately limited to one thread
    to reduce memory pressure on the 512 MiB Render instance.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video_stream:
        raise ValueError("下载文件没有视频画面轨道，无法生成可播放 MP4。")

    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
    video_codec = (video_stream.get("codec_name") or "").lower()
    audio_codec = (audio_stream.get("codec_name") or "").lower() if audio_stream else ""
    video_needs_transcode = video_codec not in IPHONE_MP4_VIDEO_CODECS
    audio_needs_transcode = bool(audio_stream and audio_codec not in MP4_AUDIO_CODECS)
    needs_mp4 = path.suffix.lower() != ".mp4"

    if not (video_needs_transcode or audio_needs_transcode or needs_mp4):
        return path

    output_path = path.with_name(path.stem + "-iphone.mp4")
    command = [
        "ffmpeg", "-y", "-threads", "1", "-filter_threads", "1", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
    ]
    if video_needs_transcode:
        command += [
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-threads:v", "1",
        ]
    else:
        command += ["-c:v", "copy"]
        if video_codec in ("hevc", "h265"):
            command += ["-tag:v", "hvc1"]
    if audio_needs_transcode:
        command += ["-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-c:a", "copy"]
    command += ["-movflags", "+faststart", "-f", "mp4", str(output_path)]

    try:
        subprocess.run(command, capture_output=True, text=True, timeout=1800, check=True)
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not create a non-empty MP4 file.")
        verify = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output_path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        verified_streams = json.loads(verify.stdout).get("streams", [])
        verified_video = next((item for item in verified_streams if item.get("codec_type") == "video"), None)
        if not verified_video or (verified_video.get("codec_name") or "").lower() != (
            "h264" if video_needs_transcode else video_codec
        ):
            raise RuntimeError("Output MP4 video codec verification failed.")
        if audio_stream and not any(item.get("codec_type") == "audio" for item in verified_streams):
            raise RuntimeError("Output MP4 is missing its audio stream.")
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        try:
            output_path.unlink()
        except OSError:
            pass
        logger.error(
            "iPhone MP4 conversion failed source=%s video_codec=%s audio_codec=%s error=%s",
            path.name, video_codec, audio_codec, str(exc)[:1000],
        )
        raise ValueError(
            "视频编码转换或校验失败，未交付无画面的文件。请稍后重试，原始下载文件未修改。"
        ) from exc

    try:
        path.unlink()
    except OSError:
        logger.warning("Could not remove original temporary file: %s", path.name)
    logger.info(
        "iPhone MP4 ready source=%s output=%s video=%s->%s audio=%s",
        path.name, output_path.name, video_codec,
        "h264" if video_needs_transcode else video_codec, audio_codec,
    )
    return output_path
