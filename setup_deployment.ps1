# LeadGenie — Pre-Deployment Setup Script (PowerShell Version)
# Version: V25.2.3 | 2026-07-03
#
# PURPOSE:
#   Creates all GCP infrastructure that Cloud Build cannot create itself
#   (IAM policy bindings, service accounts, Secret Manager secrets, BQ datasets,
#   Cloud Tasks queues, Firestore TTL indexes).

$ErrorActionPreference = "Stop"

$PROJECT_ID = "lead-sniper-prod"
$REGION = "asia-south1"

Write-Host "`n╔══════════════════════════════════════════════════════════════╗"
Write-Host "║   LeadGenie Pre-Deployment Setup — V25.2.3                  ║"
Write-Host "╚══════════════════════════════════════════════════════════════╝`n"
Write-Host "  Project : $PROJECT_ID"
Write-Host "  Region  : $REGION`n"

# --- PHASE A — Enable Required APIs ---
Write-Host "═══ PHASE A: Enabling GCP APIs ═══"
gcloud config set project $PROJECT_ID --quiet

$APIS = @(
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtasks.googleapis.com",
    "secretmanager.googleapis.com",
    "bigquery.googleapis.com",
    "firestore.googleapis.com",
    "firebase.googleapis.com",
    "aiplatform.googleapis.com",
    "containerregistry.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com"
)

foreach ($API in $APIS) {
    Write-Host "  Enabling $API..."
    gcloud services enable $API --project=$PROJECT_ID --quiet
}
Write-Host "  ✓ All APIs enabled."

# --- PHASE B — Service Accounts ---
Write-Host "`n═══ PHASE B: Creating Service Accounts ═══"

function Create-SA($SA_ID, $SA_NAME) {
    $SA_EMAIL = "${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
    $exists = gcloud iam service-accounts describe $SA_EMAIL --project=$PROJECT_ID --quiet 2>$null
    if ($exists) {
        Write-Host "  ✓ $SA_ID already exists — skipping."
    } else {
        gcloud iam service-accounts create $SA_ID --display-name=$SA_NAME --project=$PROJECT_ID --quiet
        Write-Host "  ✓ Created: $SA_EMAIL"
    }
}

Create-SA "lead-pipeline-sa"    "LeadGenie — Main Pipeline Service Account"
Create-SA "scraper-heavy-sa"    "LeadGenie — Scraper Heavy Service Account"
Create-SA "shadow-learner-sa"   "LeadGenie — Shadow Learner Aggregator"
Create-SA "email-summary-sa"    "LeadGenie — Email Summary Sender"
Create-SA "whatsapp-webhook-sa" "LeadGenie — WhatsApp Webhook Receiver"

$PIPELINE_SA = "lead-pipeline-sa@${PROJECT_ID}.iam.gserviceaccount.com"
$SCRAPER_SA = "scraper-heavy-sa@${PROJECT_ID}.iam.gserviceaccount.com"
$EMAIL_SA = "email-summary-sa@${PROJECT_ID}.iam.gserviceaccount.com"
$SHADOW_SA = "shadow-learner-sa@${PROJECT_ID}.iam.gserviceaccount.com"
$PROJ_NUM = gcloud projects describe $PROJECT_ID --format='value(projectNumber)'
$CLOUDBUILD_SA = "${PROJ_NUM}@cloudbuild.gserviceaccount.com"

Write-Host "  ✓ All service accounts ready."

# --- PHASE C — IAM Bindings ---
Write-Host "`n═══ PHASE C: Granting IAM Roles ═══"

function Grant-Role($MEMBER_EMAIL, $ROLE) {
    Write-Host "  $ROLE → $MEMBER_EMAIL"
    gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:$MEMBER_EMAIL" --role=$ROLE --condition=None --quiet > $null
}

# lead-pipeline-sa
Grant-Role $PIPELINE_SA "roles/run.invoker"
Grant-Role $PIPELINE_SA "roles/datastore.user"
Grant-Role $PIPELINE_SA "roles/bigquery.jobUser"
Grant-Role $PIPELINE_SA "roles/bigquery.dataEditor"
Grant-Role $PIPELINE_SA "roles/secretmanager.secretAccessor"
Grant-Role $PIPELINE_SA "roles/cloudtasks.enqueuer"
Grant-Role $PIPELINE_SA "roles/cloudscheduler.jobRunner"
Grant-Role $PIPELINE_SA "roles/aiplatform.user"
Grant-Role $PIPELINE_SA "roles/storage.objectViewer"
Grant-Role $PIPELINE_SA "roles/iam.serviceAccountTokenCreator"

