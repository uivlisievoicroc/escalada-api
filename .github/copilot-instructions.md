# Copilot instructions (escalada-api)

## Big picture
- FastAPI backend that persists contest state and broadcasts real-time updates.
- All contest/business logic comes from the separate package `escalada-core`.
- **JSON-only storage** (no Postgres/Docker): box states in `STORAGE_DIR/boxes/*.json`, audit events in `STORAGE_DIR/events.ndjson`.

## Key entrypoints
- App + startup tasks: `escalada/main.py`
  - Startup calls `preload_states()` (loads JSON box states from disk), then starts periodic backups.
  - Backup loop is controlled by `BACKUP_INTERVAL_MIN` (default 10), `BACKUP_RETENTION_FILES` (default 20), `BACKUP_DIR` (default `backups`).
  - Startup default: clears all persisted box states on startup (fresh start). To **keep** state across restarts, set `RESET_BOXES_ON_START=0` (see `escalada/api/live.py`).

## Live state + commands (critical)
- `escalada/api/live.py` is the “source of truth” runtime layer:
  - In-memory `state_map` keyed by `boxId` (plus `state_locks` per box) guarded by `init_lock`.
  - `/api/cmd` validates via `escalada_core.ValidatedCmd`, rate-limits via `escalada/rate_limit.py`, then applies `escalada_core.apply_command()`.
  - Stale-tab protection is enforced via `sessionId` + `boxVersion` (`validate_session_and_version()`); rejected commands return `{status:"ignored"}`.
  - Global time tiebreak toggle uses `type: SET_TIME_CRITERION` and is handled outside per-box state.

## WebSocket contract
- WS endpoint: `/api/ws/{box_id}` (defined in `escalada/api/live.py`).
- Requires `token` query param (JWT); immediately sends a `STATE_SNAPSHOT`.
- Heartbeat: server sends `PING`; clients reply `PONG`. WS also accepts `REQUEST_STATE`.

## Public spectator API (read-only)
- Router: `escalada/api/public.py` under `/api/public/*`.
- Token: `POST /api/public/token` → spectator JWT with 24h TTL (no credentials required).
- Boxes list: `GET /api/public/boxes?token=...` → only returns **initiated** boxes.
- WS per box: `WS /api/public/ws/{boxId}?token=...` → sends `STATE_SNAPSHOT`, accepts only `PONG`/`REQUEST_STATE`.
- Role `spectator` is blocked from `/api/cmd` and private WS endpoints.

## Persistence & audit
- Storage layer: `escalada/storage/json_store.py` (JSON-only; Postgres/Alembic removed).
- Box states: `STORAGE_DIR/boxes/{boxId}.json` (contains `state` dict, `box_version`, `session_id`).
- Audit events: `STORAGE_DIR/events.ndjson` (one JSON object per line; dedupe on `box_id` + `action_id`).
- Snapshot/backup/export/restore: `escalada/api/backup.py`.
- Admin upload (Excel → listbox shape): `escalada/routers/upload.py`.
- Role/box authorization: `escalada/auth/deps.py` (HTTP uses OAuth2 bearer; WS uses token query param).

## Dev workflow
- Install core in editable mode: `poetry run pip install -e ../escalada-core`.
- Run API: `poetry run uvicorn escalada.main:app --reload --host 0.0.0.0 --port 8000` (use `--workers 1` for JSON mode).
- Tests: `poetry run pytest tests -q`.
- Formatting: `poetry run pre-commit run --all-files` (Black + isort).
