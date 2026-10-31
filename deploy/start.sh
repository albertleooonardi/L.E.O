#!/usr/bin/env bash
# One-container start for hosts like Railway: the worker and the API share the SQLite database on the
# mounted data volume. If either process exits, the container stops so the host restarts both.
set -euo pipefail
python -m backend.worker &
uvicorn backend.api:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips '*' &
wait -n
exit 1
