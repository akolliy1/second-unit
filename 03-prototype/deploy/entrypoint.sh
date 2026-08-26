#!/usr/bin/env bash
# Start the MCP bridge, wait for it to actually answer, then start the web app.
#
# "Wait for it to actually answer" matters: Cloud Run starts routing traffic as soon as the
# port is open, and a judge's first click landing while the bridge is still booting would
# surface as an agent with no tools -- which looks exactly like a broken submission.
set -uo pipefail

: "${GRAFANA_URL:?GRAFANA_URL is required}"
: "${GRAFANA_SERVICE_ACCOUNT_TOKEN:?GRAFANA_SERVICE_ACCOUNT_TOKEN is required}"
: "${MCP_GRAFANA_SERVER_TOKEN:?MCP_GRAFANA_SERVER_TOKEN is required}"

echo "==> starting mcp-grafana against ${GRAFANA_URL}"
mcp-grafana \
  -t streamable-http \
  --address 127.0.0.1:8000 \
  --endpoint-path /mcp \
  --server-auth-token "${MCP_GRAFANA_SERVER_TOKEN}" &
MCP_PID=$!

# The bridge rejects unauthenticated requests, so a 401 is proof it is UP and enforcing
# auth. Anything else (connection refused, empty) means it is not ready yet.
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 http://127.0.0.1:8000/mcp || true)
  if [ "$code" = "401" ] || [ "$code" = "400" ] || [ "$code" = "405" ]; then
    echo "==> mcp-grafana responding (HTTP ${code}) after ${i} attempt(s)"
    break
  fi
  if ! kill -0 "$MCP_PID" 2>/dev/null; then
    echo "!! mcp-grafana exited during startup" >&2
    exit 1
  fi
  sleep 0.5
done

echo "==> starting web on :${PORT}"
exec python -m uvicorn app:app --host 0.0.0.0 --port "${PORT}"
