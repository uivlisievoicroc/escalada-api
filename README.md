# Escalada Backend (FastAPI)

Real-time climbing competition management backend using FastAPI + WebSockets.

## Quick Start

```bash
poetry install
poetry run pip install -e ../escalada-core

poetry run uvicorn escalada.main:app --reload --host 0.0.0.0 --port 8000
```

## Tests

```bash
poetry install
poetry run pip install -e ../escalada-core

# Optional (DB integration):
# docker compose up -d db

poetry run pytest tests -q
```

## CI notes

- Workflow-ul de CI instalează `escalada-core` din repo separat; dacă `escalada-core` este privat, setează secretul `ESCALADA_CORE_TOKEN` în GitHub Actions (PAT cu access read la `escalada-core`).

## Formatting & Hooks

Python formatting is enforced via pre-commit with Black and isort.

```bash
# Format all backend Python files (Black + isort)
poetry run pre-commit run --all-files
```
