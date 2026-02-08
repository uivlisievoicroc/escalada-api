"""
Escalada API entrypoint (FastAPI).

This module wires together:
- App startup/shutdown (lifespan): preload JSON state + start background maintenance tasks
- Global middleware: request logging + CORS
- Router registration: public endpoints + admin/ops endpoints
"""

# -------------------- Standard library imports --------------------
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from time import time

# -------------------- Third-party imports --------------------
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# -------------------- Local application imports --------------------
from escalada.api import live as live_module
from escalada.api.audit import router as audit_router
from escalada.api.auth import router as auth_router
from escalada.api.backup import collect_snapshots, router as backup_router, write_backup_file
from escalada.api.health import router as health_router
from escalada.api.live import router as live_router
from escalada.api.public import router as public_router
from escalada.api.ops import router as ops_router
from escalada.api.podium import router as podium_router
from escalada.api.save_ranking import router as save_ranking_router
from escalada.routers.upload import router as upload_router
from escalada.rate_limit import cleanup_rate_limit_data

# -------------------- Logging --------------------
# Log to stdout (for containers/terminal) and also to a local file (useful on event day).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("escalada.log")],
)

logger = logging.getLogger(__name__)

# -------------------- Environment configuration --------------------
# Load `.env` early because we use env vars for CORS, auth, and background task tuning.
load_dotenv()

# JWT secret is required for production deployments (dev default is intentionally flagged).
_jwt_secret = os.getenv("JWT_SECRET")
if not _jwt_secret or _jwt_secret == "dev-secret-change-me":
    logger.warning(
        "JWT_SECRET is missing or uses the default value; set a strong JWT_SECRET in the environment for production."
    )

# Background task tuning (minutes) + backup storage location.
BACKUP_INTERVAL_MIN = int(os.getenv("BACKUP_INTERVAL_MIN", "10"))
BACKUP_RETENTION_FILES = int(os.getenv("BACKUP_RETENTION_FILES", "20"))
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
RATE_LIMIT_CLEANUP_INTERVAL_MIN = int(os.getenv("RATE_LIMIT_CLEANUP_INTERVAL_MIN", "5"))

# References to asyncio Tasks so we can cancel them cleanly on shutdown.
backup_task: asyncio.Task | None = None
rate_limit_cleanup_task: asyncio.Task | None = None


async def run_migrations() -> None:
    """Backward-compatible no-op (Postgres/Alembic removed)."""
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events for the FastAPI application."""

    # -------------------- Startup --------------------
    logger.info("🚀 Escalada API starting up (JSON-only)...")

    # Best-effort state preload (allows restarts to pick up where the event left off).
    try:
        await live_module.preload_states()
    except Exception as exc:
        logger.warning("State preload skipped: %s", exc)

    async def _backup_loop():
        # Periodically snapshot all box states to JSON files for disaster recovery.
        output_dir = Path(BACKUP_DIR)
        while True:
            try:
                await asyncio.sleep(max(BACKUP_INTERVAL_MIN, 1) * 60)
                snaps = await collect_snapshots()
                path = await write_backup_file(output_dir, snaps)
                logger.info("Periodic backup saved to %s", path)

                files = sorted(output_dir.glob("backup_*.json"), reverse=True)
                for old in files[BACKUP_RETENTION_FILES:]:
                    try:
                        old.unlink()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Periodic backup failed: %s", exc, exc_info=True)

    async def _rate_limit_cleanup_loop():
        """Periodic cleanup of old rate limiting data to prevent memory leak."""
        while True:
            try:
                await asyncio.sleep(max(RATE_LIMIT_CLEANUP_INTERVAL_MIN, 1) * 60)
                cleanup_rate_limit_data()
                logger.debug("Rate limit data cleanup completed")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Rate limit cleanup failed: %s", exc)

    # Start background tasks if enabled (interval > 0).
    global backup_task, rate_limit_cleanup_task
    if BACKUP_INTERVAL_MIN > 0:
        backup_task = asyncio.create_task(_backup_loop())
    else:
        backup_task = None

    if RATE_LIMIT_CLEANUP_INTERVAL_MIN > 0:
        rate_limit_cleanup_task = asyncio.create_task(_rate_limit_cleanup_loop())
    else:
        rate_limit_cleanup_task = None

    yield

    # -------------------- Shutdown --------------------
    # Cancel background tasks to ensure a graceful shutdown (no dangling loops).
    logger.info("🛑 Escalada API shutting down...")
    if backup_task:
        backup_task.cancel()
        try:
            await backup_task
        except asyncio.CancelledError:
            pass

    if rate_limit_cleanup_task:
        rate_limit_cleanup_task.cancel()
        try:
            await rate_limit_cleanup_task
        except asyncio.CancelledError:
            pass


# -------------------- FastAPI app --------------------
app = FastAPI(
    title="Escalada Control Panel API",
    lifespan=lifespan,
)

# -------------------- CORS --------------------
# Default origins cover local dev + typical LAN deployments; can be overridden via env vars.
DEFAULT_ORIGINS = "http://localhost:5173,http://localhost:3000,http://192.168.100.205:5173"
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")

# Regex allows *.local and common private LAN IP ranges (useful for phones/tablets/TV browsers).
DEFAULT_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|[a-zA-Z0-9-]+\.local|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?$"
ALLOWED_ORIGIN_REGEX = os.getenv("ALLOWED_ORIGIN_REGEX", DEFAULT_ORIGIN_REGEX)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request, call_next):
    # Lightweight access log with timing; errors include stack traces for debugging.
    start_time = time()

    logger.info(
        "%s %s - Client: %s",
        request.method,
        request.url.path,
        request.client.host if request.client else "unknown",
    )

    try:
        response = await call_next(request)
        process_time = time() - start_time
        logger.info(
            "%s %s - Status: %s - Duration: %.3fs",
            request.method,
            request.url.path,
            response.status_code,
            process_time,
        )
        return response
    except Exception as exc:
        process_time = time() - start_time
        logger.error(
            "%s %s - Error: %s - Duration: %.3fs",
            request.method,
            request.url.path,
            str(exc),
            process_time,
            exc_info=True,
        )
        raise


@app.get("/health")
async def health():
    # Minimal liveness probe used by local tooling / reverse proxies.
    return {"status": "ok", "storage": "json"}


@app.get("/status/summary")
async def status_summary():
    # Human-friendly status endpoint (useful during events for quick sanity checks).
    return {
        "competitions": 0,
        "boxes": len(live_module.state_map),
        "events": 0,
        "last_event_at": None,
        "storage": "json",
    }


# -------------------- Router registration --------------------
# Public/API routers
app.include_router(upload_router, prefix="/api")
app.include_router(save_ranking_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(live_router, prefix="/api")
app.include_router(public_router, prefix="/api")
app.include_router(podium_router, prefix="/api")
app.include_router(health_router, prefix="/api")

# Admin routers (restricted endpoints)
app.include_router(backup_router, prefix="/api/admin")
app.include_router(audit_router, prefix="/api/admin")
app.include_router(ops_router, prefix="/api/admin")
