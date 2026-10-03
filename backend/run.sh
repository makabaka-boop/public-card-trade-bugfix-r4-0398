#!/usr/bin/env bash
# Start the FastAPI draft server. Override knobs via environment:
#   DRAFT_DB_PATH=./draft.db DRAFT_ROUNDS=6 DRAFT_TIMEOUT=45 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
