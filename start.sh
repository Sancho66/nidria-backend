#!/bin/sh
# Boot order (Prism rule, ported in full): migrations -> seed -> superadmins
# -> API. The seed is idempotent by construction (get-or-create everywhere) so
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

# Ensure the platform superadmins (idempotent): the seed never migrates an
# EXISTING agent's role, so on an already-seeded DB the founders would stay
# 'admin'. This upgrades the superadmin role's permissions and flips Alexandre
# & Eric to it — a no-op once done. Runs on every deploy.
echo "[start.sh] Ensuring platform superadmins..."
python scripts/migrate_superadmins.py

echo "[start.sh] Starting Nidria API..."
exec uvicorn src.main:app --host 0.0.0.0 --port 8000
