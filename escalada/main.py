import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from time import time

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from escalada.api import live as live_module
from escalada.api.audit import router as audit_router
from escalada.api.auth import router as auth_router
from escalada.api.backup import collect_snapshots, router as backup_router, write_backup_file
from escalada.api.live import router as live_router
from escalada.api.ops import router as ops_router
from escalada.api.podium import router as podium_router
from escalada.api.save_ranking import router as save_ranking_router
from escalada.routers.upload import router as upload_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("escalada.log")],
)

logger = logging.getLogger(__name__)

# Load .env early for CORS settings
load_dotenv()

_jwt_secret = os.getenv("JWT_SECRET")
if not _jwt_secret or _jwt_secret == "dev-secret-change-me":
    logger.warning(
        "JWT_SECRET is missing or uses the default value; set a strong JWT_SECRET in the environment for production."
    )

BACKUP_INTERVAL_MIN = int(os.getenv("BACKUP_INTERVAL_MIN", "10"))
BACKUP_RETENTION_FILES = int(os.getenv("BACKUP_RETENTION_FILES", "20"))
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
backup_task: asyncio.Task | None = None


async def run_migrations() -> None:
    """Backward-compatible no-op (Postgres/Alembic removed)."""
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events for the FastAPI application."""

    logger.info("🚀 Escalada API starting up (JSON-only)...")

    try:
        await live_module.preload_states()
    except Exception as exc:
        logger.warning("State preload skipped: %s", exc)

    async def _backup_loop():
        output_dir = Path(BACKUP_DIR)
        while True:
            await asyncio.sleep(max(BACKUP_INTERVAL_MIN, 1) * 60)
            try:
                snaps = await collect_snapshots()
                path = await write_backup_file(output_dir, snaps)
                logger.info("Periodic backup saved to %s", path)

                files = sorted(output_dir.glob("backup_*.json"), reverse=True)
                for old in files[BACKUP_RETENTION_FILES:]:
                    try:
                        old.unlink()
                    except Exception:
                        pass
            except Exception as exc:
                logger.error("Periodic backup failed: %s", exc, exc_info=True)

    global backup_task
    if BACKUP_INTERVAL_MIN > 0:
        backup_task = asyncio.create_task(_backup_loop())
    else:
        backup_task = None

    yield

    logger.info("🛑 Escalada API shutting down...")
    if backup_task:
        backup_task.cancel()
        try:
            await backup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Escalada Control Panel API",
    lifespan=lifespan,
)

# Secure CORS configuration
DEFAULT_ORIGINS = "http://localhost:5173,http://localhost:3000,http://192.168.100.205:5173"
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")

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
    return {"status": "ok", "storage": "json"}


@app.get("/status/summary")
async def status_summary():
    return {
        "competitions": 0,
        "boxes": len(live_module.state_map),
        "events": 0,
        "last_event_at": None,
        "storage": "json",
    }


app.include_router(upload_router, prefix="/api")
app.include_router(save_ranking_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(live_router, prefix="/api")
app.include_router(podium_router, prefix="/api")
app.include_router(backup_router, prefix="/api/admin")
app.include_router(audit_router, prefix="/api/admin")
app.include_router(ops_router, prefix="/api/admin")
