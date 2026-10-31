#!/usr/bin/env bash
# Run everything L.E.O. needs locally with one command; Ctrl+C stops it all.
#
#   ./start.sh               workspace in .leo/
#   ./start.sh .leo-bench    workspace in another folder (connections, keys, chats, settings)
#
# Installs dependencies on first run, rebuilds the UI when its source changed, starts web search
# (SearXNG) if it is installed in <workspace>/searxng or already running on port 8888, then runs the
# API and worker on http://127.0.0.1:8000 and opens it in the browser.
set -euo pipefail
cd "$(dirname "$0")"
export LEO_DATA_DIR="${1:-${LEO_DATA_DIR:-.leo}}"
SEARXNG_DIR="${SEARXNG_DIR:-$LEO_DATA_DIR/searxng}"
URL=http://127.0.0.1:8000

listening() { nc -z 127.0.0.1 "$1" 2>/dev/null; }

if listening 8000; then
  echo "Port 8000 is already in use; L.E.O. may already be running at $URL." >&2
  exit 1
fi

[ -d .venv ] || uv sync --frozen
[ -d frontend/node_modules ] || npm ci --prefix frontend
if [ ! -f frontend/dist/index.html ] || [ -n "$(find frontend/src frontend/public -newer frontend/dist/index.html -print -quit 2>/dev/null)" ]; then
  echo "Building the UI…"
  npm run build --prefix frontend >/dev/null
fi

searxng=""
if listening 8888; then
  echo "Web search: using SearXNG already running on port 8888"
elif [ -x "$SEARXNG_DIR/venv/bin/python" ]; then
  echo "Web search: starting SearXNG on port 8888 (log: $SEARXNG_DIR/searxng.log)"
  SEARXNG_SETTINGS_PATH="$PWD/deploy/searxng/settings.yml" SEARXNG_SECRET="${SEARXNG_SECRET:-$(openssl rand -hex 24)}" \
    PYTHONPATH="$SEARXNG_DIR/src" "$SEARXNG_DIR/venv/bin/python" -m searx.webapp >"$SEARXNG_DIR/searxng.log" 2>&1 &
  searxng=$!
else
  echo "Web search: off (SearXNG not found in $SEARXNG_DIR; see README → Web research)"
fi
trap '[ -n "$searxng" ] && kill "$searxng" 2>/dev/null; true' EXIT

# Open the browser once the API answers.
( for _ in $(seq 60); do
    if curl -sf "$URL/api/health" >/dev/null; then command -v open >/dev/null && open "$URL"; exit; fi
    sleep 0.5
  done ) &

echo "L.E.O.: $URL  (workspace: $LEO_DATA_DIR)"
uv run --frozen python -m backend.dev
