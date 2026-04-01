terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 4.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# -------------------------------------------------------------
# IAM & Services
# -------------------------------------------------------------
resource "google_project_service" "services" {
  for_each = toset([
    "run.googleapis.com",
    "cloudtasks.googleapis.com",
    "cloudscheduler.googleapis.com",
    "firestore.googleapis.com",
    "cloudfunctions.googleapis.com",
    "eventarc.googleapis.com",
    "pubsub.googleapis.com",
    "artifactregistry.googleapis.com"
  ])
  service = each.value
  disable_on_destroy = false
}

# -------------------------------------------------------------
# Secret Manager
# -------------------------------------------------------------
resource "google_secret_manager_secret" "serper_api_key" {
  secret_id = "serper_api_key"
  replication { 
    auto {} 
  }
}
resource "google_secret_manager_secret" "whatsapp_webhook_token" {
  secret_id = "whatsapp_webhook_token"
  replication { 
    auto {} 
  }
}
resource "google_secret_manager_secret" "sendgrid_api_key" {
  secret_id = "sendgrid_api_key"
  replication { 
    auto {} 
  }
}

# -------------------------------------------------------------
# Microservice Dedicated Service Accounts
# -------------------------------------------------------------
resource "google_service_account" "lead_pipeline_sa" {
  account_id   = "lead-pipeline-sa"
  display_name = "Lead Pipeline Execution Engine"
}

resource "google_service_account" "scraper_heavy_sa" {
  account_id   = "scraper-heavy-sa"
  display_name = "Playwright Execution Fallback"
}

resource "google_service_account" "whatsapp_webhook_sa" {
  account_id   = "whatsapp-webhook-sa"
  display_name = "Meta Webhook Verifier"
}

resource "google_service_account" "email_summary_sa" {
  account_id   = "email-worker-sa"
  display_name = "Daily Automated Worker"
}

resource "google_service_account" "auth_trigger_sa" {
  account_id   = "auth-trigger-sa"
  display_name = "Eventarc Auth Manager"
}

# -------------------------------------------------------------
# Strict Least Privilege Secret Bindings (IAM)
# -------------------------------------------------------------
resource "google_project_iam_member" "firestore_access_pipeline" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.lead_pipeline_sa.email}"
}

resource "google_project_iam_member" "firestore_access_webhook" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.whatsapp_webhook_sa.email}"
}

resource "google_project_iam_member" "firestore_access_email" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.email_summary_sa.email}"
}

resource "google_project_iam_member" "auth_trigger_firebase" {
  project = var.project_id
  role    = "roles/firebaseauth.admin"
  member  = "serviceAccount:${google_service_account.auth_trigger_sa.email}"
}

resource "google_project_iam_member" "auth_trigger_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.auth_trigger_sa.email}"
}
resource "google_secret_manager_secret_iam_member" "pipeline_serper_access" {
  secret_id = google_secret_manager_secret.serper_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.lead_pipeline_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "webhook_token_access" {
  secret_id = google_secret_manager_secret.whatsapp_webhook_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.whatsapp_webhook_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "email_smtp_access" {
  secret_id = google_secret_manager_secret.sendgrid_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.email_summary_sa.email}"
}

# -------------------------------------------------------------
# Monitoring, Alerts & Backups
# -------------------------------------------------------------
resource "google_monitoring_alert_policy" "cloud_run_failures" {
  display_name = "Cloud Run 5xx Errors"
  combiner     = "OR"
  conditions {
    display_name = "5xx API Rate"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }
}

resource "google_storage_bucket" "firestore_backup" {
  name          = "${var.project_id}-firestore-backups"
  location      = var.region
  force_destroy = true
  lifecycle_rule {
      condition { age = 30 }
      action { type = "Delete" }
  }
}

# -------------------------------------------------------------
# Cloud Tasks Queue
# -------------------------------------------------------------
# resource "google_cloud_tasks_queue" "pipeline_queue" {
#   name     = "lead-pipeline-queue"
#   location = var.region
#
#   rate_limits {
#     max_dispatches_per_second = 1
#     max_concurrent_dispatches = 5
#   }
#
#   retry_config {
#     max_attempts       = 3
#     min_backoff        = "10s"
#   }
#   depends_on = [google_project_service.services]
# }

# -------------------------------------------------------------
# Cloud Run Services (Placeholders for IaC; deployed via Cloud Build)
# -------------------------------------------------------------
# Services: lead-pipeline-main, scraper-heavy, whatsapp-webhook
# These are typically created first to get the URLs, but actual code deployed later.
