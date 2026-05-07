"""
FastAPI application factory for drop-pensa.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from drop_pensa import ratelimit
from drop_pensa.cleanup import cleanup_loop
from drop_pensa.config import settings
from drop_pensa.logging_setup import configure as configure_logging, log_event
from drop_pensa.middleware import SecurityHeadersMiddleware
from drop_pensa.routes import fetch as fetch_routes
from drop_pensa.routes import delete as delete_routes
from drop_pensa.routes import info as info_routes
from drop_pensa.routes import pages as pages_routes
from drop_pensa.routes import upload as upload_routes
from drop_pensa.storage.db import DbStore, ensure_schema, init_engine, session_scope


_PKG_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PKG_DIR / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown sequence."""
    configure_logging(debug=settings.debug)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    init_engine()
    ensure_schema()
    ratelimit.init(settings.redis_url)

    log_event(
        "startup",
        storage_dir=str(settings.storage_dir),
        db_url=settings.db_url,
        max_file_size_mb=settings.max_file_size_mb,
        default_ttl=settings.default_ttl_seconds,
        cleanup_enabled=settings.cleanup_enabled,
        redis=bool(settings.redis_url),
    )

    stop_event = asyncio.Event()
    cleanup_task = None
    if settings.cleanup_enabled:
        cleanup_task = asyncio.create_task(cleanup_loop(stop_event))

    try:
        yield
    finally:
        if cleanup_task is not None:
            stop_event.set()
            try:
                await asyncio.wait_for(cleanup_task, timeout=5.0)
            except asyncio.TimeoutError:
                cleanup_task.cancel()
        log_event("shutdown")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
    )

    # CORS: allow any origin to POST /upload (so third-party tools can call
    # us from the browser), but never expose mutation endpoints other than
    # upload to cross-origin browser callers.
    #
    # Foreign origins still get rate-limited harder via clientinfo +
    # ratelimit. CORS is only the "is the response readable" gate.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "POST"],
        allow_headers=["content-type"],
        max_age=600,
    )

    # Security headers wrap everything else, applied last → run first on
    # the response side because middleware in Starlette is LIFO.
    app.add_middleware(SecurityHeadersMiddleware)

    # API routes.
    app.include_router(upload_routes.router)
    app.include_router(info_routes.router)
    app.include_router(fetch_routes.router)
    app.include_router(delete_routes.router)
    app.include_router(pages_routes.router)

    @app.get("/healthz")
    async def healthz():
        with session_scope() as sess:
            store = DbStore(sess)
            return JSONResponse(
                status_code=200,
                content={
                    "ok": True,
                    "files_count": store.count(),
                    "storage_used_mb": round(store.total_size() / (1024 * 1024), 2),
                },
            )

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots():
        return "User-agent: *\nDisallow: /\n"

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        async def root():
            # no-store on the index so a stale cached HTML can't pin the
            # client to obsolete asset paths after we ship UI changes.
            # The assets themselves (style.css, app.js, qrcode.js) are
            # fine to cache — if the index references them, they're current.
            return FileResponse(
                _STATIC_DIR / "index.html",
                headers={"Cache-Control": "no-store"},
            )

        @app.api_route("/favicon.svg", methods=["GET", "HEAD"], include_in_schema=False)
        async def favicon():
            return FileResponse(_STATIC_DIR / "favicon.svg")

    return app


app = create_app()
