# RUNBOOK (ziua concursului)

## Cerințe

- API rulează în **JSON storage mode** (fără Postgres/Docker)
- Rulează un singur worker: `uvicorn ... --workers 1`

## Setup rapid

```bash
export STORAGE_DIR=./data
export BACKUP_DIR=./backups
# IMPORTANT: set a strong secret (do not use the default)
# export JWT_SECRET="..."
poetry run uvicorn escalada.main:app --host 0.0.0.0 --port 8000 --workers 1
```

## Securitate & CORS

- `JWT_SECRET`: dacă lipsește, API folosește un default (inacceptabil în producție). Setează un secret puternic în `.env` sau environment.
- `ALLOWED_ORIGINS`: listă separată prin virgulă cu origin‑urile UI permise (ex. `http://192.168.1.223:5173`).
- `ALLOWED_ORIGIN_REGEX`: alternativ/în plus, regex pentru origin‑uri (implicit permite `192.168.*.*` și `10.*.*.*`).

## Reset demo (opțional)

- Pentru demo/test, poți porni cu `RESET_BOXES_ON_START=1` ca să șteargă `data/boxes/*.json` la startup (pornește “curat”).
- Nu folosi asta în concurs (pierzi starea persistată).

## Verificări

- Health: `GET /health`
- Ops status: `GET /api/admin/ops/status`
- Backup manual: `POST /api/admin/ops/backup/now`

## Backup & restore

- Backup (all): `GET /api/admin/backup/full`
- Restore: `POST /api/admin/restore` (cu payload `{"snapshots":[...]}`)
