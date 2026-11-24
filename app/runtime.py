"""Small, process-local runtime helpers for the single-instance deployment."""
import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("social-video-downloader.runtime")

DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "social-video-downloader"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# Render Free currently runs one web instance with one Uvicorn worker.
# This lock protects the limited-memory FFmpeg/download pipeline in that process.
DOWNLOAD_LOCK = threading.Lock()


def create_job_dir(prefix: str = "") -> Path:
    """Create a private per-request workspace under DOWNLOAD_ROOT."""
    if prefix and (not prefix.replace("-", "").isalnum() or len(prefix) > 24):
        raise ValueError("Invalid job directory prefix")
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(DOWNLOAD_ROOT)))


def cleanup_job_dir(path: Path) -> None:
    """Remove a job workspace only when it is a direct child of DOWNLOAD_ROOT."""
    root = DOWNLOAD_ROOT.resolve()
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=False)
        if resolved.parent != root or candidate.is_symlink():
            logger.warning("Refusing to clean path outside job root: %s", candidate)
            return
        shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
        logger.exception("Failed to clean job directory: %s", candidate)


def cleanup_stale_jobs(max_age_seconds: int = 24 * 60 * 60) -> int:
    """Remove abandoned job directories left by a process restart.

    Startup runs before the app accepts requests, so no live job can be removed.
    Only direct child directories older than the retention window are eligible.
    """
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    now = time.time()
    removed = 0
    for entry in DOWNLOAD_ROOT.iterdir():
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if now - entry.stat().st_mtime < max_age_seconds:
                continue
            shutil.rmtree(entry)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("Could not remove stale job directory: %s", entry)
    if removed:
        logger.info("Startup cleanup removed %s stale job directories", removed)
    return removed
