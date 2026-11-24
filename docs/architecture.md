# Architecture notes

## Current deployment boundary

- One Render web service: `social-video-downloader`.
- One Uvicorn worker on the current Free instance; the media lock is process-local.
- No Redis/Valkey, background worker, persistent disk, or additional paid service is required by this phase.
- The legacy `videohub-api-c0uf` service is out of scope.

## Temporary media lifecycle

- Every download, metadata, or editor request gets its own directory below the system temporary directory `social-video-downloader/`.
- The shared runtime module owns directory creation, safe cleanup, and the process-local media lock.
- Successful file responses remove their workspace after the response has been sent.
- Failed requests remove their workspace immediately.
- Startup removes abandoned child directories older than 24 hours, which covers workspaces left behind when the process restarts before a response cleanup callback can run.
- Cleanup refuses paths outside the dedicated job root and skips symlinks.

## Resource limits and media behavior

- Keep media processing serialized on the current 512 MiB-class Free instance.
- Continue probing actual downloaded streams with ffprobe; do not infer HDR solely from the URL or filename.
- Prefer stream copy for already compatible source video/audio. Only transcode when the selected iPhone output requires it.
- Re-encoding cannot create genuine HDR or change who filmed/owns a source video.
- Temporary workspaces are not durable storage; a response must finish before its workspace is removed.

## Next safe phases

1. Add endpoint-level regression tests with mocked yt-dlp and FFmpeg.
2. Separate downloader, metadata, and editor pipelines into focused modules without changing their HTTP contracts.
3. Add explicit media probe reporting for source, downloaded file, and final iPhone output.
4. Consider a durable queue/worker only if measured traffic justifies the added service and operational cost.
