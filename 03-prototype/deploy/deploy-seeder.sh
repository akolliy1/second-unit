#!/usr/bin/env bash
# Deploy the telemetry seeder as a Cloud Run Job and start it.
#
# A JOB, not a service: it has no HTTP surface and must not be scaled by request traffic.
# Task timeout is set to the maximum so one execution streams for hours; Cloud Scheduler
# restarts it if it ever exits.
set -euo pipefail

PROJECT="${PROJECT:-second-unit-506700}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-second-unit-seeder}"
REPO="${REPO:-second-unit}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/seeder"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

export CLOUDSDK_PYTHON="${CLOUDSDK_PYTHON:-/opt/homebrew/opt/python@3.11/bin/python3.11}"
G="${G:-$HOME/google-cloud-sdk/bin/gcloud}"
"$G" config set project "${PROJECT}" >/dev/null

set -a; . "${HERE}/.env"; set +a

echo "==> build seeder image"
"$G" builds submit "${HERE}" \
  --config "${HERE}/deploy/cloudbuild-seeder.yaml" \
  --substitutions "_IMAGE=${IMAGE}"

SA="$("$G" projects describe "${PROJECT}" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

ARGS=(
  --image "${IMAGE}:latest"
  --region "${REGION}"
  --tasks 1
  --max-retries 3
  # MUST stay below the Scheduler interval, which is every 12h. At 24h each execution
  # outlived the next trigger by 12h, so two seeders wrote the same series continuously:
  # not an occasional overlap, a permanent one guaranteed by the arithmetic. Two writers
  # advancing one counter is what produced every "980 fr/min" and every demo-readiness
  # failure blamed on a counter reset. 11h50m leaves a gap before the next start.
  --task-timeout 42600
  --cpu 1 --memory 512Mi
  --set-env-vars "PROM_REMOTE_WRITE_URL=${PROM_REMOTE_WRITE_URL},PROM_USER=${PROM_USER},LOKI_PUSH_URL=${LOKI_PUSH_URL},LOKI_USER=${LOKI_USER}"
  --set-secrets "GRAFANA_CLOUD_TOKEN=grafana-cloud-token:latest"
)

if "$G" run jobs describe "${JOB}" --region "${REGION}" >/dev/null 2>&1; then
  echo "==> update existing job"
  "$G" run jobs update "${JOB}" "${ARGS[@]}"
else
  echo "==> create job"
  "$G" run jobs create "${JOB}" "${ARGS[@]}"
fi

# STOP ANY EXISTING WRITER FIRST.
#
# `run jobs execute` starts a NEW execution without touching running ones, so each deploy
# added another seeder. Three were running at once before this was noticed: three
# independent Farm objects writing the same Prometheus series with their own counters,
# which is the corruption that produced a "980 frames/min" peak on a farm that maxes near
# ten. One writer, always.
echo "==> cancelling any running execution (only one writer may exist)"
for e in $("$G" run jobs executions list --job "${JOB}" --region "${REGION}" \
             --format="value(name.basename())" 2>/dev/null); do
  running=$("$G" run jobs executions describe "$e" --region "${REGION}" \
              --format="value(status.runningCount)" 2>/dev/null)
  if [ "$running" = "1" ]; then
    echo "    cancelling $e"
    "$G" run jobs executions cancel "$e" --region "${REGION}" --quiet >/dev/null 2>&1 || true
  fi
done

echo "==> start an execution now"
"$G" run jobs execute "${JOB}" --region "${REGION}"

echo
echo "==> seeder running. Watch it with:"
echo "   gcloud run jobs executions list --job ${JOB} --region ${REGION}"
echo "   gcloud logging read 'resource.labels.job_name=\"${JOB}\"' --limit 20 --freshness=10m --format='value(textPayload)'"
echo
echo "   Then add a Scheduler trigger so it restarts if it ever stops:"
echo "   gcloud scheduler jobs create http ${JOB}-restart --location ${REGION} \\"
echo "     --schedule='0 */12 * * *' \\"
echo "     --uri='https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run' \\"
echo "     --http-method=POST --oauth-service-account-email=${SA}"
