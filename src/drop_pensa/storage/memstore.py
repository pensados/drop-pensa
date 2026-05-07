"""
In-memory metadata store. PLACEHOLDER for Step 3.

This is intentionally minimal: it lets us validate upload+fetch end-to-end
in Step 2 without committing to a DB schema yet. In Step 3 we replace this
with SQLAlchemy core + SQLite, and the routes only have to swap the import.
"""
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class FileMeta:
    """Metadata for a stored file."""
    id: str
    filename: str
    size: int
    sha256: str
    content_type: str
    created_at: datetime
    expires_at: datetime
    one_shot: bool = False
    delete_token_hash: str = ""
    fetched_count: int = 0


class MemStore:
    """Simple in-memory dict-backed metadata store."""

    def __init__(self):
        self._files: dict[str, FileMeta] = {}

    def put(self, meta: FileMeta) -> None:
        self._files[meta.id] = meta

    def get(self, file_id: str) -> FileMeta | None:
        return self._files.get(file_id)

    def delete(self, file_id: str) -> bool:
        return self._files.pop(file_id, None) is not None

    def count(self) -> int:
        return len(self._files)

    def total_size(self) -> int:
        return sum(m.size for m in self._files.values())


# Module-level singleton. In Step 3 this is replaced by a DB session dep.
_store = MemStore()


def get_store() -> MemStore:
    """Return the global store. Used as a FastAPI dependency."""
    return _store


def now_utc() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)
