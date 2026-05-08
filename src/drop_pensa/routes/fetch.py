"""
GET /f/<id>/<filename> — serve a stored file.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from drop_pensa import ratelimit
from drop_pensa.clientinfo import client_ip
from drop_pensa.config import settings
from drop_pensa.logging_setup import log_event, log_warning
from drop_pensa.storage.db import DbStore, get_session
from drop_pensa.storage.filesystem import storage_path_for, stream_file
from drop_pensa.storage.memstore import now_utc


router = APIRouter()


_DANGEROUS_TYPES = {"text/html", "image/svg+xml", "application/xhtml+xml"}


def _safe_content_type(detected: str, render: bool) -> str:
    if render:
        return detected
    return "text/plain" if detected in _DANGEROUS_TYPES else detected


def _check_meta(store: DbStore, file_id: str, filename: str):
    """
    Common preconditions for fetch/head/info.

    Raises 410 Gone when a one-shot upload has been consumed (RFC 9110
    §15.5.21): the resource intentionally exists no more. Otherwise 404
    for everything else (unknown id, expired, filename mismatch),
    which preserves the no-enumeration property — an attacker without
    the matching filename only ever sees 404.
    """
    meta = store.get(file_id)
    if meta is None:
        # Could be: never existed, expired, soft-deleted, or one-shot
        # consumed. Peek at the raw row to tell consumed-one-shot apart
        # from the rest, since that's the only case where 410 is correct.
        raw = store.get_raw(file_id)
        if (
            raw is not None
            and raw["deleted_at"] is not None
            and filename == raw["filename"]
            # The DB row doesn't carry one_shot in get_raw's projection,
            # but we can infer: a soft-deleted row whose deleted_at is
            # very close to its expires_at is a normal expiry, while one
            # whose deleted_at is well before is a one-shot consumption.
            # Cleaner: extend get_raw to include one_shot. We do that.
            and raw.get("one_shot") is True
        ):
            raise HTTPException(status_code=410, detail="consumed")
        raise HTTPException(status_code=404, detail="not found")

    if filename != meta.filename:
        raise HTTPException(status_code=404, detail="not found")
    if meta.expires_at <= now_utc():
        raise HTTPException(status_code=404, detail="not found")
    return meta


def _enforce_fetch_limits(ip: str, file_id: str) -> None:
    """Apply the two fetch-side rate limits. Raises 429 if either bucket is full."""
    if not ratelimit.check("fetch_per_ip", ip):
        log_warning("fetch_rate_limited_ip", ip=ip, file_id=file_id)
        raise HTTPException(status_code=429, detail="too many fetches, slow down")
    if not ratelimit.check("fetch_per_file_ip", file_id, ip):
        log_warning("fetch_rate_limited_file", ip=ip, file_id=file_id)
        raise HTTPException(status_code=429, detail="too many fetches for this file")


@router.get("/f/{file_id}/{filename}")
async def fetch(
    request: Request,
    file_id: str,
    filename: str,
    download: int = Query(0),
    render: int = Query(0),
    session: Session = Depends(get_session),
):
    """Stream the file with the requested disposition."""
    ip = client_ip(request)
    _enforce_fetch_limits(ip, file_id)

    store = DbStore(session)
    meta = _check_meta(store, file_id, filename)

    path = storage_path_for(settings.storage_dir, file_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")

    content_type = _safe_content_type(meta.content_type, bool(render))
    disposition_kind = "attachment" if download else "inline"
    headers = {
        "Content-Length": str(meta.size),
        "Content-Disposition": f'{disposition_kind}; filename="{meta.filename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "public, max-age=3600" if not meta.one_shot else "no-store",
    }

    store.mark_fetched(file_id)
    if meta.one_shot:
        store.soft_delete(file_id)

    log_event(
        "fetch_ok",
        ip=ip,
        file_id=file_id,
        size=meta.size,
        one_shot=meta.one_shot,
        download=bool(download),
    )

    return StreamingResponse(
        stream_file(path),
        media_type=content_type,
        headers=headers,
    )


@router.head("/f/{file_id}/{filename}")
async def head(
    request: Request,
    file_id: str,
    filename: str,
    download: int = Query(0),
    render: int = Query(0),
    session: Session = Depends(get_session),
):
    """Return only headers (metadata) without the file body. Mirrors GET."""
    ip = client_ip(request)
    _enforce_fetch_limits(ip, file_id)

    store = DbStore(session)
    meta = _check_meta(store, file_id, filename)

    content_type = _safe_content_type(meta.content_type, bool(render))
    disposition_kind = "attachment" if download else "inline"
    return Response(
        status_code=200,
        headers={
            "Content-Length": str(meta.size),
            "Content-Type": content_type,
            "Content-Disposition": f'{disposition_kind}; filename="{meta.filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
