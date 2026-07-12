#!/bin/bash
# Start DealDesk locally. Default port 8799; override with DEALDESK_PORT=9000 ./run.sh
set -e
cd "$(dirname "$0")"
PORT="${DEALDESK_PORT:-8799}"
[ -x .venv/bin/uvicorn ] && UV=.venv/bin/uvicorn || UV=uvicorn
echo "DealDesk -> http://localhost:$PORT   (Ctrl-C to stop)"
exec "$UV" app.app:app --host 127.0.0.1 --port "$PORT"
