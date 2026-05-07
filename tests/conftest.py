"""
Pytest fixtures for drop-pensa.

Each test gets:
- A fresh tmp directory for storage
- A fresh SQLite DB in that directory
- A fresh FastAPI app instance bound to those
- An httpx test client that talks to the app in-process

We deliberately do NOT spin up uvicorn or hit the network. The TestClient
goes straight through ASGI, which is faster and avoids port collisions.

The cleanup background worker is disabled — tests that exercise expiry
do so by calling the cleanup function directly.
"""
from __future__ import annotations


import pytest


# Set env vars BEFORE drop_pensa modules are imported. The Settings
# object reads these at module load.
@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Each test gets its own storage dir, DB file, and ratelimit backend."""
    storage = tmp_path / "files"
    db_path = tmp_path / "drop.db"

    monkeypatch.setenv("STORAGE_DIR", str(storage))
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "5")  # smaller for tests
    monkeypatch.setenv("DEFAULT_TTL_SECONDS", "3600")
    monkeypatch.setenv("MAX_TTL_SECONDS", "604800")
    monkeypatch.setenv("BLOCKED_EXTENSIONS", "exe,bat,sh")
    monkeypatch.setenv("CLEANUP_ENABLED", "false")
    monkeypatch.delenv("REDIS_URL", raising=False)  # force memory backend

    # Wipe imports of drop_pensa so settings re-init under the new env.
    import sys
    for mod in list(sys.modules):
        if mod == "drop_pensa" or mod.startswith("drop_pensa."):
            del sys.modules[mod]

    yield


@pytest.fixture
def client():
    """A TestClient bound to a fresh app instance."""
    from fastapi.testclient import TestClient
    from drop_pensa.app import create_app

    app = create_app()
    # TestClient runs the lifespan, so DB schema gets created.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def upload_one(client):
    """
    Helper: upload a small file and return the JSON payload.

    Most tests start by getting *something* into the system, so this
    saves a few lines per test.
    """
    def _upload(content: bytes = b"hello drop-pensa\n",
                filename: str = "hello.txt",
                expires_in: int | None = None,
                one_shot: bool = False) -> dict:
        params = {}
        if expires_in is not None:
            params["expires_in"] = expires_in
        if one_shot:
            params["one_shot"] = "true"

        resp = client.post(
            "/upload",
            params=params,
            files={"file": (filename, content, "application/octet-stream")},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _upload
