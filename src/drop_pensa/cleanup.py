"""
Background cleanup worker.

Two responsibilities:
1. Find rows past their expires_at, soft-delete them, and remove the
   on-disk file. (We soft-delete first so a concurrent fetch sees a 404
   immediately, before we touch the disk.)
2. Hard-delete soft-deleted rows older than 24h to keep the DB small.

Runs as an asyncio task started/stopped from the FastAPI lifespan.
The work itself is sync DB+filesystem; we run it on the default
threadpool via asyncio.to_thread to avoid blocking the event loop.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from drop_pensa.config import settings
from drop_pensa.storage.db import DbStore, _to_iso, session_scope
from drop_pensa.storage.filesystem import delete_file


# Soft-deleted rows live for this long before we hard-delete and free disk.
SOFT_DELETE_GRACE = timedelta(hours=24)


def _cleanup_once() -> dict:
    """
    Run one pass of expiry + hard-delete. Returns a small stats dict.

    This is the unit a test or admin can call directly. The async loop
    just wraps repeated calls of this with a sleep in between.
    """
    expired_count = 0
    bytes_freed = 0
    hard_deleted_count = 0
    errors = 0

    # --- pass 1: expire what's past expires_at ---
    with session_scope() as sess:
        store = DbStore(sess)
        ids_to_expire = store.expired_ids(limit=1000)

    # Soft-delete + on-disk removal happens in its own session per id so
    # that a single corrupt file can't poison the whole batch.
    for fid in ids_to_expire:
        try:
            with session_scope() as sess:
                store = DbStore(sess)
                meta = store.get(fid)
                if meta is None:
                    continue
                store.soft_delete(fid)
                expired_count += 1
                bytes_freed += meta.size
            # Disk delete outside the DB tx — slow filesystem calls
            # shouldn't hold a write lock.
            delete_file(settings.storage_dir, fid)
        except Exception as e:
            errors += 1
            print(f"[cleanup] error expiring {fid}: {e}")

    # --- pass 2: hard-delete old soft-deleted rows ---
    threshold = _to_iso(datetime.now(timezone.utc) - SOFT_DELETE_GRACE)
    try:
        with session_scope() as sess:
            store = DbStore(sess)
            removed_ids = store.hard_delete_old_soft_deletes(threshold, limit=1000)
            hard_deleted_count = len(removed_ids)
        # Best-effort disk cleanup for these too. They were soft-deleted
        # already so the file might be gone; ignore missing.
        for fid in removed_ids:
            try:
                delete_file(settings.storage_dir, fid)
            except Exception:
                pass
    except Exception as e:
        errors += 1
        print(f"[cleanup] error in hard-delete pass: {e}")

    return {
        "expired": expired_count,
        "bytes_freed": bytes_freed,
        "hard_deleted": hard_deleted_count,
        "errors": errors,
    }


async def cleanup_loop(stop_event: asyncio.Event) -> None:
    """Run cleanup_once at the configured interval until stop_event is set."""
    interval = settings.cleanup_interval_seconds
    print(f"[cleanup] worker starting, interval={interval}s")
    while not stop_event.is_set():
        try:
            stats = await asyncio.to_thread(_cleanup_once)
            if any(stats[k] for k in ("expired", "hard_deleted", "errors")):
                print(f"[cleanup] {stats}")
        except Exception as e:
            print(f"[cleanup] unexpected error: {e}")

        # Wait either for the interval to elapse OR for shutdown.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    print("[cleanup] worker stopped")
