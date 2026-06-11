#!/bin/sh
set -e

echo "[start.sh] Running database migrations..."
alembic upgrade head

echo "[start.sh] Starting Nidria API..."
exec uvicorn src.main:app --host 0.0.0.0 --port 8000
