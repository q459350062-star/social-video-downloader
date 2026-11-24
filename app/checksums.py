import hashlib


def calculate_file_hashes(fileobj, max_bytes: int = 300 * 1024 * 1024, chunk_size: int = 1024 * 1024) -> dict:
    """Stream MD5/SHA-256 for integrity checks without storing or changing file bytes."""
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    size = 0
    while True:
        chunk = fileobj.read(chunk_size)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("文件超过大小限制。")
        md5.update(chunk)
        sha256.update(chunk)
    if size == 0:
        raise ValueError("文件为空。")
    return {"size_bytes": size, "md5": md5.hexdigest(), "sha256": sha256.hexdigest()}
