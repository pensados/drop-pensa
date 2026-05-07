"""
SQLAlchemy core schema for drop-pensa.

We use the imperative (Core) style rather than the ORM Declarative style.
The data model is small and flat — adding ORM machinery would be more
ceremony than benefit. This also makes the eventual port to Postgres
trivial: change the URL, run migrations.

All timestamps are stored as ISO 8601 strings in UTC. SQLite has no
native timezone-aware datetime type, and forcing TIMESTAMP gives you
naive datetimes that round-trip badly. ISO 8601 strings are unambiguous
and compare correctly lexicographically.
"""
from sqlalchemy import (
    Boolean,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)


metadata = MetaData()


files = Table(
    "files",
    metadata,
    # 13-char URL-safe id (see security.py). Primary key.
    Column("id", String(32), primary_key=True),

    # User-supplied (sanitized) filename.
    Column("filename", String(255), nullable=False),

    # File size in bytes.
    Column("size", Integer, nullable=False),

    # Hex sha256 of the file contents. 64 hex chars.
    Column("sha256", String(64), nullable=False),

    # Detected content-type from libmagic.
    Column("content_type", String(255), nullable=False),

    # Source IP (from X-Forwarded-For if behind a proxy). 45 chars covers IPv6.
    # Kept for abuse investigation, not exposed via API.
    Column("ip_uploaded_from", String(45), nullable=True),

    # ISO 8601 UTC timestamps.
    Column("created_at", Text, nullable=False),
    Column("expires_at", Text, nullable=False),

    # Soft delete: when set, the row is logically gone but kept until the
    # cleanup worker physically removes it. NULL = active.
    Column("deleted_at", Text, nullable=True),

    # one_shot: delete after first successful fetch.
    Column("one_shot", Boolean, nullable=False, default=False),

    # Track fetch activity for rate-limit-per-file in a future step.
    Column("fetched_count", Integer, nullable=False, default=0),
    Column("last_fetched_at", Text, nullable=True),

    # SHA256 of the delete token. The raw token is shown ONCE on upload
    # and never stored. Comparing hashes makes leaked DB dumps useless
    # for forging delete requests.
    Column("delete_token_hash", String(64), nullable=False),
)


# Hot-path index: cleanup worker scans for expired non-deleted rows.
Index(
    "ix_files_expires_active",
    files.c.expires_at,
    files.c.deleted_at,
)
