#!/bin/sh
set -e
PORT="${PORT:-8000}"
exec xvfb-run -a --server-args="-screen 0 1280x900x24" \
    uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
