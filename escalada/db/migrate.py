"""
Auto-apply Alembic migrations on startup.
"""
import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from escalada.db.config import settings

logger = logging.getLogger(__name__)


async def run_migrations() -> None:
    """Apply migrations to head using the runtime DATABASE_URL."""
    # __file__ => escalada/db/migrate.py ; project root is one level up
    repo_root = Path(__file__).resolve().parent.parent.parent  # .../Escalada
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    logger.info("Applying migrations to head…")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (e.g., CLI execution) — run synchronously
        command.upgrade(cfg, "head")
    else:
        # Running inside an event loop (FastAPI lifespan) — offload to thread
        await asyncio.to_thread(command.upgrade, cfg, "head")

    logger.info("Migrations up to date.")
