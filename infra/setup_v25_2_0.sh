#!/bin/bash
# LeadGenie V25.2.0 — Infrastructure Setup
# Run AFTER deploying new code to Cloud Run
# Usage: bash infra/setup_v25_2_0.sh

set -e

PROJECT_ID=$(gcloud config get-value project)
REGION="asia-south1"

echo "=== LeadGenie V25.2.0 Infrastructure Setup ==="
echo "Project: $PROJECT_ID"
echo "Region:  $REGION"
echo ""

# 1. Create BQ tables
echo "[1/6] Creating BigQuery tables..."
bq query --use_legacy_sql=false --project_id="$PROJECT_ID" < "$(dirname "$0")/bq_v25_2_0_tables.sql"
echo "  Done."

# 2. Create SOCIAL_TOKEN_SECRET
echo "[2/6] Creating social-token-secret in Secret Manager..."
SECRET_VAL=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
echo -n "$SECRET_VAL" | gcloud secrets create social-token-secret \
  --data-file=- \
  --replication-policy=automatic \
  --project="$PROJECT_ID" 2>/dev/null || echo "  Secret already exists — skipping creation."
echo "  Done."

# 3. Grant pipeline-main SA BigQuery Editor role
echo "[3/6] Granting BigQuery Editor to pipeline-main service account..."
PIPELINE_SA=$(gcloud run services describe pipeline-main --region="$REGION" --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || echo "")
if [ -n "$PIPELINE_SA" ]; then
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$PIPELINE_SA" \
    --role="roles/bigquery.dataEditor" \
    --quiet
  echo "  Granted to $PIPELINE_SA"
else
  echo "  WARNING: Could not determine pipeline-main SA. Grant BigQuery Editor manually."
fi

# 4. Update orchestrator env with social token secret name
echo "[4/6] Updating orchestrator environment variables..."
SOCIAL_TOKEN_SECRET_NAME="projects/${PROJECT_ID}/secrets/social-token-secret/versions/latest"
gcloud run services update orchestrator \
  --region="$REGION" \
  --update-env-vars="SOCIAL_TOKEN_SECRET_NAME=${SOCIAL_TOKEN_SECRET_NAME}" \
  --quiet
echo "  Done."

# 5. Create harvest-sweep Cloud Scheduler job
echo "[5/6] Creating harvest-sweep Cloud Scheduler job (every 4 hours)..."
ORCHESTRATOR_URL=$(gcloud run services describe orchestrator --region="$REGION" --format='value(status.url)')
ORCHESTRATOR_SA=$(gcloud run services describe orchestrator --region="$REGION" --format='value(spec.template.spec.serviceAccountName)')
gcloud scheduler jobs create http harvest-sweep-job \
  --location="$REGION" \
  --schedule="0 */4 * * *" \
  --uri="${ORCHESTRATOR_URL}/api/internal/cron/harvest-sweep" \
  --http-method=POST \
  --message-body='{}' \
  --oidc-service-account-email="${ORCHESTRATOR_SA}" \
  --oidc-token-audience="${ORCHESTRATOR_URL}" \
  --project="$PROJECT_ID" 2>/dev/null || echo "  Job already exists — updating..."
echo "  Done."

# 6. YouTube API Key (manual — requires user to set their key)
echo "[6/6] YouTube API Key (MANUAL STEP)..."
echo "  Run this command with your YouTube Data API v3 key:"
echo ""
echo "  gcloud run services update pipeline-main \\"
echo "    --region=${REGION} \\"
echo "    --update-env-vars='YOUTUBE_API_KEY=<YOUR_KEY>,CLUSTER_LEAD_THRESHOLD=60,CLUSTER_LOOKBACK_HOURS=48'"
echo ""

echo "=== Setup Complete ==="
echo ""
echo "NEXT STEPS:"
echo "1. Set YOUTUBE_API_KEY (see step 6 above)"
echo "2. Deploy: gcloud run deploy pipeline-main (from Cloud Build)"
echo "3. Deploy: gcloud run deploy orchestrator (from Cloud Build)"
echo "4. Monitor: gcloud logging read 'jsonPayload.message=\"signal_harvest_complete\"' --limit=10"
