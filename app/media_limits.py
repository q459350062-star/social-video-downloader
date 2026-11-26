"""Memory-conscious enhancement limits for the 512 MiB Render Free instance."""

MAX_INPUT_PIXELS = 3840 * 2160
MAX_INTERPOLATION_PIXELS = 1280 * 720


def validate_enhancement_limits(
    *,
    width: int,
    height: int,
    duration: float,
    upscale_to: str,
    target_fps: float,
    source_fps: float,
    interpolate: bool,
) -> None:
    """Reject FFmpeg workloads that are unsafe for the small production container."""
    if width <= 0 or height <= 0:
        raise ValueError("无法读取视频分辨率，请换一个有效的视频文件。")

    pixels = width * height
    if pixels > MAX_INPUT_PIXELS:
        raise ValueError("当前免费服务器最多处理原生 4K 输入；更高分辨率请先在本地缩小。")

    if interpolate:
        if pixels > MAX_INTERPOLATION_PIXELS:
            raise ValueError(
                "为避免服务器内存溢出，运动插帧目前仅支持 720p 及以下素材。"
                "请先降低原视频分辨率，再选择 30/60FPS。"
            )
        if target_fps >= 59.5 and duration > 30:
            raise ValueError(
                "免费服务器的 60FPS 插帧单次最多 30 秒；请缩短素材后重试。"
            )
        if target_fps < 59.5 and duration > 60:
            raise ValueError(
                "免费服务器的 30FPS 插帧单次最多 60 秒；请缩短素材后重试。"
            )

    if upscale_to == "2160":
        if interpolate:
            raise ValueError(
                "4K 放大与运动插帧需要由导出接口自动分阶段执行；请使用最新版本后重试。"
            )
        if pixels > 1920 * 1080:
            raise ValueError("4K 放大仅接受 1080p 及以下源素材，避免高分辨率重复编码导致内存溢出。")
        if duration > 30:
            raise ValueError("免费服务器的 4K 放大单次最多 30 秒；请缩短素材后重试。")
    elif upscale_to == "1440":
        if pixels > 1920 * 1080:
            raise ValueError("1440p 放大仅接受 1080p 及以下源素材。")
        if duration > 60:
            raise ValueError("免费服务器的 1440p 放大单次最多 60 秒；请缩短素材后重试。")
    elif upscale_to == "1080":
        if duration > 120:
            raise ValueError("免费服务器的 1080p 放大单次最多 120 秒；请缩短素材后重试。")
