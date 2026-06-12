#!/bin/sh
# Boot order (Prism rule, ported in full): migrations -> seed -> API.
# The seed is idempotent by construction (get-or-create everywhere) so
# it runs on EVERY boot; `set -e` makes a seed failure a loud non-zero
# exit — the RBAC boot check would fail right after anyway, better the
# clear error first.
set -e

echo "[start.sh] Running database migrations..."
alembic upgrade head

if [ "$ENVIRONMENT" = "production" ]; then
    SEED_MODE="prod"
else
    SEED_MODE="dev"
fi
echo "[start.sh] Seeding database (mode: $SEED_MODE)..."
python scripts/seed.py --mode "$SEED_MODE"

echo "[start.sh] Starting Nidria API..."
exec uvicorn src.main:app --host 0.0.0.0 --port 8000
