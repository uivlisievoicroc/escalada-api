"""
JSON storage backend (file-based persistence).

This module provides:
- Per-box state persistence under `STORAGE_DIR/boxes/{boxId}.json` (atomic writes)
- Append-only audit log in NDJSON format (`STORAGE_DIR/events.ndjson`) with size-based rotation
- User database stored in `STORAGE_DIR/users.json` (includes default admin bootstrap + reset escape hatch)
- Global competition officials stored in `STORAGE_DIR/competition_officials.json`

Concurrency model:
- A per-box asyncio.Lock prevents overlapping writes for the same box id
- A global audit lock serializes appends/rotations of the NDJSON audit log
"""

# -------------------- Standard library imports --------------------
import asyncio
import json
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

# -------------------- Storage configuration --------------------
# JSON-only build: Postgres/Alembic removed (all persistence is file-based).
STORAGE_MODE = "json"
STORAGE_DIR = os.getenv("STORAGE_DIR", "data")

# -------------------- Concurrency primitives --------------------
# Per-box locks are created lazily and guarded by `_box_locks_lock` to avoid races.
_box_locks: Dict[int, asyncio.Lock] = {}
_box_locks_lock = asyncio.Lock()
# Single lock for audit writes/rotation (NDJSON is append-only but rotation/rename must be serialized).
_audit_lock = asyncio.Lock()

# -------------------- Audit file rotation settings --------------------
MAX_AUDIT_FILE_SIZE_MB = int(os.getenv("MAX_AUDIT_FILE_SIZE_MB", "50"))

logger = logging.getLogger(__name__)


async def _get_box_lock(box_id: int) -> asyncio.Lock:
    # Lazily create the lock for this box id (safe under `_box_locks_lock`).
    async with _box_locks_lock:
        lock = _box_locks.get(box_id)
        if lock is None:
            lock = asyncio.Lock()
            _box_locks[box_id] = lock
        return lock


def is_json_mode() -> bool:
    return STORAGE_MODE == "json"


def _storage_dir() -> Path:
    # Root storage directory (defaults to `./data`).
    return Path(STORAGE_DIR)


def _boxes_dir() -> Path:
    # Per-box state files live under `data/boxes/{boxId}.json`.
    return _storage_dir() / "boxes"


def _events_path() -> Path:
    # Append-only audit log (NDJSON: 1 JSON object per line).
    return _storage_dir() / "events.ndjson"


def _users_path() -> Path:
    # User database (JSON dict keyed by username).
    return _storage_dir() / "users.json"


def _competition_officials_path() -> Path:
    # Global competition officials persisted as a single JSON object.
    return _storage_dir() / "competition_officials.json"


def ensure_storage_dirs() -> None:
    # Create the root + `boxes/` folder if missing (idempotent).
    base = _storage_dir()
    base.mkdir(parents=True, exist_ok=True)
    _boxes_dir().mkdir(parents=True, exist_ok=True)


def clear_box_state_files() -> int:
    """
    Delete all persisted box state JSON files (data/boxes/*.json).
    Returns the number of deleted files.
    """
    ensure_storage_dirs()
    removed = 0
    for path in _boxes_dir().glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except Exception as exc:
            logger.warning("Failed to delete box state file %s: %s", path, exc)
    return removed


def _atomic_write_json(path: Path, payload: Any) -> None:
    # Atomic write pattern:
    # 1) write to `*.tmp`
    # 2) replace the target file in one filesystem operation
    # This prevents partial/corrupt JSON files on power loss or concurrent restarts.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def load_box_states() -> Dict[int, dict]:
    """Load box states from JSON files with validation.
    Skips invalid/corrupt files to prevent startup crashes.
    """
    ensure_storage_dirs()
    states: Dict[int, dict] = {}
    for path in _boxes_dir().glob("*.json"):
        # Box id is derived from the filename (e.g. `0.json` -> box_id=0).
        try:
            box_id = int(path.stem)
        except ValueError:
            logger.warning(f"Skipping invalid box state filename: {path.name}")
            continue

        # Parse JSON (corrupt files are skipped instead of crashing the process).
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error(f"Corrupt JSON in box state file {path.name}: {exc}")
            continue
        except Exception as exc:
            logger.error(f"Failed to read box state file {path.name}: {exc}")
            continue
        
        # Validate basic structure (must be a dict-like state object).
        if not isinstance(data, dict):
            logger.warning(f"Invalid box state format in {path.name} (not a dict), skipping")
            continue
        
        # Validate critical fields if present (defensive against manual edits / older versions).
        if "initiated" in data and not isinstance(data["initiated"], bool):
            logger.warning(f"Invalid 'initiated' field in {path.name}, skipping")
            continue
        
        if "competitors" in data and not isinstance(data["competitors"], list):
            logger.warning(f"Invalid 'competitors' field in {path.name}, skipping")
            continue
        
        # Apply defaults for missing fields (backward compatibility).
        if "boxVersion" not in data:
            data["boxVersion"] = 0
        if "sessionId" not in data:
            data["sessionId"] = str(uuid.uuid4())
        if "routesCount" not in data:
            data["routesCount"] = data.get("routeIndex") or 1
        if "holdsCounts" not in data:
            data["holdsCounts"] = []
        
        states[box_id] = data
        logger.debug(f"Loaded box state: {box_id} (version={data.get('boxVersion')})")
    
    if states:
        logger.info(f"Successfully loaded {len(states)} valid box states")
    return states


