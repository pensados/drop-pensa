# drop-pensa

[![ci](https://github.com/pensados/drop-pensa/actions/workflows/ci.yml/badge.svg)](https://github.com/pensados/drop-pensa/actions/workflows/ci.yml)
[![license: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Instant file drop service. Upload a file, get a temporary public URL,
share or fetch it programmatically. Files auto-expire on a window you
choose.

Live at **https://drop.pensa.ar**.

```bash
curl -F "file=@report.pdf" https://drop.pensa.ar/upload
# → {"url": "https://drop.pensa.ar/f/aB3xK9-mnP2qR/report.pdf", ...}
```

---

## Features

- **Upload** files up to 50 MB via multipart POST or a web UI
- **Auto-expiry** — pick 1 hour, 6 hours, 24 hours, or 7 days
- **One-shot mode** — file disappears after first download
- **Delete-by-token** — capability returned at upload, hashed at rest
- **Web UI** — drag-and-drop, mobile-friendly, dark mode, QR code, ~26 KB
- **Hardened** — strict CSP, rate limits per-IP and per-file, sanitized
  filenames, blocked dangerous extensions, HTML/SVG served as text
- **Structured logs** — JSON to stdout, ready for Loki / similar
- **Self-hosted** — single Docker container + Redis (optional, for
  cross-replica rate limits)

---

## Quick start

### Local development

```bash
docker compose -f docker-compose.dev.yml up
# → http://localhost:8000
```

The dev compose mounts the source tree so edits are picked up by
uvicorn's reloader.

### Production

```bash
docker compose up -d
# → http://localhost:8019  (put nginx in front for TLS — see nginx/)
```

The production compose binds only to `127.0.0.1:8019` so you can put a
reverse proxy in front of it. Sample nginx vhost in
[`nginx/drop-pensa.conf`](nginx/drop-pensa.conf).

---

## API

Full reference at https://drop.pensa.ar/api. Quick tour:

```bash
# Upload (default TTL = 1 hour)
curl -F "file=@./README.md" https://drop.pensa.ar/upload

# Upload with a 7-day TTL and one-shot
curl -F "file=@./photo.jpg" \
  "https://drop.pensa.ar/upload?expires_in=604800&one_shot=true"

# Fetch
curl -O "https://drop.pensa.ar/f/aB3xK9-mnP2qR/photo.jpg?download=1"

# Metadata only (size, sha256, expiry, fetched_count)
curl https://drop.pensa.ar/f/aB3xK9-mnP2qR/info

# Delete (token comes from the upload response)
curl -X DELETE "https://drop.pensa.ar/f/aB3xK9-mnP2qR?token=zT8w..."
```

---

## Configuration

All settings come from environment variables. See `.env.example` for the
full list with defaults. The most important ones:

| Variable                  | Default                                | Notes                          |
|---------------------------|----------------------------------------|--------------------------------|
| `STORAGE_DIR`             | `/var/lib/drop-pensa/files`            | Where files live on disk       |
| `DB_URL`                  | `sqlite:////var/lib/drop-pensa/drop.db`| SQLite for now                 |
| `MAX_FILE_SIZE_MB`        | `50`                                   | Hard limit per upload          |
| `DEFAULT_TTL_SECONDS`     | `3600`                                 | 1 hour default                 |
| `MAX_TTL_SECONDS`         | `604800`                               | 7 days max                     |
| `BLOCKED_EXTENSIONS`      | `exe,bat,cmd,scr,...`                  | Comma-separated, case-insensitive |
| `CLEANUP_ENABLED`         | `true`                                 | Background expiry worker       |
| `CLEANUP_INTERVAL_SECONDS`| `300`                                  | How often it runs              |
| `REDIS_URL`               | unset                                  | Optional. Used for rate limits |

When `REDIS_URL` is unset, rate limits fall back to in-memory (per
process). For a single-node deploy that's fine. For multi-replica
deploys you want Redis so limits are shared.

---

## Architecture

```
[client]
   │
   ▼
[nginx]               (TLS termination, X-Forwarded-* headers, 60 MB body limit)
   │
   ▼
[drop-pensa]          (FastAPI + uvicorn, port 8000 inside container)
   │
   ├── POST /upload          → save_stream() → /var/lib/drop-pensa/files/<aa>/<id>
   ├── GET  /f/<id>/<name>   → stream_file()
   ├── HEAD /f/<id>/<name>   → headers only
   ├── GET  /f/<id>/info     → metadata
   ├── DELETE /f/<id>?token= → soft-delete + unlink
   ├── GET  /healthz         → counts and storage usage
   └── GET  /, /about, /api, /privacy, /terms
   │
   ├── (background) cleanup loop every 5 min
   │
   ├── SQLite metadata DB    /var/lib/drop-pensa/drop.db
   └── Redis (optional)      rate-limit buckets via Lua sliding window
```

Code organization:

```
src/drop_pensa/
├── app.py            # FastAPI factory, lifespan, middleware
├── config.py         # Settings (Pydantic v2)
├── security.py       # ID/token generation, filename sanitization, sharding
├── ratelimit.py      # Sliding-window limiter, Redis + memory backends
├── clientinfo.py     # IP extraction (X-Forwarded-For aware)
├── logging_setup.py  # JSON formatter + log_event/log_warning helpers
├── middleware.py     # Security headers (CSP, X-Frame-Options, etc.)
├── cleanup.py        # Async expiry worker
├── models.py         # SQLAlchemy core schema
├── routes/
│   ├── upload.py     # POST /upload
│   ├── fetch.py      # GET / HEAD /f/{id}/{filename}
│   ├── info.py       # GET /f/{id}/info
│   ├── delete.py     # DELETE /f/{id}
│   └── pages.py      # /about, /api, /privacy, /terms
├── storage/
│   ├── filesystem.py # Stream save, stream read, sharded layout
│   ├── db.py         # DbStore (SQLAlchemy core, sync sessions)
│   └── memstore.py   # FileMeta dataclass, used as common DTO
└── web/
    ├── static/       # index.html, app.js, qrcode.js, style.css, favicon.svg
    └── templates/    # base.html + about/api/privacy/terms (Jinja2)
```

---

## Tests

```bash
docker build -f Dockerfile.test -t drop-pensa-test .
docker run --rm drop-pensa-test
```

Three test files, ~75 tests total:

- `test_security.py` — sanitization, blocked extensions, size limits,
  delete tokens, XSS via uploaded HTML/SVG, security headers, ID
  non-enumeration
- `test_lifecycle.py` — upload, fetch, head, info, delete, expiry, one-shot,
  cleanup worker, doc pages
- `test_units.py` — pure-logic tests for `security.py` and `ratelimit.py`

Tests run in-process via `httpx.TestClient`, no network. Each gets a
fresh tmp dir, fresh SQLite, and fresh app instance via fixtures in
`conftest.py`.

---

## Deployment notes

### Behind a reverse proxy

drop-pensa expects to be behind a TLS-terminating reverse proxy. The app
trusts `X-Forwarded-Proto`, `X-Forwarded-For`, and `X-Forwarded-Host`
because uvicorn is started with `proxy_headers=True`. **Don't expose the
container port directly to the public internet** — without TLS the URLs
returned in upload responses will be HTTP, and rate limits will think
every client has the same IP.

### Storage

Files are stored at `<STORAGE_DIR>/<aa>/<id>` where `<aa>` is the first
two characters of the random ID. Mount this on a persistent volume —
the in-container `/var/lib/drop-pensa` is ephemeral by default.

Disk usage is bounded by `MAX_FILE_SIZE_MB × concurrent_uploads × MAX_TTL`.
For default settings: 50 MB × ~30 uploads/hour × 7 days = ~250 GB
worst case. Plan accordingly.

### Database

SQLite for now. Migrations live in `alembic/versions/`. The first
migration (`0001_create_files_table.py`) matches what `metadata.create_all()`
produces, so a fresh deploy is consistent with applying migrations.

To switch to Postgres later: change `DB_URL`, run alembic, ship.

### Cleanup

The cleanup worker runs every `CLEANUP_INTERVAL_SECONDS`. Each pass:

1. Soft-deletes rows whose `expires_at < now`, then unlinks the file
   from disk
2. Hard-deletes rows that have been soft-deleted for more than 24 hours

If the worker fails (disk full, DB locked), it logs and retries on the
next pass. A single bad row doesn't poison the batch — each file is
deleted in its own DB transaction.

---

## Limits at a glance

| Resource           | Limit                              |
|--------------------|------------------------------------|
| File size          | 50 MB                              |
| Retention          | 1 hour (default) — 7 days (max)    |
| Uploads per IP     | 30/hour (10/hour if cross-origin)  |
| Fetches per IP     | 200/minute                         |
| Fetches per file   | 100/hour per (file_id, IP)         |
| Deletes per IP     | 60/hour                            |
| ID entropy         | ~78 bits (13 chars URL-safe)       |
| Delete token entropy| ~132 bits                         |

---

## Security

The threat model is "anyone can upload, the URL is the secret":

- **IDs are unguessable** (78 bits of entropy from `secrets.token_urlsafe`).
  Brute-forcing the URL space is infeasible.
- **Filenames are sanitized** — null bytes, path separators, percent-encoded
  variants, control characters, and traversal patterns all rejected at
  the boundary.
- **HTML and SVG are served as `text/plain`** with `X-Content-Type-Options:
  nosniff` to neutralize XSS via uploads.
- **Strict CSP** on UI pages: no inline scripts, no inline styles, no
  framing, no third-party connections.
- **Delete tokens are hashed** before storage (sha256). Even a full DB
  dump doesn't grant deletion authority.
- **Rate limits** are enforced per-IP and per-(file, IP) to mitigate
  abuse and hotlinking.
- **Logs never contain file contents** — only IP, filename, size, hash,
  and action.

What it's **not** designed for:

- **End-to-end encryption** — the server reads everything. Encrypt
  client-side if it matters.
- **Hosting** — explicit `text/plain` for HTML/SVG breaks any attempt
  to host a website here.
- **Persistence** — files expire. That's the point.

---

## License

Apache License 2.0. See [LICENSE](LICENSE).

---

## Author

Built by [Carlos Javier Torres Pensa](https://pensa.com.ar). Part of the
[pensados](https://github.com/pensados) projects.
