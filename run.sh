#!/usr/bin/env bash
# Start the A Cappella Arranger.
#
#   ./run.sh                 -> http://127.0.0.1:8000 (this machine only)
#   HOST=0.0.0.0 ./run.sh    -> also reachable from other machines by hostname
#   PORT=9000 ./run.sh       -> different port
#
# Use HOST=0.0.0.0 when your browser runs somewhere other than this machine.
# It makes the app reachable to anyone who can route to this host, so prefer an
# SSH tunnel instead if that matters:
#   ssh -L 8000:localhost:8000 <this-host>
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo "Serving on http://${HOST}:${PORT}"
if [ "${HOST}" = "0.0.0.0" ]; then
  echo "Reachable at http://$(hostname -f):${PORT}"
fi

exec .venv/bin/python -m uvicorn app.main:app --host "${HOST}" --port "${PORT}" "$@"