def load_competition_officials() -> dict[str, str]:
    """Load global competition officials (judge chief + competition director + chief routesetter)."""
    ensure_storage_dirs()
    path = _competition_officials_path()
    if not path.exists():
        return {"judgeChief": "", "competitionDirector": "", "chiefRoutesetter": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to load competition officials: %s", exc)
        return {"judgeChief": "", "competitionDirector": "", "chiefRoutesetter": ""}
    if not isinstance(data, dict):
        return {"judgeChief": "", "competitionDirector": "", "chiefRoutesetter": ""}
    judge = data.get("judgeChief")
    director = data.get("competitionDirector")
    chief_routesetter = data.get("chiefRoutesetter")
    return {
        "judgeChief": judge.strip() if isinstance(judge, str) else "",
        "competitionDirector": director.strip() if isinstance(director, str) else "",
        "chiefRoutesetter": chief_routesetter.strip() if isinstance(chief_routesetter, str) else "",
    }


def save_competition_officials(judge_chief: str, competition_director: str, chief_routesetter: str) -> None:
    """Persist global competition officials (JSON-only)."""
    ensure_storage_dirs()
    payload = {
        "judgeChief": (judge_chief or "").strip(),
        "competitionDirector": (competition_director or "").strip(),
        "chiefRoutesetter": (chief_routesetter or "").strip(),
    }
    _atomic_write_json(_competition_officials_path(), payload)


async def save_box_state(box_id: int, state: dict) -> None:
    # Persist a single box state file (serialized under the per-box lock).
    ensure_storage_dirs()
    payload = dict(state)
    path = _boxes_dir() / f"{box_id}.json"
    lock = await _get_box_lock(box_id)
    async with lock:
        _atomic_write_json(path, payload)


def _rotate_audit_file_if_needed() -> None:
    """Rotate audit file if it exceeds MAX_AUDIT_FILE_SIZE_MB."""
    path = _events_path()
    if not path.exists():
        return
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb >= MAX_AUDIT_FILE_SIZE_MB:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            archive_name = f"events.{timestamp}.ndjson"
            archive_path = path.parent / archive_name
            path.rename(archive_path)
            logger.info("Rotated audit file to %s (was %.2f MB)", archive_name, size_mb)
    except Exception as exc:
        logger.warning("Failed to rotate audit file: %s", exc)


async def append_audit_event(event: dict) -> None:
    # Append a single event as NDJSON.
    # Rotation happens under the same lock so rename + append cannot interleave.
    ensure_storage_dirs()
    line = json.dumps(event, ensure_ascii=False)
    async with _audit_lock:
        try:
            # Check rotation before appending
            _rotate_audit_file_if_needed()
            with _events_path().open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception as exc:
            logger.warning("Failed to append audit event: %s", exc)


def read_latest_events(
    *,
    limit: int = 200,
    include_payload: bool = False,
    box_id: int | None = None,
) -> list[dict]:
    # Tail the NDJSON audit log in a memory-bounded way using a deque.
    path = _events_path()
    if not path.exists():
        return []
    tail: deque[dict] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if box_id is not None and event.get("boxId") != box_id:
                continue
            if not include_payload:
                # Strip payload for lighter UI responses unless explicitly requested.
                event = dict(event)
                event["payload"] = None
            tail.append(event)
    return list(reversed(list(tail)))


def load_users() -> Dict[str, dict]:
    # Supports both dict and legacy list formats.
    ensure_storage_dirs()
    path = _users_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        users: Dict[str, dict] = {}
        for entry in data:
            if isinstance(entry, dict) and entry.get("username"):
                users[entry["username"]] = entry
        return users
    return {}


def save_users(users: Dict[str, dict]) -> None:
    ensure_storage_dirs()
    _atomic_write_json(_users_path(), users)


def get_users_with_default_admin() -> Dict[str, dict]:
    # Ensure there is always an "admin" user present.
    # This is intentionally file-based and local-only (JSON mode).
    users = load_users()

    if "admin" in users:
        # Optional escape hatch to reset admin password without editing users.json
        if os.getenv("RESET_ADMIN_PASSWORD"):
            from escalada.auth.service import hash_password

            now = datetime.now(timezone.utc).isoformat()
            password = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin")
            users["admin"]["password_hash"] = hash_password(password)
            users["admin"]["updated_at"] = now
            save_users(users)
            logger.warning("Admin password was reset via RESET_ADMIN_PASSWORD")
        return users
    from escalada.auth.service import hash_password

    now = datetime.now(timezone.utc).isoformat()
    password = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin")
    users["admin"] = {
        "username": "admin",
        "password_hash": hash_password(password),
        "role": "admin",
        "assigned_boxes": [],
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    save_users(users)
    return users


def build_audit_event(
    *,
    action: str,
    payload: dict,
    box_id: int | None,
    state: dict | None,
    actor: dict | None,
) -> dict:
    # Normalized event envelope written to NDJSON.
    # Includes useful metadata for later debugging/auditing (actor, session/version, timestamps).
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "createdAt": now,
        "competitionId": 0,
        "boxId": box_id,
        "action": action,
        "actionId": payload.get("actionId") if isinstance(payload, dict) else None,
        "boxVersion": (state or {}).get("boxVersion", 0) if state else 0,
        "sessionId": (state or {}).get("sessionId") if state else None,
        "actorUsername": (actor or {}).get("username"),
        "actorRole": (actor or {}).get("role"),
        "actorIp": (actor or {}).get("ip"),
        "actorUserAgent": (actor or {}).get("user_agent"),
        "payload": payload if isinstance(payload, dict) else {},
    }
