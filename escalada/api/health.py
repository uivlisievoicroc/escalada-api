# escalada/api/health.py
"""Health check endpoint for monitoring and load balancer probes."""

# This module intentionally exposes lightweight, read-only diagnostics.
# It is used by:
# - Liveness probes (process is running)
# - Readiness probes (service can answer basic requests)
# - Optional operational dashboards (storage/audit sizes, loaded box count)

# -------------------- Standard library imports --------------------
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

# -------------------- Third-party imports --------------------
from fastapi import APIRouter

# -------------------- Local application imports --------------------
# These are "in-memory" structures maintained by the live module (authoritative at runtime).
from escalada.api.live import state_map, state_locks
# JSON store path helpers (events file + storage root) used for size/usage reporting.
from escalada.storage.json_store import _events_path, STORAGE_DIR

logger = logging.getLogger(__name__)
# Router is mounted under `/api` in `escalada/main.py`.
router = APIRouter(tags=["health"])


def _get_audit_file_size_mb() -> float:
    """Return audit file size in MB, or 0 if not found."""
    # Best-effort: failures here should not break health probes.
    try:
        path = _events_path()
        if path.exists():
            return path.stat().st_size / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def _get_storage_usage_mb() -> float:
    """Return total storage directory size in MB."""
    # Best-effort recursion: counts file sizes under STORAGE_DIR.
    # Useful for monitoring disk growth during long events.
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
    # This endpoint is intentionally "safe": no secrets, only coarse counters and sizes.
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
    # Minimal readiness: ensure core in-memory structures are accessible.
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
    # If this handler is reachable, the process is alive.
    return {"status": "alive"}
