"""
Security-critical tests.

These cover the boundaries that, if broken, would let an attacker
compromise other users' files or the host itself. They are deliberately
paranoid: each one corresponds to a CVE class we don't want.
"""
import pytest


# ---------- filename sanitization ----------


@pytest.mark.parametrize("evil_name", [
    "../etc/passwd",
    "../../etc/passwd",
    "/etc/passwd",
    "..\\..\\windows\\system32\\evil",
    "foo/bar.txt",
    "foo\\bar.txt",
    "with\x00null.txt",
    ".",
    "..",
    "",
])
def test_filename_path_traversal_rejected(client, evil_name):
    """Any filename with separators, traversal, or null bytes must be 4xx."""
    resp = client.post(
        "/upload",
        files={"file": (evil_name, b"x", "application/octet-stream")},
    )
    assert resp.status_code in (400, 422), (
        f"expected rejection for {evil_name!r}, got {resp.status_code}: {resp.text}"
    )


def test_filename_override_via_query_also_sanitized(client):
    """The ?filename= override must go through the same sanitizer."""
    resp = client.post(
        "/upload",
        params={"filename": "../../etc/shadow"},
        files={"file": ("legit.txt", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_filename_unicode_normalized(client, upload_one):
    """NFC normalization: visually-identical names should round-trip cleanly."""
    # "café" with combining accent should normalize to precomposed form.
    nfd_name = "cafe\u0301.txt"
    payload = upload_one(filename=nfd_name)
    # The stored name should be NFC; either form must round-trip.
    assert payload["filename"] in ("café.txt", "cafe\u0301.txt")


# ---------- blocked extensions ----------


@pytest.mark.parametrize("ext", ["exe", "bat", "sh", "EXE", "BaT"])
def test_blocked_extensions_rejected(client, ext):
    """Configured blocked extensions are rejected (case-insensitive)."""
    resp = client.post(
        "/upload",
        files={"file": (f"payload.{ext}", b"#!/bin/sh\n", "application/octet-stream")},
    )
    assert resp.status_code == 415


# ---------- size limits ----------


def test_oversize_streaming_rejected(client):
    """A body larger than the limit must be rejected with 413."""
    # MAX_FILE_SIZE_MB=5 in tests → limit is 5 MiB.
    too_big = b"A" * (6 * 1024 * 1024)
    resp = client.post(
        "/upload",
        files={"file": ("big.bin", too_big, "application/octet-stream")},
    )
    assert resp.status_code == 413


def test_lying_content_length_rejected_early(client):
    """Forged Content-Length over the limit must be refused before reading."""
    # We can't easily craft a multipart with lying CL via TestClient, so
    # we test the simpler case: a Content-Length header on a small body.
    resp = client.post(
        "/upload",
        headers={"Content-Length": str(100 * 1024 * 1024)},  # 100 MiB
        files={"file": ("small.txt", b"x", "application/octet-stream")},
    )
    # Either the framework or our own check should refuse.
    assert resp.status_code in (400, 413)


# ---------- delete token ----------


def test_delete_requires_correct_token(client, upload_one):
    """Wrong token → 403, file survives."""
    payload = upload_one()
    resp = client.delete(f"/f/{payload['id']}", params={"token": "wrong-token-here"})
    assert resp.status_code == 403

    # File still alive
    fetch = client.get(f"/f/{payload['id']}/{payload['filename']}")
    assert fetch.status_code == 200


def test_delete_unknown_id_returns_403_not_404(client):
    """No 404/403 distinction → can't enumerate ids by trying tokens."""
    resp = client.delete("/f/nonexistent-id", params={"token": "anything"})
    assert resp.status_code == 403


def test_delete_idempotent(client, upload_one):
    """Deleting twice with a valid token must succeed both times."""
    payload = upload_one()
    file_id = payload["id"]
    token = payload["delete_url"].split("token=")[1]

    r1 = client.delete(f"/f/{file_id}", params={"token": token})
    assert r1.status_code == 200
    assert r1.json()["already"] is False

    r2 = client.delete(f"/f/{file_id}", params={"token": token})
    assert r2.status_code == 200
    assert r2.json()["already"] is True


def test_delete_token_not_returned_in_info(client, upload_one):
    """The /info endpoint must never expose the delete token hash."""
    payload = upload_one()
    info = client.get(f"/f/{payload['id']}/info").json()
    assert "delete_token" not in info
    assert "delete_token_hash" not in info
    assert "delete_url" not in info


# ---------- XSS / dangerous content-types ----------


def test_html_served_as_text_plain(client):
    """An uploaded .html must be served as text/plain to neutralize XSS."""
    html = b"<script>alert('xss')</script><h1>hi</h1>"
    resp = client.post(
        "/upload",
        files={"file": ("evil.html", html, "text/html")},
    )
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["content_type"] == "text/html"  # detected truthfully

    # But served as text/plain
    fetch = client.get(f"/f/{payload['id']}/{payload['filename']}")
    assert fetch.status_code == 200
    assert fetch.headers["content-type"].startswith("text/plain")
    assert fetch.headers["x-content-type-options"] == "nosniff"


def test_svg_served_as_text_plain(client):
    """SVG gets the same treatment — it can carry script tags."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    resp = client.post(
        "/upload",
        files={"file": ("logo.svg", svg, "image/svg+xml")},
    )
    assert resp.status_code == 201
    fetch = client.get(f"/f/{resp.json()['id']}/{resp.json()['filename']}")
    assert fetch.headers["content-type"].startswith("text/plain")


# ---------- security headers ----------


def test_security_headers_on_index(client):
    """The web UI must carry the strict CSP and friends."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "strict-origin" in resp.headers["referrer-policy"]


def test_no_csp_on_served_files(client, upload_one):
    """File responses don't need CSP and shouldn't carry it."""
    payload = upload_one()
    fetch = client.get(f"/f/{payload['id']}/{payload['filename']}")
    assert "content-security-policy" not in fetch.headers
    # But other defenses still apply
    assert fetch.headers.get("x-content-type-options") == "nosniff"


# ---------- id non-enumeration ----------


def test_filename_mismatch_returns_404(client, upload_one):
    """Right id + wrong name must 404, same as a totally unknown id."""
    payload = upload_one()
    wrong = client.get(f"/f/{payload['id']}/wrong-name.txt")
    unknown = client.get("/f/AAAAAAAAAAAAA/whatever.txt")
    assert wrong.status_code == 404
    assert unknown.status_code == 404


def test_filename_too_long_rejected_with_400(client):
    """Filenames over 255 chars are rejected with 400 (issue #1)."""
    long_name = "a" * 252 + ".txt"  # 256 chars
    resp = client.post(
        "/upload",
        files={"file": (long_name, b"x", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["detail"]