# Cloud Build SA
Grant-Role $CLOUDBUILD_SA "roles/run.admin"
Grant-Role $CLOUDBUILD_SA "roles/storage.admin"
Grant-Role $CLOUDBUILD_SA "roles/iam.serviceAccountUser"
Grant-Role $CLOUDBUILD_SA "roles/cloudscheduler.admin"
Grant-Role $CLOUDBUILD_SA "roles/cloudtasks.admin"

# --- PHASE D — Secret Manager ---
Write-Host "`n═══ PHASE D: Secret Manager Secrets ═══"

function Create-Secret($SECRET_ID, $VALUE) {
    $exists = gcloud secrets describe $SECRET_ID --project=$PROJECT_ID --quiet 2>$null
    if ($exists) {
        Write-Host "  ✓ $SECRET_ID already exists."
    } else {
        $VALUE | gcloud secrets create $SECRET_ID --data-file=- --project=$PROJECT_ID --quiet
        Write-Host "  ✓ Created: $SECRET_ID (placeholder)"
    }
}

Create-Secret "ENCRYPTION_KEY"       "REPLACE_WITH_FERNET_KEY"
Create-Secret "SERPER_API_KEY"       "REPLACE_WITH_SERPER_KEY"
Create-Secret "FIREBASE_SA_KEY"      '{"type":"service_account"}'
Create-Secret "GEMINI_API_KEY"       "REPLACE_WITH_GEMINI_KEY"

# V25.2.3: Auto-generate INTERNAL_CRON_SECRET
$secretExists = gcloud secrets describe "INTERNAL_CRON_SECRET" --project=$PROJECT_ID --quiet 2>$null
if ($secretExists) {
    Write-Host "  ✓ INTERNAL_CRON_SECRET already exists."
} else {
    $CRON_SECRET = [System.Web.Security.Membership]::GeneratePassword(32, 0)
    $CRON_SECRET = $CRON_SECRET -replace '[^\w]', 'x'
    $CRON_SECRET | gcloud secrets create "INTERNAL_CRON_SECRET" --data-file=- --project=$PROJECT_ID --quiet
    Write-Host "  ✓ Created: INTERNAL_CRON_SECRET (auto-generated)"
}

# --- PHASE E — Cloud Tasks Queue ---
Write-Host "`n═══ PHASE E: Cloud Tasks Queue ═══"
$queueExists = gcloud tasks queues describe lead-pipeline-queue --location=$REGION --project=$PROJECT_ID --quiet 2>$null
if ($queueExists) {
    Write-Host "  ✓ lead-pipeline-queue already exists."
} else {
    gcloud tasks queues create lead-pipeline-queue --location=$REGION --max-dispatches-per-second=10 --max-concurrent-dispatches=50 --max-attempts=3 --project=$PROJECT_ID --quiet
    Write-Host "  ✓ Created: lead-pipeline-queue"
}

# --- PHASE F — BigQuery Dataset ---
Write-Host "`n═══ PHASE F: BigQuery Dataset ═══"
bq mk --dataset --location="asia-south1" --description="LeadGenie RLHF analytics" "${PROJECT_ID}:swarm_analytics" 2>$null
if ($LASTEXITCODE -eq 0) { Write-Host "  ✓ Created: swarm_analytics" } else { Write-Host "  ✓ swarm_analytics already exists." }

Write-Host "`n╔══════════════════════════════════════════════════════════════╗"
Write-Host "║   SETUP COMPLETE — V25.2.3                                  ║"
Write-Host "╚══════════════════════════════════════════════════════════════╝`n"
Write-Host "  MANUAL STEPS REMAINING:"
Write-Host "  1. Update Secret Manager values with real keys (SERPER, FIREBASE_SA, etc.)"
Write-Host "  2. Re-run Cloud Build trigger."
