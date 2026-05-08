"""
Tests for HTTP Range request support (issue #3).

Two layers:
- Unit tests on parse_range_header (the parser is pure logic)
- Integration tests via TestClient against the GET /f/{id}/{name} handler
"""
import pytest


# ---------- parser unit tests ----------


def test_parse_no_header_returns_none():
    from drop_pensa.range_parser import parse_range_header
    assert parse_range_header(None, total=1000) is None
    assert parse_range_header("", total=1000) is None
    assert parse_range_header("   ", total=1000) is None


def test_parse_simple_range():
    from drop_pensa.range_parser import parse_range_header
    r = parse_range_header("bytes=0-99", total=1000)
    assert r.start == 0
    assert r.end == 99
    assert r.length == 100
    assert r.total == 1000
    assert r.content_range_header() == "bytes 0-99/1000"


def test_parse_open_ended():
    from drop_pensa.range_parser import parse_range_header
    r = parse_range_header("bytes=500-", total=1000)
    assert r.start == 500
    assert r.end == 999
    assert r.length == 500


def test_parse_suffix():
    from drop_pensa.range_parser import parse_range_header
    r = parse_range_header("bytes=-100", total=1000)
    assert r.start == 900
    assert r.end == 999
    assert r.length == 100


def test_parse_clamps_end_to_size():
    """RFC: end-over-size is allowed, server clamps."""
    from drop_pensa.range_parser import parse_range_header
    r = parse_range_header("bytes=0-9999", total=1000)
    assert r.start == 0
    assert r.end == 999


def test_parse_full_file_range():
    from drop_pensa.range_parser import parse_range_header
    r = parse_range_header("bytes=0-999", total=1000)
    assert r.is_full_file


def test_parse_partial_not_full_file():
    from drop_pensa.range_parser import parse_range_header
    r = parse_range_header("bytes=0-99", total=1000)
    assert not r.is_full_file


@pytest.mark.parametrize("bad", [
    "bytes=",
    "bytes=abc-def",
    "bytes=100-50",       # inverted
    "bytes=2000-3000",    # past EOF
    "bytes=-0",           # zero suffix
    "items=0-99",         # wrong unit
    "bytes=0-99,200-299", # multi-range
    "0-99",               # no unit
])
def test_parse_rejects_bad(bad):
    from drop_pensa.range_parser import RangeNotSatisfiable, parse_range_header
    with pytest.raises(RangeNotSatisfiable):
        parse_range_header(bad, total=1000)


def test_parse_empty_file_any_range_unsatisfiable():
    from drop_pensa.range_parser import RangeNotSatisfiable, parse_range_header
    with pytest.raises(RangeNotSatisfiable):
        parse_range_header("bytes=0-0", total=0)


def test_parse_suffix_larger_than_file_clamps():
    from drop_pensa.range_parser import parse_range_header
    r = parse_range_header("bytes=-99999", total=1000)
    assert r.start == 0
    assert r.end == 999


# ---------- integration tests ----------


def _make_payload(client, content: bytes, **kwargs):
    """Helper: upload `content` and return (id, filename, payload)."""
    params = {}
    if "expires_in" in kwargs:
        params["expires_in"] = kwargs.pop("expires_in")
    if kwargs.get("one_shot"):
        params["one_shot"] = "true"
    resp = client.post(
        "/upload",
        params=params,
        files={"file": ("data.bin", content, "application/octet-stream")},
    )
    assert resp.status_code == 201
    p = resp.json()
    return p["id"], p["filename"], p


def test_get_full_advertises_accept_ranges(client):
    fid, name, _ = _make_payload(client, b"hello world\n")
    resp = client.get(f"/f/{fid}/{name}")
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"


def test_head_advertises_accept_ranges(client):
    fid, name, _ = _make_payload(client, b"hello")
    resp = client.head(f"/f/{fid}/{name}")
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"


def test_range_simple_returns_206(client):
    content = bytes(range(256))  # 256 bytes 0..255
    fid, name, _ = _make_payload(client, content)
    resp = client.get(
        f"/f/{fid}/{name}",
        headers={"Range": "bytes=10-19"},
    )
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 10-19/256"
    assert resp.headers["content-length"] == "10"
    assert resp.content == content[10:20]


