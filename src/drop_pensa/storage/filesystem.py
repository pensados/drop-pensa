"""
Filesystem storage for uploaded files.

We store files at <storage_dir>/<shard>/<file_id>, where <shard> is the
first 2 chars of <file_id>. The original filename is NOT used as a
filesystem path — we keep it in metadata only. This decouples the URL
namespace from the disk layout and avoids whole classes of filename-
related issues.
"""
import hashlib
from pathlib import Path
from typing import AsyncIterator, BinaryIO

from drop_pensa.security import shard_path


# 1 MiB chunks: large enough that syscall overhead is amortized,
# small enough that a few concurrent uploads don't blow up RSS.
CHUNK_SIZE = 1024 * 1024


class FileTooLargeError(Exception):
    """Raised when an upload exceeds the configured size limit."""

    def __init__(self, limit_bytes: int):
        self.limit_bytes = limit_bytes
        super().__init__(f"file exceeds limit of {limit_bytes} bytes")


def storage_path_for(storage_dir: Path, file_id: str) -> Path:
    """Return the absolute path where the file with this id should live."""
    return storage_dir / shard_path(file_id) / file_id


async def save_stream(
    storage_dir: Path,
    file_id: str,
    source: BinaryIO,
    max_bytes: int,
) -> tuple[int, str]:
    """
    Save an uploaded stream to disk, computing sha256 on the fly.

    Aborts and removes the partial file if the stream exceeds max_bytes.
    Returns (size_in_bytes, sha256_hex).

    Note: `source` is the FastAPI UploadFile.file (a SpooledTemporaryFile),
    which gives us .read() but not async. We do the I/O synchronously here;
    moving it to a thread pool is a future optimization if it ever shows up
    in profiling.
    """
    target = storage_path_for(storage_dir, file_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()
    total = 0

    # Write to a tempfile alongside the target, then rename. This way
    # a crashed upload never leaves a half-written file at the canonical path.
    tmp_path = target.with_suffix(".part")

    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLargeError(max_bytes)
                hasher.update(chunk)
                out.write(chunk)

        tmp_path.replace(target)
        return total, hasher.hexdigest()

    except Exception:
        # Best-effort cleanup. If unlink itself fails, propagate the
        # original exception, not the cleanup error.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


async def stream_file(
    path: Path,
    start: int = 0,
    length: int | None = None,
) -> AsyncIterator[bytes]:
    """
    Yield a file's contents in chunks for streaming responses.

    Args:
        path: file to stream.
        start: byte offset to start at (0 = beginning).
        length: how many bytes to emit. None means "until EOF".

    Range-aware: pass `start` and `length` to serve a partial range
    without loading the whole file. The caller is responsible for
    range validation; this function trusts its inputs.
    """
    with path.open("rb") as f:
        if start:
            f.seek(start)
        remaining = length  # None means unbounded
        while True:
            to_read = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
            if to_read <= 0:
                break
            chunk = f.read(to_read)
            if not chunk:
                break
            yield chunk
            if remaining is not None:
                remaining -= len(chunk)


def delete_file(storage_dir: Path, file_id: str) -> bool:
    """
    Delete a file from disk. Returns True if it was deleted, False if missing.
    Does NOT raise on missing — deletion is idempotent at this layer.
    """
    target = storage_path_for(storage_dir, file_id)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
