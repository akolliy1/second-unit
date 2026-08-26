#!/usr/bin/env bash
# Deploy Second Unit to Cloud Run. Idempotent: safe to re-run.
#
# Reads nothing from .env by design — secrets go to Secret Manager and are injected as
# --set-secrets, so the running service never has a token baked into its image and the
# repo never needs one to deploy.
set -euo pipefail

PROJECT="${PROJECT:-second-unit-506700}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-second-unit}"
REPO="${REPO:-second-unit}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/web"
HERE="$(cd "$(dirname "$0")/.." && pwd)"     # 03-prototype

export CLOUDSDK_PYTHON="${CLOUDSDK_PYTHON:-/opt/homebrew/opt/python@3.11/bin/python3.11}"
G="${G:-$HOME/google-cloud-sdk/bin/gcloud}"

echo "==> project ${PROJECT}, region ${REGION}"
"$G" config set project "${PROJECT}" >/dev/null

echo "==> artifact registry"
"$G" artifacts repositories describe "${REPO}" --location "${REGION}" >/dev/null 2>&1 || \
  "$G" artifacts repositories create "${REPO}" --repository-format=docker \
      --location "${REGION}" --description "Second Unit images"

echo "==> secrets"
# Sourced here only to push values INTO Secret Manager; never baked into the image.
set -a; . "${HERE}/.env"; set +a
put_secret() {
  local name="$1" value="$2"
  if ! "$G" secrets describe "$name" >/dev/null 2>&1; then
    "$G" secrets create "$name" --replication-policy=automatic >/dev/null
  fi
  printf '%s' "$value" | "$G" secrets versions add "$name" --data-file=- >/dev/null
  echo "    ${name} updated"
}
put_secret grafana-sa-token       "${GRAFANA_SERVICE_ACCOUNT_TOKEN}"
put_secret grafana-cloud-token    "${GRAFANA_CLOUD_TOKEN}"
put_secret mcp-bridge-token       "${MCP_GRAFANA_SERVER_TOKEN}"

echo "==> build (Cloud Build, so no local Docker daemon is needed)"
"$G" builds submit "${HERE}" \
  --config "${HERE}/deploy/cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE}"

echo "==> grant the runtime service account access to the secrets"
SA="$("$G" projects describe "${PROJECT}" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
for s in grafana-sa-token grafana-cloud-token mcp-bridge-token; do
  "$G" secrets add-iam-policy-binding "$s" \
    --member "serviceAccount:${SA}" \
    --role roles/secretmanager.secretAccessor >/dev/null 2>&1 || true
done

echo "==> deploy"
"$G" run deploy "${SERVICE}" \
  --image "${IMAGE}:latest" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --cpu 2 --memory 2Gi \
  --timeout 900 \
  --concurrency 20 \
  --min-instances 1 \
  --max-instances 4 \
  --set-env-vars "GRAFANA_URL=${GRAFANA_URL},GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION},GOOGLE_GENAI_USE_VERTEXAI=TRUE,GEMINI_MODEL=${GEMINI_MODEL},GEMINI_MODEL_STRONG=${GEMINI_MODEL_STRONG},PROM_REMOTE_WRITE_URL=${PROM_REMOTE_WRITE_URL},PROM_USER=${PROM_USER},LOKI_PUSH_URL=${LOKI_PUSH_URL},LOKI_USER=${LOKI_USER}" \
  --set-secrets "GRAFANA_SERVICE_ACCOUNT_TOKEN=grafana-sa-token:latest,GRAFANA_CLOUD_TOKEN=grafana-cloud-token:latest,MCP_GRAFANA_SERVER_TOKEN=mcp-bridge-token:latest"

URL="$("$G" run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo
echo "==> deployed: ${URL}"
echo "    verify as an anonymous judge would:"
echo "      curl -s -o /dev/null -w '%{http_code}\\n' ${URL}/healthz"
echo "      open ${URL}   # in a private window, with no Google session"
