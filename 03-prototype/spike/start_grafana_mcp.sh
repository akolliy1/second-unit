#!/usr/bin/env bash
# Runs Grafana's official OSS MCP server as a local streamable-HTTP bridge to your
# Grafana Cloud stack. This is the headless-auth path: static service account token
# in, bearer token out. Leave this running in its own terminal.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ../.env; set +a   # single shared env file at 03-prototype/.env

: "${GRAFANA_URL:?set GRAFANA_URL in .env}"
: "${GRAFANA_SERVICE_ACCOUNT_TOKEN:?set GRAFANA_SERVICE_ACCOUNT_TOKEN in .env}"
: "${MCP_GRAFANA_SERVER_TOKEN:?set MCP_GRAFANA_SERVER_TOKEN in .env}"

echo "==> mcp-grafana -> ${GRAFANA_URL}"
echo "==> listening on http://localhost:8000/mcp"

# Prefer the native binary: Docker Desktop's daemon is often not running on this
# machine, and `go install` gave us mcp-grafana without that dependency. Falls back
# to the container if the binary is absent.
BIN="${HOME}/go/bin/mcp-grafana"
if [ -x "$BIN" ]; then
  exec "$BIN" \
    -t streamable-http \
    --address 0.0.0.0:8000 \
    --endpoint-path /mcp \
    --server-auth-token "${MCP_GRAFANA_SERVER_TOKEN}"
fi

echo "(no native binary at $BIN — falling back to Docker; needs the daemon running)"
docker run --rm -i -p 8000:8000 \
  -e GRAFANA_URL="${GRAFANA_URL}" \
  -e GRAFANA_SERVICE_ACCOUNT_TOKEN="${GRAFANA_SERVICE_ACCOUNT_TOKEN}" \
  grafana/mcp-grafana \
    -t streamable-http \
    --address 0.0.0.0:8000 \
    --endpoint-path /mcp \
    --server-auth-token "${MCP_GRAFANA_SERVER_TOKEN}"
