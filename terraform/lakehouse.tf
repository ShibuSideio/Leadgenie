# =============================================================================
# V18 Data Lakehouse: Terraform additions
# Appended to terraform/main.tf
# Resources:
#   1. GCS raw firehose bucket (14-day lifecycle — GDPR mandate)
#   2. BigQuery dataset: swarm_analytics
#   3. BigQuery table: rlhf_events (with schema)
#   4. Cloud Scheduler job: shadow-learner-aggregator (every 12h)
#   5. Service Account: shadow-learner-sa
#   6. IAM bindings: BQ + GCS roles on lead-pipeline-sa and shadow-learner-sa
# =============================================================================

# Enable required APIs
resource "google_project_service" "lakehouse_apis" {
  for_each = toset([
    "bigquery.googleapis.com",
    "storage.googleapis.com",
    "cloudscheduler.googleapis.com",
    "run.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ── 1. GCS Raw Firehose Data Lake ─────────────────────────────────────────────
resource "google_storage_bucket" "raw_firehose_lake" {
  name          = "sideio-raw-firehose-lake"
  location      = var.region
  force_destroy = false

  # Uniform bucket-level access (no per-object ACLs)
  uniform_bucket_level_access = true

  # 14-day auto-purge — GDPR / DPDP compliance mandate (raw noise, pre-filter)
  lifecycle_rule {
    condition { age = 14 }
    action    { type = "Delete" }
  }

  labels = {
    env     = "production"
    purpose = "raw-swarm-firehose"
  }
}

# ── 2. BigQuery Dataset ────────────────────────────────────────────────────────
resource "google_bigquery_dataset" "swarm_analytics" {
  dataset_id                  = "swarm_analytics"
  friendly_name               = "Sideio Swarm Analytics"
  description                 = "RLHF telemetry and intent signal warehouse for Shadow Learner"
  location                    = "asia-south1"
  delete_contents_on_destroy  = false

  labels = {
    env = "production"
  }
}

# ── 3. BigQuery Table: rlhf_events ────────────────────────────────────────────
resource "google_bigquery_table" "rlhf_events" {
  dataset_id = google_bigquery_dataset.swarm_analytics.dataset_id
  table_id   = "rlhf_events"
  description = "Structured RLHF conversion telemetry events, streamed from Orchestrator"

  schema = jsonencode([
    {
      name = "event_id"
      type = "STRING"
      mode = "REQUIRED"
      description = "UUID for this RLHF event (idempotency key)"
    },
    {
      name = "timestamp"
      type = "TIMESTAMP"
      mode = "REQUIRED"
      description = "UTC timestamp of the status-change event"
    },
    {
      name = "tenant_id"
      type = "STRING"
      mode = "REQUIRED"
      description = "Firebase UID / tenant identifier"
    },
    {
      name = "prism_mode"
      type = "STRING"
      mode = "NULLABLE"
      description = "Prism Engine mode: GeneralDomain | WalledGarden | B2B2C"
    },
    {
      name = "conversion_status"
      type = "STRING"
      mode = "REQUIRED"
      description = "Lead status at the time of event: contacted | converted | ignored | won | lost"
    },
    {
      name = "intent_hash"
      type = "STRING"
      mode = "NULLABLE"
      description = "SHA-256 hash of (intent_signal + sourcing_vector) — anonymized signal fingerprint"
    },
    {
      name = "raw_signal_payload"
      type = "JSON"
      mode = "NULLABLE"
      description = "Stripped lead signal payload (no PII): score, sourcing_vector, tech_stack, hiring_intent"
    }
  ])

  time_partitioning {
    type  = "DAY"
    field = "timestamp"
  }

  depends_on = [google_bigquery_dataset.swarm_analytics]
}

# ── 4. Service Account: Shadow Learner Aggregator ─────────────────────────────
resource "google_service_account" "shadow_learner_sa" {
  account_id   = "shadow-learner-sa"
  display_name = "Shadow Learner Aggregator Job"
  description  = "Runs the 12-hour BigQuery aggregation and writes global_swarm_weights to Firestore"
}

# ── 5. IAM: lead-pipeline-sa → GCS objectCreator + BQ dataEditor ─────────────
resource "google_project_iam_member" "pipeline_gcs_writer" {
  project = var.project_id
  role    = "roles/storage.objectCreator"
  member  = "serviceAccount:${google_service_account.lead_pipeline_sa.email}"
}

resource "google_project_iam_member" "pipeline_bq_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.lead_pipeline_sa.email}"
}

# orchestrator-sa (compute default SA used by Cloud Run) → BQ dataEditor
# Cloud Run jobs typically use the Compute Engine default SA unless overridden.
# We bind the explicit shadow-learner-sa here:
resource "google_project_iam_member" "shadow_learner_bq_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.shadow_learner_sa.email}"
}

resource "google_project_iam_member" "shadow_learner_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.shadow_learner_sa.email}"
}

resource "google_project_iam_member" "shadow_learner_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.shadow_learner_sa.email}"
}

# orchestrator SA also needs BQ streaming access (for async telemetry push)
# Bind to the lead-pipeline-sa (same SA used by orchestrator Cloud Run)
resource "google_project_iam_member" "orchestrator_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.lead_pipeline_sa.email}"
}

# ── 6. Cloud Scheduler: trigger shadow-learner-aggregator every 12h ───────────
resource "google_cloud_scheduler_job" "shadow_learner_trigger" {
  name        = "shadow-learner-12h-trigger"
  description = "Triggers the Shadow Learner Aggregator Cloud Run Job every 12 hours"
  schedule    = "0 */12 * * *"
  time_zone   = "UTC"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/shadow-learner-aggregator:run"

    oauth_token {
      service_account_email = google_service_account.shadow_learner_sa.email
    }
  }

  depends_on = [google_project_service.services]
}
