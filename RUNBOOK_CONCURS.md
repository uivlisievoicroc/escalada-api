# RUNBOOK (ziua concursului)

## Cerințe

- API rulează în **JSON storage mode** (fără Postgres/Docker)
- Rulează un singur worker: `uvicorn ... --workers 1`

## Setup rapid

```bash
export STORAGE_DIR=./data
export BACKUP_DIR=./backups
poetry run uvicorn escalada.main:app --host 0.0.0.0 --port 8000 --workers 1
```

## Verificări

- Health: `GET /health`
- Ops status: `GET /api/admin/ops/status`
- Backup manual: `POST /api/admin/ops/backup/now`

## Backup & restore

- Backup (all): `GET /api/admin/backup/full`
- Restore: `POST /api/admin/restore` (cu payload `{"snapshots":[...]}`)
