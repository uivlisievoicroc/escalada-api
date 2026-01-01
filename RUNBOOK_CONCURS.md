# Runbook — Operare concurs (Escalada)

Document scurt pentru “ziua de concurs”: pornire, verificări, backup, recovery, export oficial, audit.

## 0) Prerechizite
- Postgres pornit și accesibil prin `DATABASE_URL` (sau `TEST_DATABASE_URL`)
- `BACKUP_DIR` (opțional) — folder local unde se scriu backup-urile JSON (default: `backups`)
- UI pornește separat (repo `escalada-ui`) și comunică doar prin API

## 1) Pornire (start of day)
1. Pornește DB (ex: Docker) și verifică port/cred.
2. Pornește API (`uvicorn escalada.main:app ...`).
3. Verifică:
   - `GET /health`
   - `GET /api/admin/ops/status` (admin)

## 2) În timpul concursului (operare)
- Comenzile de live vin pe WS/HTTP din UI.
- Persistența stării se face în DB la fiecare comandă; la restart se reîncarcă automat din DB.

## 3) Backup (manual + automat)
### Automat
- API scrie periodic backup JSON (interval din `BACKUP_INTERVAL_MIN`, folder `BACKUP_DIR`).

### Manual (recomandat înainte de final / la pauze)
- `POST /api/admin/ops/backup/now` (admin) → scrie un backup JSON în `BACKUP_DIR`.
- `GET /api/admin/backup/last?download=true` (admin) → descarcă ultimul backup JSON.

## 4) Drill de failover (non-destructiv)
Scop: confirmă că snapshot-urile curente “se pot restaura” logic, fără să schimbe DB sau memoria live.
- `POST /api/admin/ops/drill/backup_restore` (admin)
  - opțional body: `{ "box_ids": [1,2], "write_backup_file": true }`
  - returnează câte snapshot-uri/restaurări ar reuși; pe conflict dă `409`.

## 5) Recovery (după restart / incident)
### Restart normal (cel mai comun)
- API la startup rulează migrații și `preload_states_from_db()`; concursul revine din DB.

### Restore manual din backup JSON
1. Ia backup-ul (ex: din `GET /api/admin/backup/last?download=true`).
2. Trimite conținutul în:
   - `POST /api/admin/restore` cu `{ "snapshots": [...] }`
3. Verifică UI că state-ul e coerent pe box-urile afectate.

## 6) Export rezultate oficiale (ZIP)
Per box:
- `GET /api/admin/export/official/box/{box_id}` (admin) → descarcă ZIP cu:
  - `overall.xlsx` + `overall.pdf`
  - `route_N.xlsx` + `route_N.pdf`
  - `metadata.json`

## 7) Audit log (cine/ce/când)
- `GET /api/admin/audit/events?boxId=&limit=&includePayload=` (admin)
- Audit include: `action_id`, user/role (din JWT), IP + user-agent (din request).

