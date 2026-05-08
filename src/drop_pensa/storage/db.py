"""
Database access layer.

We expose a small, hand-written API rather than letting routes touch
SQLAlchemy directly. This keeps the SQL in one auditable place and lets
us swap implementations (in-memory for tests, Postgres later) without
ripping up the routes.

The session is sync. FastAPI's `Depends` hands one out per request
and closes it on teardown. SQLite is fast enough for our workload that
async DB is over-engineering at this stage.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from drop_pensa.config import settings
from drop_pensa.models import files, metadata
from drop_pensa.storage.memstore import FileMeta


# ---------- engine / session machinery ----------


def _make_engine(url: str) -> Engine:
    """
    Build a SQLAlchemy engine. SQLite needs a couple of tweaks:
    - check_same_thread=False because FastAPI may pass sessions across
      threads when running sync work in a threadpool.
    - WAL mode would be nice but we set it via PRAGMA in the migration.
    """
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    return create_engine(url, connect_args=connect_args, future=True)


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def init_engine(url: str | None = None) -> Engine:
    """Initialize the global engine. Idempotent."""
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine
    _engine = _make_engine(url or settings.db_url)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def ensure_schema() -> None:
    """
    Create tables if they don't exist. For prod we use Alembic, but this
    lets the dev container come up cleanly on first run and lets tests
    spin up an in-memory DB in milliseconds.
    """
    if _engine is None:
        init_engine()
    metadata.create_all(_engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for a transactional session."""
    if _SessionLocal is None:
        init_engine()
    sess = _SessionLocal()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a request-scoped session."""
    with session_scope() as sess:
        yield sess


# ---------- helpers: row <-> FileMeta ----------


def _row_to_meta(row) -> FileMeta:
    """Map a SQLAlchemy Row to a FileMeta dataclass."""
    return FileMeta(
        id=row.id,
        filename=row.filename,
        size=row.size,
        sha256=row.sha256,
        content_type=row.content_type,
        created_at=_parse_iso(row.created_at),
        expires_at=_parse_iso(row.expires_at),
        one_shot=bool(row.one_shot),
        delete_token_hash=row.delete_token_hash,
        fetched_count=row.fetched_count,
    )


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 UTC timestamp back to a tz-aware datetime."""
    # We always write with isoformat() and a 'Z' or '+00:00' suffix, so
    # fromisoformat handles it on Python 3.11+.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _to_iso(dt: datetime) -> str:
    """Serialize a datetime as ISO 8601 UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------- store API (matches MemStore shape) ----------


class DbStore:
    """Database-backed metadata store."""

    def __init__(self, session: Session):
        self.s = session

    def put(self, meta: FileMeta, ip_uploaded_from: str | None = None) -> None:
        self.s.execute(
            files.insert().values(
                id=meta.id,
                filename=meta.filename,
                size=meta.size,
                sha256=meta.sha256,
                content_type=meta.content_type,
                ip_uploaded_from=ip_uploaded_from,
                created_at=_to_iso(meta.created_at),
                expires_at=_to_iso(meta.expires_at),
                deleted_at=None,
                one_shot=meta.one_shot,
                fetched_count=meta.fetched_count,
                last_fetched_at=None,
                delete_token_hash=meta.delete_token_hash,
            )
        )

    def get(self, file_id: str) -> FileMeta | None:
        """Return the file metadata, or None if missing or soft-deleted."""
        stmt = select(files).where(
            files.c.id == file_id,
            files.c.deleted_at.is_(None),
        )
        row = self.s.execute(stmt).first()
        return _row_to_meta(row) if row else None

    def get_raw(self, file_id: str) -> dict | None:
        """
        Return the raw DB row including soft-delete state and the delete
        token hash. Used by:
        - DELETE endpoint, which validates the token even on already-deleted
          rows so deletes stay idempotent.
        - GET endpoint, which distinguishes "consumed one-shot" (410) from
          "unknown id" (404).
        """
        stmt = select(files).where(files.c.id == file_id)
        row = self.s.execute(stmt).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "filename": row.filename,
            "delete_token_hash": row.delete_token_hash,
            "deleted_at": row.deleted_at,
            "expires_at": row.expires_at,
            "one_shot": bool(row.one_shot),
        }

    def mark_fetched(self, file_id: str) -> None:
        """Increment fetch counter and update last_fetched_at."""
        self.s.execute(
            files.update()
            .where(files.c.id == file_id)
            .values(
                fetched_count=files.c.fetched_count + 1,
                last_fetched_at=_to_iso(datetime.now(timezone.utc)),
            )
        )
        self.s.commit()

    def soft_delete(self, file_id: str) -> bool:
        """Mark a row as deleted. Returns True if a row was affected."""
        result = self.s.execute(
            files.update()
            .where(files.c.id == file_id, files.c.deleted_at.is_(None))
            .values(deleted_at=_to_iso(datetime.now(timezone.utc)))
        )
        self.s.commit()
        return result.rowcount > 0

    def count(self) -> int:
        """Count active (non-deleted, non-expired) files."""
        from sqlalchemy import func
        now_iso = _to_iso(datetime.now(timezone.utc))
        stmt = select(func.count()).select_from(files).where(
            files.c.deleted_at.is_(None),
            files.c.expires_at > now_iso,
        )
        return self.s.execute(stmt).scalar() or 0

    def total_size(self) -> int:
        """Total bytes of active files."""
        from sqlalchemy import func
        now_iso = _to_iso(datetime.now(timezone.utc))
        stmt = select(func.coalesce(func.sum(files.c.size), 0)).where(
            files.c.deleted_at.is_(None),
            files.c.expires_at > now_iso,
        )
        return int(self.s.execute(stmt).scalar() or 0)

    def expired_ids(self, limit: int = 1000) -> list[str]:
        """Return ids of rows past their expiry that aren't soft-deleted yet."""
        now_iso = _to_iso(datetime.now(timezone.utc))
        stmt = (
            select(files.c.id)
            .where(
                files.c.deleted_at.is_(None),
                files.c.expires_at <= now_iso,
            )
            .limit(limit)
        )
        return [r[0] for r in self.s.execute(stmt).all()]

    def hard_delete_old_soft_deletes(self, older_than_iso: str, limit: int = 1000) -> list[str]:
        """
        Find soft-deleted rows older than `older_than_iso` and physically remove
        them. Returns the list of removed ids so the caller can clean up disk.
        """
        stmt = (
            select(files.c.id)
            .where(
                files.c.deleted_at.is_not(None),
                files.c.deleted_at < older_than_iso,
            )
            .limit(limit)
        )
        ids = [r[0] for r in self.s.execute(stmt).all()]
        if ids:
            self.s.execute(files.delete().where(files.c.id.in_(ids)))
            self.s.commit()
        return ids


def get_store(session: Session) -> DbStore:
    """FastAPI dependency to get a store bound to the current session."""
    return DbStore(session)
