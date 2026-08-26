#!/usr/bin/env bash
# Rung 0: make this machine capable. Your system Python is 3.9; ADK needs 3.10+.
# Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Python 3.11"
if ! brew list python@3.11 >/dev/null 2>&1; then brew install python@3.11; fi
PY="$(brew --prefix python@3.11)/bin/python3.11"
"$PY" -V

echo "==> gcloud CLI"
if ! command -v gcloud >/dev/null 2>&1; then
  brew install --cask google-cloud-sdk
  echo "!! Open a NEW terminal (or source your shell profile) so gcloud lands on PATH,"
  echo "!! then re-run this script."
  exit 1
fi
gcloud version | head -2

echo "==> venv + deps"
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt
echo "installed: $(./.venv/bin/python -m pip show google-adk | grep -i ^Version)"

echo "==> .env  (single file at 03-prototype/.env, shared with telemetry/)"
[ -f ../.env ] || { cp ../.env.example ../.env; echo "created ../.env — FILL IT IN before preflight"; }

cat <<'NEXT'

==> Bootstrap done. Now, in order:

  1. Fill in ../.env  (project id, Grafana URL + service account token, push creds)
  2. gcloud auth application-default login
     gcloud config set project <your-project-id>
     gcloud services enable aiplatform.googleapis.com
  3. ./.venv/bin/python preflight.py
  4. ./start_grafana_mcp.sh          # in a second terminal, leave running
  5. ./.venv/bin/python rung1_mcp_raw.py
  6. ./.venv/bin/python rung2_adk_agent.py
  7. ./.venv/bin/python rung3_cloud_mcp.py   # optional, records the strict-reading path

NEXT
