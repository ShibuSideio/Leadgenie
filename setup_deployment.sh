#!/usr/bin/env bash
# =============================================================================
# LeadGenie — Pre-Deployment Setup Script
# Version: V25.2.2 | 2026-07-03
#
# PURPOSE:
#   Creates all GCP infrastructure that Cloud Build cannot create itself
#   (IAM policy bindings, service accounts, Secret Manager secrets, BQ datasets,
#   Cloud Tasks queues, Firestore TTL indexes, Scheduler email SA).
#
# USAGE:
#   chmod +x setup_deployment.sh
#   ./setup_deployment.sh
#
# IDEMPOTENT: Safe to run multiple times.
# =============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-lead-sniper-prod}"
REGION="asia-south1"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   LeadGenie Pre-Deployment Setup — V25.2.2                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Project : $PROJECT_ID"
echo "  Region  : $REGION"
echo ""

# ─── PHASE A — Enable Required APIs ───────────────────────────────────────────
echo "═══ PHASE A: Enabling GCP APIs ═══"

gcloud config set project "$PROJECT_ID" --quiet

APIS=(
  "run.googleapis.com"
  "cloudbuild.googleapis.com"
  "cloudscheduler.googleapis.com"
  "cloudtasks.googleapis.com"
  "secretmanager.googleapis.com"
  "bigquery.googleapis.com"
  "firestore.googleapis.com"
  "firebase.googleapis.com"
  "aiplatform.googleapis.com"
  "containerregistry.googleapis.com"
  "iam.googleapis.com"
  "iamcredentials.googleapis.com"
)

for API in "${APIS[@]}"; do
  echo "  Enabling $API..."
  gcloud services enable "$API" --project="$PROJECT_ID" --quiet 2>/dev/null || true
done
echo "  ✓ All APIs enabled."

# ─── PHASE B — Service Accounts ───────────────────────────────────────────────
echo ""
echo "═══ PHASE B: Creating Service Accounts ═══"

create_sa() {
  local SA_ID="$1"
  local SA_NAME="$2"
  local SA_EMAIL="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" --quiet 2>/dev/null; then
    echo "  ✓ $SA_ID already exists — skipping."
  else
    gcloud iam service-accounts create "$SA_ID" \
      --display-name="$SA_NAME" \
      --project="$PROJECT_ID" --quiet
    echo "  ✓ Created: $SA_EMAIL"
  fi
}

create_sa "lead-pipeline-sa"    "LeadGenie — Main Pipeline Service Account"
create_sa "scraper-heavy-sa"    "LeadGenie — Scraper Heavy Service Account"
create_sa "shadow-learner-sa"   "LeadGenie — Shadow Learner Aggregator"
create_sa "email-summary-sa"    "LeadGenie — Email Summary Sender"
create_sa "whatsapp-webhook-sa" "LeadGenie — WhatsApp Webhook Receiver"

PIPELINE_SA="lead-pipeline-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SCRAPER_SA="scraper-heavy-sa@${PROJECT_ID}.iam.gserviceaccount.com"
EMAIL_SA="email-summary-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SHADOW_SA="shadow-learner-sa@${PROJECT_ID}.iam.gserviceaccount.com"
PROJ_NUM=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
CLOUDBUILD_SA="${PROJ_NUM}@cloudbuild.gserviceaccount.com"

echo "  ✓ All service accounts ready."

# ─── PHASE C — IAM Bindings ───────────────────────────────────────────────────
echo ""
echo "═══ PHASE C: Granting IAM Roles ═══"

grant_role() {
  local MEMBER="$1"; local ROLE="$2"
  echo "  $ROLE → $MEMBER"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$MEMBER" --role="$ROLE" \
    --condition=None --quiet 2>/dev/null || true
}

# lead-pipeline-sa (orchestrator + pipeline-main + autonomous-engine)
grant_role "$PIPELINE_SA" "roles/run.invoker"
grant_role "$PIPELINE_SA" "roles/datastore.user"
grant_role "$PIPELINE_SA" "roles/bigquery.jobUser"
grant_role "$PIPELINE_SA" "roles/bigquery.dataEditor"
grant_role "$PIPELINE_SA" "roles/secretmanager.secretAccessor"
grant_role "$PIPELINE_SA" "roles/cloudtasks.enqueuer"
grant_role "$PIPELINE_SA" "roles/cloudscheduler.jobRunner"
grant_role "$PIPELINE_SA" "roles/aiplatform.user"
grant_role "$PIPELINE_SA" "roles/storage.objectViewer"
grant_role "$PIPELINE_SA" "roles/iam.serviceAccountTokenCreator"

