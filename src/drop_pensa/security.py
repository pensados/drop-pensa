"""
Security primitives: ID generation, token generation, filename sanitization.

These are the trust-critical pieces of the service. Keep them small,
readable, and well-tested.
"""
import re
import secrets
import unicodedata
from pathlib import PurePosixPath


# ID_LENGTH yields ~78 bits of entropy (well above the 72-bit floor in the spec).
# token_urlsafe encodes 3 bytes per 4 chars, so length 13 gives ~78 bits.
ID_LENGTH = 13
DELETE_TOKEN_LENGTH = 22  # ~132 bits, generous since this is a capability token

MAX_FILENAME_LENGTH = 255

# Characters that we will NEVER tolerate in a sanitized filename.
# Path separators, null bytes, and control characters are all rejected.
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")


def generate_id() -> str:
    """Generate a URL-safe random ID for a stored file."""
    return secrets.token_urlsafe(ID_LENGTH)[:ID_LENGTH]


def generate_delete_token() -> str:
    """Generate a URL-safe random delete token (capability)."""
    return secrets.token_urlsafe(DELETE_TOKEN_LENGTH)[:DELETE_TOKEN_LENGTH]


def shard_path(file_id: str) -> str:
    """
    Return the sharded subdirectory for a given file id.

    We only shard one level (first 2 chars). With ~14k buckets and an
    expected steady state of 10s-1000s of files, this keeps directory
    listings cheap without creating millions of empty directories.
    """
    if len(file_id) < 2:
        raise ValueError("file_id too short to shard")
    return file_id[:2]


def sanitize_filename(raw: str) -> str:
    """
    Sanitize a user-supplied filename.

    Rules:
    - Strip any directory components (defense against `../etc/passwd` style
      attacks even if the OS would catch them later).
    - Reject path separators, null bytes, and control characters.
    - Reject percent-encoded sequences that decode to forbidden chars
      (multipart parsers may percent-encode \x00 and other bytes).
    - Normalize unicode to NFC to avoid encoding-based bypasses.
    - Cap length at MAX_FILENAME_LENGTH while preserving the extension.
    - Reject empty results.

    Raises ValueError on anything that cannot be made safe.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("filename is empty")

    # Decode percent-encoded sequences so a forbidden char hidden as %00
    # is caught by the same check as a literal one. Multipart parsers
    # (e.g. Starlette's) may produce %xx sequences from non-ASCII or
    # control bytes in the original Content-Disposition header.
    from urllib.parse import unquote
    decoded = unquote(raw)
    if decoded != raw and _FORBIDDEN_CHARS.search(decoded):
        raise ValueError("filename contains forbidden characters (encoded)")

    # Reject before stripping — an attacker putting `/` in the name
    # is signaling intent and we don't want to silently "fix" it.
    if _FORBIDDEN_CHARS.search(raw):
        raise ValueError("filename contains forbidden characters")

    # PurePosixPath().name strips any directory component. After the
    # forbidden-char check above this is belt-and-suspenders.
    name = PurePosixPath(raw).name
    if not name:
        raise ValueError("filename is empty after path stripping")

    # Reject "." and ".." explicitly. PurePosixPath would let them through.
    if name in (".", ".."):
        raise ValueError("filename is reserved")

    # NFC normalization keeps visually-identical strings byte-identical,
    # closing off homoglyph-style filename collisions.
    name = unicodedata.normalize("NFC", name)

    # Truncate while preserving extension, if any.
    if len(name) > MAX_FILENAME_LENGTH:
        # rsplit on the last dot to find the extension; leave room for it.
        if "." in name[1:]:  # ignore leading-dot files like ".env"
            stem, _, ext = name.rpartition(".")
            ext = ext[:32]  # absurdly long extensions are themselves fishy
            keep = MAX_FILENAME_LENGTH - len(ext) - 1
            name = stem[:keep] + "." + ext
        else:
            name = name[:MAX_FILENAME_LENGTH]

    return name


def has_blocked_extension(filename: str, blocked: set[str]) -> bool:
    """Check if the filename's extension is in the blocked set."""
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in blocked