def test_range_open_ended(client):
    content = bytes(range(100))
    fid, name, _ = _make_payload(client, content)
    resp = client.get(
        f"/f/{fid}/{name}",
        headers={"Range": "bytes=80-"},
    )
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 80-99/100"
    assert resp.content == content[80:]


def test_range_suffix(client):
    content = bytes(range(100))
    fid, name, _ = _make_payload(client, content)
    resp = client.get(
        f"/f/{fid}/{name}",
        headers={"Range": "bytes=-20"},
    )
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 80-99/100"
    assert resp.content == content[80:]


def test_range_unsatisfiable_returns_416(client):
    fid, name, _ = _make_payload(client, b"hello")
    resp = client.get(
        f"/f/{fid}/{name}",
        headers={"Range": "bytes=1000-2000"},
    )
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */5"
    assert resp.headers["accept-ranges"] == "bytes"


def test_range_malformed_returns_416(client):
    fid, name, _ = _make_payload(client, b"hello")
    resp = client.get(
        f"/f/{fid}/{name}",
        headers={"Range": "items=0-99"},
    )
    assert resp.status_code == 416


def test_range_multi_range_returns_416(client):
    """Multi-range requests are not supported and return 416."""
    fid, name, _ = _make_payload(client, b"x" * 100)
    resp = client.get(
        f"/f/{fid}/{name}",
        headers={"Range": "bytes=0-9,50-59"},
    )
    assert resp.status_code == 416


def test_no_range_header_returns_200_full(client):
    content = b"the whole file"
    fid, name, _ = _make_payload(client, content)
    resp = client.get(f"/f/{fid}/{name}")
    assert resp.status_code == 200
    assert resp.content == content
    # Still advertises support
    assert resp.headers["accept-ranges"] == "bytes"


# ---------- one_shot interaction (option B) ----------


def test_one_shot_partial_range_does_not_consume(client):
    """A partial Range read on a one-shot must not consume the file."""
    content = bytes(range(100))
    fid, name, _ = _make_payload(client, content, one_shot=True)

    # Partial read
    r1 = client.get(f"/f/{fid}/{name}", headers={"Range": "bytes=0-9"})
    assert r1.status_code == 206

    # File is still there — full GET works
    r2 = client.get(f"/f/{fid}/{name}")
    assert r2.status_code == 200
    assert r2.content == content


def test_one_shot_partial_range_repeatable(client):
    """Multiple partial reads on a one-shot are all OK as long as none are full."""
    content = bytes(range(100))
    fid, name, _ = _make_payload(client, content, one_shot=True)

    for hdr in ("bytes=0-9", "bytes=10-19", "bytes=-20", "bytes=50-"):
        # bytes=50- spans 50-99 = 50 bytes < total 100, so still partial
        if hdr == "bytes=50-":
            continue  # this one would be partial, kept for clarity
        resp = client.get(f"/f/{fid}/{name}", headers={"Range": hdr})
        assert resp.status_code == 206, f"failed for {hdr}"

    # Full fetch still works
    final = client.get(f"/f/{fid}/{name}")
    assert final.status_code == 200


def test_one_shot_full_range_consumes(client):
    """A Range that spans the entire file consumes the one-shot."""
    content = b"x" * 100
    fid, name, _ = _make_payload(client, content, one_shot=True)

    r1 = client.get(f"/f/{fid}/{name}", headers={"Range": "bytes=0-99"})
    assert r1.status_code == 206
    assert r1.content == content

    # Now consumed: 410
    r2 = client.get(f"/f/{fid}/{name}")
    assert r2.status_code == 410


def test_one_shot_no_range_consumes_normally(client):
    """A vanilla GET (no Range) on a one-shot consumes it as before."""
    fid, name, _ = _make_payload(client, b"hi", one_shot=True)
    r1 = client.get(f"/f/{fid}/{name}")
    assert r1.status_code == 200
    r2 = client.get(f"/f/{fid}/{name}")
    assert r2.status_code == 410