# scraper-heavy-sa
grant_role "$SCRAPER_SA" "roles/datastore.user"
grant_role "$SCRAPER_SA" "roles/secretmanager.secretAccessor"

# shadow-learner-sa
grant_role "$SHADOW_SA" "roles/bigquery.jobUser"
grant_role "$SHADOW_SA" "roles/bigquery.dataEditor"
grant_role "$SHADOW_SA" "roles/datastore.user"
grant_role "$SHADOW_SA" "roles/secretmanager.secretAccessor"

# email-summary-sa
grant_role "$EMAIL_SA" "roles/secretmanager.secretAccessor"
grant_role "$EMAIL_SA" "roles/datastore.user"

# Cloud Build SA
grant_role "$CLOUDBUILD_SA" "roles/run.admin"
grant_role "$CLOUDBUILD_SA" "roles/storage.admin"
grant_role "$CLOUDBUILD_SA" "roles/iam.serviceAccountUser"
grant_role "$CLOUDBUILD_SA" "roles/cloudscheduler.admin"
grant_role "$CLOUDBUILD_SA" "roles/cloudtasks.admin"

# Cloud Build → impersonate pipeline-sa
gcloud iam service-accounts add-iam-policy-binding "$PIPELINE_SA" \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/iam.serviceAccountUser" \
  --project="$PROJECT_ID" --quiet 2>/dev/null || true

# Cloud Tasks OIDC → pipeline-main (run AGAIN after first deploy)
gcloud run services add-iam-policy-binding lead-pipeline-main \
  --region="$REGION" \
  --member="serviceAccount:$PIPELINE_SA" \
  --role="roles/run.invoker" \
  --project="$PROJECT_ID" --quiet 2>/dev/null || \
  echo "  (lead-pipeline-main not yet deployed — re-run after first Cloud Build)"

# Cloud Scheduler OIDC → orchestrator (V25.2.2: required for inbound radar cron)
gcloud run services add-iam-policy-binding orchestrator \
  --region="$REGION" \
  --member="serviceAccount:$PIPELINE_SA" \
  --role="roles/run.invoker" \
  --project="$PROJECT_ID" --quiet 2>/dev/null || \
  echo "  (orchestrator not yet deployed — re-run after first Cloud Build)"

echo "  ✓ All IAM roles granted."

# ─── PHASE D — Secret Manager ─────────────────────────────────────────────────
echo ""
echo "═══ PHASE D: Secret Manager Secrets ═══"

create_secret() {
  local SECRET_ID="$1"; local PLACEHOLDER="$2"
  if gcloud secrets describe "$SECRET_ID" --project="$PROJECT_ID" --quiet 2>/dev/null; then
    echo "  ✓ $SECRET_ID already exists."
  else
    echo -n "$PLACEHOLDER" | gcloud secrets create "$SECRET_ID" \
      --data-file=- --project="$PROJECT_ID" --quiet
    echo "  ✓ Created: $SECRET_ID (placeholder — update with real value)"
  fi
}

create_secret "ENCRYPTION_KEY"       "REPLACE_WITH_FERNET_KEY"
create_secret "SERPER_API_KEY"       "REPLACE_WITH_SERPER_KEY"
create_secret "FIREBASE_SA_KEY"      '{"type":"service_account"}'
create_secret "GEMINI_API_KEY"       "REPLACE_WITH_GEMINI_KEY"

# V25.2.2: Auto-generate INTERNAL_CRON_SECRET for inbound radar cron auth
if gcloud secrets describe "INTERNAL_CRON_SECRET" --project="$PROJECT_ID" --quiet 2>/dev/null; then
  echo "  ✓ INTERNAL_CRON_SECRET already exists."
else
  CRON_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || \
                openssl rand -hex 32)
  echo -n "$CRON_SECRET" | gcloud secrets create "INTERNAL_CRON_SECRET" \
    --data-file=- --project="$PROJECT_ID" --quiet
  echo "  ✓ Created: INTERNAL_CRON_SECRET (auto-generated)"
fi

echo ""
echo "  ⚠️  UPDATE THESE WITH REAL VALUES:"
echo "       gcloud secrets versions add ENCRYPTION_KEY --data-file=<path>"
echo "       gcloud secrets versions add SERPER_API_KEY --data-file=<path>"
echo "       gcloud secrets versions add FIREBASE_SA_KEY --data-file=firebase-sa.json"

# ─── PHASE E — Cloud Tasks Queue ──────────────────────────────────────────────
echo ""
echo "═══ PHASE E: Cloud Tasks Queue ═══"

