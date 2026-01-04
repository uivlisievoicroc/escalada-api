# Copilot instructions (escalada-api)

## Big picture
- FastAPI backend that persists contest state and broadcasts real-time updates.
- All contest/business logic comes from the separate package `escalada-core`.

## Key entrypoints
- App + startup tasks: `escalada/main.py`
  - Startup runs `run_migrations()`, calls `preload_states_from_db()`, then starts periodic backups.
  - Backup loop is controlled by `BACKUP_INTERVAL_MIN`, `BACKUP_RETENTION_FILES`, `BACKUP_DIR`.

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

## Persistence & audit
- DB models: `escalada/db/models.py` (`Box.state` JSONB, `box_version`, `session_id`; `Event` audit rows with dedupe on `(box_id, action_id)`).
- Snapshot/backup/export/restore: `escalada/api/backup.py`.
- Admin upload (Excel → listbox shape): `escalada/routers/upload.py`.
- Role/box authorization: `escalada/auth/deps.py` (HTTP uses OAuth2 bearer; WS uses token query param).

## Dev workflow
- Install core in editable mode: `poetry run pip install -e ../escalada-core`.
- Run API: `poetry run uvicorn escalada.main:app --reload --host 0.0.0.0 --port 8000`.
- Tests: `poetry run pytest tests -q`.
- Formatting: `poetry run pre-commit run --all-files`.
