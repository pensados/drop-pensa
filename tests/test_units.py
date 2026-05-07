"""
Unit tests for pure-logic modules: security primitives and rate limiter.
These don't need the FastAPI app, just import and call.
"""
import pytest


# ---------- security.py ----------


def test_generate_id_unique_and_long_enough():
    from drop_pensa.security import ID_LENGTH, generate_id
    ids = {generate_id() for _ in range(1000)}
    assert len(ids) == 1000  # collision-free at this scale
    assert all(len(i) == ID_LENGTH for i in ids)


def test_generate_delete_token_is_long():
    from drop_pensa.security import DELETE_TOKEN_LENGTH, generate_delete_token
    t = generate_delete_token()
    assert len(t) == DELETE_TOKEN_LENGTH
    # ≥22 chars URL-safe → ≥132 bits of entropy.
    assert DELETE_TOKEN_LENGTH >= 22


def test_shard_path_uses_first_two_chars():
    from drop_pensa.security import shard_path
    assert shard_path("abcdefghij") == "ab"
    assert shard_path("XYZ12345678") == "XY"


@pytest.mark.parametrize("name,expected", [
    ("hello.txt", "hello.txt"),
    ("UPPER.TXT", "UPPER.TXT"),
    ("with spaces.txt", "with spaces.txt"),
    (".env", ".env"),
])
def test_sanitize_filename_accepts_legit(name, expected):
    from drop_pensa.security import sanitize_filename
    assert sanitize_filename(name) == expected


@pytest.mark.parametrize("evil", [
    "../escape",
    "/abs",
    "back\\slash",
    "with\x00null",
    "",
    ".",
    "..",
])
def test_sanitize_filename_rejects(evil):
    from drop_pensa.security import sanitize_filename
    with pytest.raises(ValueError):
        sanitize_filename(evil)


def test_sanitize_filename_truncates_long_names():
    from drop_pensa.security import MAX_FILENAME_LENGTH, sanitize_filename
    # Long stem with a normal extension — extension should survive.
    name = "a" * 500 + ".txt"
    out = sanitize_filename(name)
    assert len(out) <= MAX_FILENAME_LENGTH
    assert out.endswith(".txt")


def test_has_blocked_extension_case_insensitive():
    from drop_pensa.security import has_blocked_extension
    blocked = {"exe", "bat"}
    assert has_blocked_extension("foo.EXE", blocked)
    assert has_blocked_extension("foo.bat", blocked)
    assert not has_blocked_extension("foo.txt", blocked)
    assert not has_blocked_extension("noextension", blocked)


# ---------- ratelimit.py ----------


def test_memory_backend_basic_window():
    from drop_pensa.ratelimit import _MemoryBackend, Limit
    b = _MemoryBackend()
    lim = Limit("test", max_events=3, window_seconds=60)
    assert b.check("k", lim) is True
    assert b.check("k", lim) is True
    assert b.check("k", lim) is True
    assert b.check("k", lim) is False  # 4th over the limit


def test_memory_backend_separates_keys():
    from drop_pensa.ratelimit import _MemoryBackend, Limit
    b = _MemoryBackend()
    lim = Limit("test", max_events=1, window_seconds=60)
    assert b.check("a", lim) is True
    assert b.check("b", lim) is True   # different key, not blocked
    assert b.check("a", lim) is False  # same key, now blocked


def test_ratelimit_module_check_no_init_returns_true():
    """Before init() is called the check should fail-open, not error."""
    # Force re-import so the global _backend is None.
    import importlib
    import drop_pensa.ratelimit as rl
    importlib.reload(rl)
    assert rl.check("upload_per_ip", "1.2.3.4") is True
