"""
Health check endpoint and observability utilities.
"""
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from escalada.db.database import engine


async def health_check_db(session: AsyncSession) -> dict:
    """Check database connectivity and report basic metrics."""
    try:
        result = await session.execute(text("SELECT 1"))
        result.scalar()
        
        # Get pool stats
        pool = engine.pool
        pool_size = pool.size() if hasattr(pool, "size") else "N/A"
        checked_in = pool.checkedout() if hasattr(pool, "checkedout") else "N/A"
        
        return {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database": "connected",
            "pool_size": pool_size,
            "connections_in_use": checked_in,
        }
    except Exception as e:
        return {
            "status": "error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database": "disconnected",
            "error": str(e),
        }
