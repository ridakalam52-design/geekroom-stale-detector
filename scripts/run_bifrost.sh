#!/usr/bin/env bash
# Start a local Bifrost gateway with this project's config (virtual key, budget, rate limit,
# model allow-list, fallback test provider, pricing). Needs Node.js (npx) or Docker.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
: "${OTARI_API_KEY:?set OTARI_API_KEY (upstream key for the otari provider)}"
: "${BIFROST_VK:?set BIFROST_VK (the virtual key your client will send)}"
: "${BIFROST_SETUP_TOKEN:?set BIFROST_SETUP_TOKEN}"
mkdir -p .bifrost-data
cp bifrost/config.json .bifrost-data/config.json
if command -v npx >/dev/null 2>&1; then
  exec npx -y @maximhq/bifrost -app-dir "$PWD/.bifrost-data" -port "${BIFROST_PORT:-8080}"
else
  exec docker run --rm -p "${BIFROST_PORT:-8080}:8080" -v "$PWD/.bifrost-data:/app/data" \
    -e OTARI_API_KEY -e BIFROST_VK -e BIFROST_SETUP_TOKEN maximhq/bifrost
fi
