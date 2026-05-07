"""
DELETE /f/<id>?token=<delete_token> — delete a stored file.

The delete capability is the raw token returned at upload time. We never
stored the token; we stored its sha256. To authorize a delete we hash
the presented token and compare it in constant time against the stored
hash.

Idempotent by design: deleting an already-gone file returns 200 with
{deleted: true, already: true}. This makes retries safe and matches the
behavior the spec asked for.
"""
import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from drop_pensa import ratelimit
from drop_pensa.clientinfo import client_ip
from drop_pensa.config import settings
from drop_pensa.logging_setup import log_event, log_warning
from drop_pensa.storage.db import DbStore, get_session
from drop_pensa.storage.filesystem import delete_file


router = APIRouter()


def _hash_token(token: str) -> str:
    """sha256 of the presented token, hex-encoded — same shape as stored."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@router.delete("/f/{file_id}")
async def delete(
    request: Request,
    file_id: str,
    token: str = Query(..., min_length=1),
    session: Session = Depends(get_session),
):
    """Delete a file by id, authorized by the delete token."""
    ip = client_ip(request)

    # Rate limit before doing any DB work.
    if not ratelimit.check("delete_per_ip", ip):
        log_warning("delete_rate_limited", ip=ip, file_id=file_id)
        raise HTTPException(status_code=429, detail="too many delete requests")

    store = DbStore(session)

    # We need the raw row (including delete_token_hash and deleted_at)
    # to validate even when the row is already soft-deleted. The standard
    # `store.get` filters out soft-deletes, so we fetch the row directly.
    raw = store.get_raw(file_id)

    if raw is None:
        # Truly unknown id. We don't reveal whether the id ever existed —
        # both "never existed" and "wrong token" return the same status,
        # so an attacker can't enumerate ids by trying tokens.
        log_warning("delete_not_found", ip=ip, file_id=file_id)
        raise HTTPException(status_code=403, detail="forbidden")

    presented = _hash_token(token)
    expected = raw["delete_token_hash"]

    # Constant-time comparison so an attacker can't time-side-channel
    # their way to the right hash. compare_digest works on equal-length
    # strings; both are 64-char hex by construction.
    if not hmac.compare_digest(presented, expected):
        log_warning("delete_bad_token", ip=ip, file_id=file_id)
        raise HTTPException(status_code=403, detail="forbidden")

    # Token is valid. If the file was already (soft-)deleted, return
    # success with already=true. This makes the operation idempotent
    # without lying about state.
    if raw["deleted_at"] is not None:
        log_event("delete_idempotent", ip=ip, file_id=file_id)
        return JSONResponse(
            status_code=200,
            content={"deleted": True, "already": True, "id": file_id},
        )

    # Live file: soft-delete in DB, then remove from disk. Disk removal
    # outside the transaction so a slow filesystem doesn't hold a write
    # lock. If disk removal fails the row stays soft-deleted and the
    # cleanup worker will retry on its next pass.
    store.soft_delete(file_id)
    try:
        delete_file(settings.storage_dir, file_id)
    except Exception as e:
        log_warning("delete_disk_failed", ip=ip, file_id=file_id, error=str(e))
        # Don't fail the request — the row is gone from the user's
        # perspective and cleanup will sweep the file. Surfacing this
        # error would just confuse the caller.

    log_event("delete_ok", ip=ip, file_id=file_id)
    return JSONResponse(
        status_code=200,
        content={"deleted": True, "already": False, "id": file_id},
    )
