# Escalada Backend (FastAPI)

Real-time climbing competition management backend using FastAPI + WebSockets.

## Storage

This repo runs in **JSON storage mode only** (no Postgres/Docker).

- Optional: `STORAGE_DIR=./data` (default: `data`)
- Run a single worker: `--workers 1`

## Quick Start

```bash
poetry install
poetry run pip install -e ../escalada-core

export STORAGE_DIR=./data
poetry run uvicorn escalada.main:app --host 0.0.0.0 --port 8000 --workers 1
```

## Tests

```bash
poetry install
poetry run pip install -e ../escalada-core
poetry run pytest tests -q
```

## Backup & restore (ops)

- Backup JSON (single box): `GET /api/admin/backup/box/{boxId}`
- Backup JSON (all boxes): `GET /api/admin/backup/full`
- Restore din backup: `POST /api/admin/restore` cu payload `{"snapshots":[...]}`
- Periodic backups: controlate de `BACKUP_INTERVAL_MIN`, `BACKUP_RETENTION_FILES`, `BACKUP_DIR`

## CI notes

- Workflow-ul de CI instalează `escalada-core` din repo separat; dacă `escalada-core` este privat, setează secretul `ESCALADA_CORE_TOKEN` în GitHub Actions (PAT cu access read la `escalada-core`).

## Formatting & Hooks

Python formatting is enforced via pre-commit with Black and isort.

```bash
poetry run pre-commit run --all-files
```
