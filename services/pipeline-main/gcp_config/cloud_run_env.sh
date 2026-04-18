#!/usr/bin/env bash
# =============================================================================
# Sideio V23 — pipeline-main Cloud Run Environment Variables
# =============================================================================
#
# Reference configuration for all required env vars on the pipeline-main
# Cloud Run service.  Apply with:
#
#   bash gcp_config/cloud_run_env.sh
#
# Or copy-paste the gcloud command below into your CI/CD pipeline.
# =============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-sideio-leads-v16}"
LOCATION="${LOCATION:-asia-south1}"
SERVICE_NAME="lead-pipeline-main"

# Your pipeline-main Cloud Run URL (no trailing slash).
# Obtain with: gcloud run services describe lead-pipeline-main --format='value(status.url)'
PIPELINE_MAIN_URL="${PIPELINE_MAIN_URL:-https://lead-pipeline-main-REPLACE.a.run.app}"

# Service account that Cloud Tasks uses to mint OIDC tokens for this service.
PIPELINE_SA_EMAIL="${PIPELINE_SA_EMAIL:-lead-pipeline-sa@${PROJECT_ID}.iam.gserviceaccount.com}"

echo "[ENV] Updating Cloud Run env vars for service: ${SERVICE_NAME}"

gcloud run services update "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${LOCATION}" \
  --update-env-vars="PROJECT_ID=${PROJECT_ID}" \
  --update-env-vars="LOCATION=${LOCATION}" \
  --update-env-vars="PIPELINE_MAIN_URL=${PIPELINE_MAIN_URL}" \
  --update-env-vars="PIPELINE_SA_EMAIL=${PIPELINE_SA_EMAIL}" \
  --update-env-vars="GCS_DUMP_QUEUE=gcs-dump-queue" \
  --update-env-vars="GCS_FIREHOSE_BUCKET=sideio-raw-firehose-lake" \
  --update-env-vars="CB_WINDOW_MINUTES=15"

echo "[ENV] ✅ Env vars applied to ${SERVICE_NAME}."
echo ""
echo "IMPORTANT — Secret Manager vars (set separately via --update-secrets):"
echo "  ENCRYPTION_KEY  -> projects/${PROJECT_ID}/secrets/ENCRYPTION_KEY/versions/latest"
echo "  SERPER_API_KEY  -> projects/${PROJECT_ID}/secrets/SERPER_API_KEY/versions/latest"
echo ""
echo "Example:"
echo "  gcloud run services update ${SERVICE_NAME} \\"
echo "    --project=${PROJECT_ID} --region=${LOCATION} \\"
echo "    --update-secrets=ENCRYPTION_KEY=ENCRYPTION_KEY:latest"
