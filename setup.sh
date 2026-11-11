#!/usr/bin/env bash
# Install a checkout for personal use. No sudo, Docker, cloud account, or database service required.
set -euo pipefail
cd "$(dirname "$0")"

for tool in uv node npm; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Missing $tool. Install Node.js 24 LTS (includes npm) and uv, then run ./setup.sh again." >&2
    echo "Node: https://nodejs.org/  uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
  fi
done
node -e 'const [a,b,c]=process.versions.node.split(".").map(Number); if(a<22 || a===22 && (b<23 || b===23 && c<4)){console.error("L.E.O. requires Node.js 22.23.4 or newer; Node 24 LTS is recommended.");process.exit(1)}'

echo "Installing L.E.O. and the CLI…"
uv sync --frozen
npm ci --prefix frontend
npm run build --prefix frontend
if [ "${LEO_LOCAL_ROUTER:-1}" != "0" ]; then
  # Use the standalone server directly: no global 9router install, tray, or postinstall downloads.
  npm ci --prefix deploy/local-router --ignore-scripts --no-audit --no-fund
fi
uv tool install --editable ./leo-cli --with-editable .
echo
echo "Ready. Run ./start.sh for the app, then leocode in your project in another terminal."
echo "If leocode is not on PATH, use ./leocode from this checkout, or run uv tool update-shell."
