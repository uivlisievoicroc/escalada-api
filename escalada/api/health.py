# escalada/api/health.py
"""Health check endpoint for monitoring and load balancer probes."""
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from escalada.api.live import state_map, state_locks
from escalada.storage.json_store import _events_path, STORAGE_DIR

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


def _get_audit_file_size_mb() -> float:
    """Return audit file size in MB, or 0 if not found."""
    try:
        path = _events_path()
        if path.exists():
            return path.stat().st_size / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def _get_storage_usage_mb() -> float:
    """Return total storage directory size in MB."""
    try:
        storage_path = Path(STORAGE_DIR)
        if not storage_path.exists():
            return 0.0
        total = sum(f.stat().st_size for f in storage_path.rglob("*") if f.is_file())
        return total / (1024 * 1024)
    except Exception:
        pass
    return 0.0


@router.get("/health")
async def health_check():
    """
    Health check endpoint for monitoring.
    
    Returns:
        - status: "ok" if healthy
        - boxes_loaded: number of box states in memory
        - ws_locks: number of active box locks
        - audit_file_mb: size of audit log file in MB
        - storage_mb: total storage usage in MB
        - timestamp: current server time (UTC)
    """
    return {
        "status": "ok",
        "boxes_loaded": len(state_map),
        "ws_locks": len(state_locks),
        "audit_file_mb": round(_get_audit_file_size_mb(), 2),
        "storage_mb": round(_get_storage_usage_mb(), 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health/ready")
async def readiness_check():
    """
    Readiness probe - checks if the service is ready to accept traffic.
    """
    # Check if we can access state_map
    try:
        _ = len(state_map)
        return {"status": "ready", "boxes_count": len(state_map)}
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        return {"status": "not_ready", "error": str(e)}


@router.get("/health/live")
async def liveness_check():
    """
    Liveness probe - basic check that the service is running.
    """
    return {"status": "alive"}
