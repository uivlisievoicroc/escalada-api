#!/bin/bash
# CI gate for database migrations and testing
# Usage: ./ci_db_gate.sh

set -euo pipefail

echo "=== Escalada DB CI Gate ==="

# Set test database
export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/escalada_test"

# Step 1: Upgrade
echo "✓ Step 1: Running Alembic upgrade head..."
poetry run alembic upgrade head
echo "  Done"

# Step 2: Seed
echo "✓ Step 2: Running seed script..."
poetry run python -m escalada.scripts.seed
echo "  Done"

# Step 3: Run DB integration tests
echo "✓ Step 3: Running DB integration tests..."
poetry run pytest tests/test_db_integration.py -v --tb=short
echo "  Done"

# Step 4: Downgrade to base
echo "✓ Step 4: Running Alembic downgrade base..."
poetry run alembic downgrade base
echo "  Done"

# Step 5: Upgrade again
echo "✓ Step 5: Running Alembic upgrade head (round-trip)..."
poetry run alembic upgrade head
echo "  Done"

# Step 6: Run tests again
echo "✓ Step 6: Running DB tests again (post-round-trip)..."
poetry run pytest tests/test_db_integration.py -v --tb=short
echo "  Done"

echo ""
echo "✅ All CI gates passed!"
