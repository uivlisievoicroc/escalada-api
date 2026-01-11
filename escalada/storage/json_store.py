import asyncio
import json
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

# JSON-only build: Postgres/Alembic removed
STORAGE_MODE = "json"
STORAGE_DIR = os.getenv("STORAGE_DIR", "data")

_box_locks: Dict[int, asyncio.Lock] = {}
_box_locks_lock = asyncio.Lock()
_audit_lock = asyncio.Lock()

logger = logging.getLogger(__name__)


async def _get_box_lock(box_id: int) -> asyncio.Lock:
    async with _box_locks_lock:
        lock = _box_locks.get(box_id)
        if lock is None:
            lock = asyncio.Lock()
            _box_locks[box_id] = lock
        return lock


def is_json_mode() -> bool:
    return STORAGE_MODE == "json"


def _storage_dir() -> Path:
    return Path(STORAGE_DIR)


def _boxes_dir() -> Path:
    return _storage_dir() / "boxes"


def _events_path() -> Path:
    return _storage_dir() / "events.ndjson"


def _users_path() -> Path:
    return _storage_dir() / "users.json"


def ensure_storage_dirs() -> None:
    base = _storage_dir()
    base.mkdir(parents=True, exist_ok=True)
    _boxes_dir().mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def load_box_states() -> Dict[int, dict]:
    ensure_storage_dirs()
    states: Dict[int, dict] = {}
    for path in _boxes_dir().glob("*.json"):
        try:
            box_id = int(path.stem)
        except ValueError:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if "boxVersion" not in data:
            data["boxVersion"] = 0
        if "sessionId" not in data:
            data["sessionId"] = str(uuid.uuid4())
        if "routesCount" not in data:
            data["routesCount"] = data.get("routeIndex") or 1
        if "holdsCounts" not in data:
            data["holdsCounts"] = []
        states[box_id] = data
    return states


async def save_box_state(box_id: int, state: dict) -> None:
    ensure_storage_dirs()
    payload = dict(state)
    path = _boxes_dir() / f"{box_id}.json"
    lock = await _get_box_lock(box_id)
    async with lock:
        _atomic_write_json(path, payload)


async def append_audit_event(event: dict) -> None:
    ensure_storage_dirs()
    line = json.dumps(event, ensure_ascii=False)
    async with _audit_lock:
        try:
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
                event = dict(event)
                event["payload"] = None
            tail.append(event)
    return list(reversed(list(tail)))


def load_users() -> Dict[str, dict]:
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

