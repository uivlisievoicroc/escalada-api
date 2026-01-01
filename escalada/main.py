import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from time import time

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from escalada.api.auth import router as auth_router
from escalada.api.live import router as live_router
from escalada.api.podium import router as podium_router
from escalada.api.backup import router as backup_router
from escalada.api.save_ranking import router as save_ranking_router
from escalada.api import live as live_module
from escalada.db.database import get_session
from escalada.db.health import health_check_db
from escalada.db.models import Box, Competition, Event
from escalada.db.migrate import run_migrations
from escalada.routers.upload import router as upload_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("escalada.log")],
)

logger = logging.getLogger(__name__)

# Load .env early for CORS/DB settings
load_dotenv()

BACKUP_INTERVAL_MIN = int(os.getenv("BACKUP_INTERVAL_MIN", "10"))
BACKUP_RETENTION_FILES = int(os.getenv("BACKUP_RETENTION_FILES", "20"))
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
backup_task = None


# Define application lifespan (replaces deprecated @app.on_event)
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events for the FastAPI application"""
    # Startup logic
    logger.info("🚀 Escalada API starting up...")
    try:
        await run_migrations()
    except Exception as e:
        logger.error(f"Auto-migration failed: {e}", exc_info=True)
    try:
        await live_module.preload_states_from_db()
    except Exception as e:
        logger.warning(f"State preload skipped: {e}")
    # Start periodic backup task
    from pathlib import Path
    from escalada.api.backup import collect_snapshots, write_backup_file
    from escalada.db.database import AsyncSessionLocal

    async def _backup_loop():
        while True:
            await asyncio.sleep(BACKUP_INTERVAL_MIN * 60)
            try:
                async with AsyncSessionLocal() as session:
                    snaps = await collect_snapshots(session)
                path = await write_backup_file(Path(BACKUP_DIR), snaps)
                logger.info(f"Periodic backup saved to {path}")
                files = sorted(Path(BACKUP_DIR).glob("backup_*.json"), reverse=True)
                for old in files[BACKUP_RETENTION_FILES:]:
                    try:
                        old.unlink()
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Periodic backup failed: {e}", exc_info=True)

    global backup_task
    backup_task = asyncio.create_task(_backup_loop())
    yield
    # Shutdown logic
    logger.info("🛑 Escalada API shutting down...")
    if backup_task:
        backup_task.cancel()
        try:
            await backup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Escalada Control Panel API",
    lifespan=lifespan,  # Use modern lifespan instead of on_event decorators
)

# Secure CORS configuration
DEFAULT_ORIGINS = (
    "http://localhost:5173,http://localhost:3000,http://192.168.100.205:5173"
)
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")

# Allow localhost, 127.0.0.1, local network IPs, and .local hostnames
DEFAULT_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|[a-zA-Z0-9-]+\.local|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?$"
ALLOWED_ORIGIN_REGEX = os.getenv("ALLOWED_ORIGIN_REGEX", DEFAULT_ORIGIN_REGEX)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],  # include OPTIONS/HEAD for CORS preflight
    allow_headers=["*"],
)


# Custom middleware for request logging
@app.middleware("http")
async def log_requests(request, call_next):
    """Log all HTTP requests and responses"""
    start_time = time()

    # Log incoming request
    logger.info(
        f"{request.method} {request.url.path} - Client: {request.client.host if request.client else 'unknown'}"
    )

    try:
        response = await call_next(request)
        process_time = time() - start_time
        logger.info(
            f"{request.method} {request.url.path} - Status: {response.status_code} - Duration: {process_time:.3f}s"
        )
        return response
    except Exception as e:
        process_time = time() - start_time
        logger.error(
            f"{request.method} {request.url.path} - Error: {str(e)} - Duration: {process_time:.3f}s",
            exc_info=True,
        )
        raise


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)):
    """Health check endpoint with database connectivity."""
    return await health_check_db(session)


@app.get("/status/summary")
async def status_summary(session: AsyncSession = Depends(get_session)):
    """Lightweight counts to confirm persistence is working."""
    comps_count = await session.scalar(select(func.count(Competition.id)))
    boxes_count = await session.scalar(select(func.count(Box.id)))
    events_count = await session.scalar(select(func.count(Event.id)))
    last_event = await session.scalar(select(func.max(Event.created_at)))
    return {
        "competitions": comps_count or 0,
        "boxes": boxes_count or 0,
        "events": events_count or 0,
        "last_event_at": last_event.isoformat() if last_event else None,
    }


app.include_router(upload_router, prefix="/api")
app.include_router(save_ranking_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(live_router, prefix="/api")
app.include_router(podium_router, prefix="/api")
app.include_router(backup_router, prefix="/api/admin")
