"""
GET /f/{id}/info — return file metadata without serving the body.

Useful for clients that want to check a URL is still alive, or want to
display size/expiry/sha256 before deciding whether to download. Distinct
from HEAD on the file URL because that one requires the filename and
hits the rate-limit-per-file bucket; this one is lighter and doesn't
need the filename in the path.

Security note: anyone with the id can call this. The id is the secret.
We don't return the delete_token_hash, the source IP, or the delete URL —
metadata only.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from drop_pensa import ratelimit
from drop_pensa.clientinfo import client_ip
from drop_pensa.storage.db import DbStore, get_session
from drop_pensa.storage.memstore import now_utc

from sqlalchemy.orm import Session


router = APIRouter()


@router.get("/f/{file_id}/info")
async def info(
    request: Request,
    file_id: str,
    session: Session = Depends(get_session),
):
    """Return public metadata for a stored file."""
    ip = client_ip(request)

    # Cheap rate limit; share the same bucket as fetches so an abuser
    # can't use info as an enumeration probe with a separate budget.
    if not ratelimit.check("fetch_per_ip", ip):
        raise HTTPException(status_code=429, detail="too many requests")

    store = DbStore(session)
    meta = store.get(file_id)
    if meta is None or meta.expires_at <= now_utc():
        raise HTTPException(status_code=404, detail="not found")

    return {
        "id": meta.id,
        "filename": meta.filename,
        "size_bytes": meta.size,
        "sha256": meta.sha256,
        "content_type": meta.content_type,
        "created_at": meta.created_at.isoformat().replace("+00:00", "Z"),
        "expires_at": meta.expires_at.isoformat().replace("+00:00", "Z"),
        "one_shot": meta.one_shot,
        "fetched_count": meta.fetched_count,
    }
