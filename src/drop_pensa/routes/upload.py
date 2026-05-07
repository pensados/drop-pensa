"""
POST /upload — receive a file, store it, return its public URL.
"""
import hashlib
from datetime import timedelta

import magic
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from drop_pensa import ratelimit
from drop_pensa.clientinfo import client_ip, is_foreign_origin
from drop_pensa.config import settings
from drop_pensa.logging_setup import log_event, log_warning
from drop_pensa.security import (
    generate_delete_token,
    generate_id,
    has_blocked_extension,
    sanitize_filename,
)
from drop_pensa.storage.db import DbStore, get_session
from drop_pensa.storage.filesystem import (
    FileTooLargeError,
    save_stream,
    storage_path_for,
)
from drop_pensa.storage.memstore import FileMeta, now_utc


router = APIRouter()


def _public_url(request: Request, file_id: str, filename: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/f/{file_id}/{filename}"


def _resolve_ttl(qv: int | None, fv: int | None) -> int:
    """Pick the TTL from query or form, falling back to default. Validate range."""
    raw = qv if qv is not None else fv
    if raw is None:
        return settings.default_ttl_seconds
    if raw < 1:
        raise HTTPException(status_code=400, detail="expires_in must be positive")
    if raw > settings.max_ttl_seconds:
        raise HTTPException(
            status_code=400,
            detail=f"expires_in exceeds maximum of {settings.max_ttl_seconds} seconds",
        )
    return raw


@router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    expires_in_q: int | None = Query(None, alias="expires_in"),
    one_shot_q: bool | None = Query(None, alias="one_shot"),
    filename_override_q: str | None = Query(None, alias="filename"),
    expires_in_f: int | None = Form(None, alias="expires_in"),
    one_shot_f: bool | None = Form(None, alias="one_shot"),
    filename_override_f: str | None = Form(None, alias="filename"),
    session: Session = Depends(get_session),
):
    """Receive a multipart upload, store it, return metadata."""
    ip = client_ip(request)

    # 1. Rate limit BEFORE anything else. Stricter bucket for cross-origin
    #    browser callers since they're more likely to be abusive scripts.
    foreign = is_foreign_origin(request)
    limit_name = "upload_per_ip_foreign" if foreign else "upload_per_ip"
    if not ratelimit.check(limit_name, ip):
        log_warning("upload_rate_limited", ip=ip, foreign=foreign)
        raise HTTPException(status_code=429, detail="too many uploads, slow down")

    # 2. Sanity-check Content-Length up front. If the client claims more
    #    than our limit, refuse before reading a single byte.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            declared = int(cl)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content-length")
        # Multipart envelope adds some overhead (~200 bytes for the boundary);
        # we add 1 KiB of slack so an honest client just under the limit
        # isn't rejected here. The actual payload is enforced in save_stream.
        if declared > settings.max_file_size_bytes + 1024:
            log_warning("upload_too_large_declared", ip=ip, declared=declared)
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds {settings.max_file_size_mb} MB limit",
            )

    store = DbStore(session)

    # 3. Resolve params (query takes precedence over form).
    ttl = _resolve_ttl(expires_in_q, expires_in_f)
    one_shot = one_shot_q if one_shot_q is not None else (one_shot_f or False)
    raw_filename = filename_override_q or filename_override_f or file.filename or ""

    # 4. Sanitize filename.
    try:
        filename = sanitize_filename(raw_filename)
    except ValueError as e:
        log_warning("upload_bad_filename", ip=ip, reason=str(e))
        raise HTTPException(status_code=400, detail=f"invalid filename: {e}")

    # 5. Block dangerous extensions.
    if has_blocked_extension(filename, settings.blocked_extensions_set):
        log_warning("upload_blocked_extension", ip=ip, filename=filename)
        raise HTTPException(status_code=415, detail="this file extension is not allowed")

    # 6. Allocate a fresh ID.
    file_id = None
    for _ in range(5):
        candidate = generate_id()
        if store.get(candidate) is None:
            file_id = candidate
            break
    if file_id is None:
        raise HTTPException(status_code=500, detail="could not allocate id")

    # 7. Stream to disk with size enforcement and SHA256 calculation.
    try:
        size, sha256_hex = await save_stream(
            settings.storage_dir,
            file_id,
            file.file,
            max_bytes=settings.max_file_size_bytes,
        )
    except FileTooLargeError:
        log_warning("upload_too_large_streamed", ip=ip, filename=filename)
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {settings.max_file_size_mb} MB limit",
        )

    # 8. Detect content-type from actual bytes.
    target_path = storage_path_for(settings.storage_dir, file_id)
    detected_type = magic.from_file(str(target_path), mime=True) or "application/octet-stream"

    # 9. Issue delete token; store only its hash.
    delete_token = generate_delete_token()
    delete_token_hash = hashlib.sha256(delete_token.encode("ascii")).hexdigest()

    # 10. Persist.
    created = now_utc()
    expires = created + timedelta(seconds=ttl)
    meta = FileMeta(
        id=file_id,
        filename=filename,
        size=size,
        sha256=sha256_hex,
        content_type=detected_type,
        created_at=created,
        expires_at=expires,
        one_shot=one_shot,
        delete_token_hash=delete_token_hash,
    )
    store.put(meta, ip_uploaded_from=ip)

    log_event(
        "upload_ok",
        ip=ip,
        file_id=file_id,
        size=size,
        content_type=detected_type,
        ttl=ttl,
        one_shot=one_shot,
        foreign=foreign,
    )

    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        status_code=201,
        content={
            "url": _public_url(request, file_id, filename),
            "id": file_id,
            "filename": filename,
            "size_bytes": size,
            "sha256": sha256_hex,
            "content_type": detected_type,
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "expires_in_seconds": ttl,
            "delete_url": f"{base}/f/{file_id}?token={delete_token}",
            "one_shot": one_shot,
        },
    )