if gcloud tasks queues describe lead-pipeline-queue \
    --location="$REGION" --project="$PROJECT_ID" --quiet 2>/dev/null; then
  echo "  ✓ lead-pipeline-queue already exists."
else
  gcloud tasks queues create lead-pipeline-queue \
    --location="$REGION" \
    --max-dispatches-per-second=10 \
    --max-concurrent-dispatches=50 \
    --max-attempts=3 \
    --min-backoff=10s \
    --max-backoff=300s \
    --project="$PROJECT_ID" --quiet
  echo "  ✓ Created: lead-pipeline-queue"
fi

# ─── PHASE F — BigQuery Dataset ───────────────────────────────────────────────
echo ""
echo "═══ PHASE F: BigQuery Dataset ═══"

bq mk --dataset --location="asia-south1" \
  --description="LeadGenie RLHF analytics" \
  "${PROJECT_ID}:swarm_analytics" 2>/dev/null || echo "  ✓ swarm_analytics already exists."

# Apply BQ DDL if available
if [ -f "infra/bq_v25_2_0_tables.sql" ]; then
  echo "  Applying BQ schema DDL..."
  envsubst < infra/bq_v25_2_0_tables.sql | \
    bq query --project_id="$PROJECT_ID" --use_legacy_sql=false --quiet 2>/dev/null || \
    echo "  ⚠️  Run infra/bq_v25_2_0_tables.sql manually if needed."
fi

# ─── PHASE G — Firestore TTL Policies ─────────────────────────────────────────
echo ""
echo "═══ PHASE G: Firestore TTL Policies ═══"

set_ttl() {
  local COLLECTION="$1"; local FIELD="$2"
  echo "  TTL: $COLLECTION.$FIELD..."
  gcloud firestore fields ttls update "$FIELD" \
    --collection-group="$COLLECTION" \
    --enable-ttl --project="$PROJECT_ID" --quiet 2>/dev/null || \
    echo "  ⚠️  Set manually: GCP Console → Firestore → Indexes → TTL → $COLLECTION.$FIELD"
}

set_ttl "leads"             "expire_at"
set_ttl "predictive_cache"  "expire_at"
set_ttl "inbound_signals"   "expire_at"   # V25.2.2: 30-day signal TTL
set_ttl "global_lead_locks" "expire_at"   # V25.2.2: 3-day lock TTL

echo "  ✓ TTL policies done."

# ─── PHASE H — Inject Env Vars into Orchestrator ──────────────────────────────
echo ""
echo "═══ PHASE H: Injecting Env Vars into Orchestrator ═══"

LIVE_SECRET=$(gcloud secrets versions access latest \
  --secret="INTERNAL_CRON_SECRET" --project="$PROJECT_ID" 2>/dev/null || echo "")

if [ -n "$LIVE_SECRET" ]; then
  gcloud run services update orchestrator \
    --region="$REGION" --platform=managed \
    --update-env-vars="INTERNAL_CRON_SECRET=${LIVE_SECRET},SCHEDULER_SA_EMAIL=${PIPELINE_SA}" \
    --project="$PROJECT_ID" --quiet 2>/dev/null || \
    echo "  ⚠️  orchestrator not deployed yet — re-run after first Cloud Build"
  echo "  ✓ INTERNAL_CRON_SECRET + SCHEDULER_SA_EMAIL injected."
else
  echo "  ⚠️  Could not read secret. After deploy, run:"
  echo "    gcloud run services update orchestrator --region=$REGION \\"
  echo "      --update-env-vars=\"INTERNAL_CRON_SECRET=\$(gcloud secrets versions access latest --secret=INTERNAL_CRON_SECRET),SCHEDULER_SA_EMAIL=$PIPELINE_SA\""
fi

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   SETUP COMPLETE — V25.2.2                                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  MANUAL STEPS REMAINING:"
echo "  1. Update placeholder Secret Manager values with real API keys"
echo "  2. Set _ORCHESTRATOR_URL in Cloud Build Trigger configuration"
echo "  3. Confirm .firebaserc project = lead-sniper-prod"
echo "  4. Re-run this script after first Cloud Build deploy to bind Cloud Run OIDC"
echo ""
echo "  VERIFICATION:"
echo "    gcloud run services list --region=$REGION --project=$PROJECT_ID"
echo "    gcloud scheduler jobs list --location=$REGION --project=$PROJECT_ID"
echo "    gcloud secrets list --project=$PROJECT_ID"
echo ""
echo "  Done: $(date '+%Y-%m-%d %H:%M:%S')"
