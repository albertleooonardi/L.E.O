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
export LEO_LOCAL_ROUTER="${LEO_LOCAL_ROUTER:-1}"
SEARXNG_DIR="${SEARXNG_DIR:-$LEO_DATA_DIR/searxng}"
export LEO_PORT="${LEO_PORT:-8000}"
if ! [[ "$LEO_PORT" =~ ^[0-9]+$ ]] || [ "$LEO_PORT" -lt 1 ] || [ "$LEO_PORT" -gt 65535 ]; then
  echo "LEO_PORT must be a port between 1 and 65535." >&2
  exit 1
fi
URL="http://127.0.0.1:$LEO_PORT"

listening() { curl -s --max-time 1 "http://127.0.0.1:$1" >/dev/null 2>&1; }

if listening "$LEO_PORT"; then
  echo "Port $LEO_PORT is already in use; L.E.O. may already be running at $URL." >&2
  exit 1
fi

if [ ! -d .venv ] || [ ! -d frontend/node_modules ] || { [ "$LEO_LOCAL_ROUTER" != "0" ] && [ ! -f deploy/local-router/node_modules/9router/app/server.js ]; }; then
  ./setup.sh
else
  uv sync --frozen
fi
if [ "$LEO_LOCAL_ROUTER" != "0" ]; then
  export LEO_LOCAL_ROUTER_ENTRY="$PWD/deploy/local-router/node_modules/9router/app/server.js"
fi
if [ ! -f frontend/dist/index.html ] || [ -n "$(find frontend/src frontend/public -newer frontend/dist/index.html -print -quit 2>/dev/null)" ]; then
  echo "Building the UI…"
  npm run build --prefix frontend >/dev/null
fi

searxng=""
browser=""
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
trap '[ -n "$searxng" ] && kill "$searxng" 2>/dev/null; [ -n "$browser" ] && kill "$browser" 2>/dev/null; true' EXIT

# Open the browser once the API answers.
( for _ in $(seq 120); do
    if curl -sf "$URL/api/health" >/dev/null; then
      if [ "${LEO_OPEN_BROWSER:-1}" != "0" ]; then
        if command -v open >/dev/null; then open "$URL";
        elif command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1; fi
      fi
      exit
    fi
    sleep 0.5
  done ) &
browser=$!

echo "L.E.O.: $URL  (workspace: $LEO_DATA_DIR)"
echo "Keep this terminal open. In another terminal: leocode (or /path/to/L.E.O/leocode)."
if [ "$LEO_PORT" != "8000" ]; then echo "For this port: leocode --server $URL"; fi
uv run --frozen python -m backend.dev
