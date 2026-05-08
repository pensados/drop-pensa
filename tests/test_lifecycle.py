"""
Happy-path lifecycle tests: upload, fetch, head, info, delete, expire.
"""
import hashlib
from datetime import datetime, timezone


def test_healthz_starts_empty(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["files_count"] == 0
    assert body["storage_used_mb"] == 0


def test_upload_returns_full_payload(client, upload_one):
    content = b"sample content for sha test"
    payload = upload_one(content=content, filename="sample.txt")

    expected_sha = hashlib.sha256(content).hexdigest()
    assert payload["sha256"] == expected_sha
    assert payload["size_bytes"] == len(content)
    assert payload["filename"] == "sample.txt"
    assert payload["one_shot"] is False
    assert payload["expires_in_seconds"] == 3600
    assert payload["url"].endswith(f"/f/{payload['id']}/sample.txt")
    assert "delete_url" in payload
    assert "?token=" in payload["delete_url"]


def test_fetch_returns_exact_bytes(client, upload_one):
    content = bytes(range(256)) * 100  # 25 KiB of binary
    payload = upload_one(content=content, filename="binary.bin")

    resp = client.get(f"/f/{payload['id']}/{payload['filename']}")
    assert resp.status_code == 200
    assert resp.content == content
    assert hashlib.sha256(resp.content).hexdigest() == payload["sha256"]


def test_fetch_disposition_inline_by_default(client, upload_one):
    payload = upload_one()
    resp = client.get(f"/f/{payload['id']}/{payload['filename']}")
    assert resp.headers["content-disposition"].startswith("inline")


def test_fetch_disposition_attachment_with_download(client, upload_one):
    payload = upload_one()
    resp = client.get(
        f"/f/{payload['id']}/{payload['filename']}",
        params={"download": 1},
    )
    assert resp.headers["content-disposition"].startswith("attachment")


def test_head_mirrors_get_headers(client, upload_one):
    payload = upload_one()
    head = client.head(f"/f/{payload['id']}/{payload['filename']}")
    assert head.status_code == 200
    assert head.headers["content-length"] == str(payload["size_bytes"])
    assert "content-disposition" in head.headers


def test_head_with_download_query(client, upload_one):
    payload = upload_one()
    head = client.head(
        f"/f/{payload['id']}/{payload['filename']}",
        params={"download": 1},
    )
    assert head.headers["content-disposition"].startswith("attachment")


def test_info_returns_metadata(client, upload_one):
    payload = upload_one(content=b"foo", filename="foo.txt")
    info = client.get(f"/f/{payload['id']}/info")
    assert info.status_code == 200
    body = info.json()
    assert body["id"] == payload["id"]
    assert body["filename"] == "foo.txt"
    assert body["size_bytes"] == 3
    assert body["sha256"] == payload["sha256"]
    assert body["one_shot"] is False
    assert body["fetched_count"] == 0


def test_info_increments_after_fetch(client, upload_one):
    payload = upload_one()
    client.get(f"/f/{payload['id']}/{payload['filename']}")
    client.get(f"/f/{payload['id']}/{payload['filename']}")
    info = client.get(f"/f/{payload['id']}/info").json()
    assert info["fetched_count"] == 2


def test_info_404_for_unknown_id(client):
    resp = client.get("/f/AAAAAAAAAAAAA/info")
    assert resp.status_code == 404


# ---------- TTL / expiry ----------


def test_expires_in_param_respected(client, upload_one):
    payload = upload_one(expires_in=60)
    assert payload["expires_in_seconds"] == 60

    expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    # Expiry should be roughly 60s out, give 5s margin for slow CI
    delta = (expires - now).total_seconds()
    assert 55 <= delta <= 65


def test_expires_in_zero_rejected(client):
    resp = client.post(
        "/upload",
        params={"expires_in": 0},
        files={"file": ("x.txt", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_expires_in_above_max_rejected(client):
    resp = client.post(
        "/upload",
        params={"expires_in": 999_999_999},
        files={"file": ("x.txt", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_expired_file_returns_404(client, upload_one):
    """
    Manually expire a file by editing the DB row, then verify fetch is 404.

    Way faster than sleeping. We're testing the route's expiry check,
    not the wall clock.
    """
    payload = upload_one(expires_in=3600)
    file_id = payload["id"]

    # Reach into the DB and backdate expires_at.
    from drop_pensa.storage.db import session_scope
    from drop_pensa.models import files as files_t
    with session_scope() as sess:
        sess.execute(
            files_t.update()
            .where(files_t.c.id == file_id)
            .values(expires_at="2000-01-01T00:00:00+00:00")
        )

    fetch = client.get(f"/f/{file_id}/{payload['filename']}")
    assert fetch.status_code == 404


# ---------- one_shot ----------


def test_one_shot_consumes_after_first_fetch(client, upload_one):
    """A consumed one-shot returns 410 Gone (RFC 9110, issue #2)."""
    payload = upload_one(one_shot=True)
    assert payload["one_shot"] is True

    first = client.get(f"/f/{payload['id']}/{payload['filename']}")
    assert first.status_code == 200

    second = client.get(f"/f/{payload['id']}/{payload['filename']}")
    assert second.status_code == 410
    assert second.json()["detail"] == "consumed"


def test_one_shot_consumed_410_only_with_correct_filename(client, upload_one):
    """410 must require the right filename, otherwise 404 (no enumeration)."""
    payload = upload_one(one_shot=True)
    client.get(f"/f/{payload['id']}/{payload['filename']}")  # consume

    # Right id, wrong filename → 404, not 410
    resp = client.get(f"/f/{payload['id']}/wrong-name.txt")
    assert resp.status_code == 404


def test_one_shot_consumed_info_returns_410(client, upload_one):
    """/info also returns 410 on a consumed one-shot."""
    payload = upload_one(one_shot=True)
    client.get(f"/f/{payload['id']}/{payload['filename']}")  # consume

    info = client.get(f"/f/{payload['id']}/info")
    assert info.status_code == 410


def test_normal_expired_returns_404_not_410(client, upload_one):
    """A non-one-shot file that expired returns 404, not 410."""
    from drop_pensa.storage.db import session_scope
    from drop_pensa.models import files as files_t

    payload = upload_one(one_shot=False)
    with session_scope() as sess:
        sess.execute(
            files_t.update()
            .where(files_t.c.id == payload["id"])
            .values(expires_at="2000-01-01T00:00:00+00:00")
        )

    resp = client.get(f"/f/{payload['id']}/{payload['filename']}")
    assert resp.status_code == 404


def test_one_shot_no_store_cache_header(client, upload_one):
    payload = upload_one(one_shot=True)
    resp = client.get(f"/f/{payload['id']}/{payload['filename']}")
    assert resp.headers["cache-control"] == "no-store"


# ---------- delete ----------


def test_delete_with_valid_token(client, upload_one):
    payload = upload_one()
    file_id = payload["id"]
    token = payload["delete_url"].split("token=")[1]

    resp = client.delete(f"/f/{file_id}", params={"token": token})
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "already": False, "id": file_id}

    # Subsequent fetch is 404
    fetch = client.get(f"/f/{file_id}/{payload['filename']}")
    assert fetch.status_code == 404


# ---------- cleanup worker ----------


def test_cleanup_removes_expired_from_disk(client, upload_one, tmp_path):
    """The cleanup function should soft-delete expired rows and unlink files."""
    from drop_pensa.cleanup import _cleanup_once
    from drop_pensa.config import settings
    from drop_pensa.storage.db import session_scope
    from drop_pensa.storage.filesystem import storage_path_for
    from drop_pensa.models import files as files_t

    payload = upload_one(content=b"to be expired")
    file_id = payload["id"]

    # File exists on disk
    on_disk = storage_path_for(settings.storage_dir, file_id)
    assert on_disk.exists()

    # Backdate expiry
    with session_scope() as sess:
        sess.execute(
            files_t.update()
            .where(files_t.c.id == file_id)
            .values(expires_at="2000-01-01T00:00:00+00:00")
        )

    stats = _cleanup_once()
    assert stats["expired"] >= 1
    assert not on_disk.exists()


# ---------- robots / favicon / static ----------


def test_robots_disallows_all(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert "Disallow: /" in resp.text


def test_index_served_with_no_store(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert "drop.pensa.ar" in resp.text


def test_favicon_responds_to_head(client):
    resp = client.head("/favicon.svg")
    assert resp.status_code == 200


# ---------- doc pages ----------


def test_about_page(client):
    resp = client.get("/about")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "About" in resp.text
    assert "drop.pensa.ar" in resp.text


def test_api_page_has_curl_examples(client):
    resp = client.get("/api")
    assert resp.status_code == 200
    assert "curl" in resp.text
    assert "POST /upload" in resp.text
    assert "DELETE /f/{id}" in resp.text


def test_privacy_page(client):
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert "Privacy" in resp.text


def test_terms_page(client):
    resp = client.get("/terms")
    assert resp.status_code == 200
    assert "Terms" in resp.text


def test_doc_pages_have_security_headers(client):
    """Strict CSP and friends apply to docs the same as the index."""
    resp = client.get("/about")
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"


def test_doc_pages_no_store(client):
    """Docs are no-store so updates ship without cache stalls."""
    resp = client.get("/api")
    assert resp.headers["cache-control"] == "no-store"


def test_doc_pages_share_layout(client):
    """All pages share the same brand and footer (via base.html)."""
    for path in ("/about", "/api", "/privacy", "/terms"):
        resp = client.get(path)
        assert resp.status_code == 200
        # Header
        assert "drop.pensa.ar" in resp.text
        # Footer
        assert 'href="/privacy"' in resp.text
        assert 'href="/terms"' in resp.text
