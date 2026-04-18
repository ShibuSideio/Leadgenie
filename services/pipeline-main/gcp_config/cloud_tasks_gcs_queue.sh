#!/usr/bin/env bash
# =============================================================================
# Sideio V23 — GCS Dump Queue Provisioning Script
# =============================================================================
#
# Creates the dedicated Cloud Tasks queue for asynchronous GCS firehose writes.
# This queue replaces the legacy threading.Thread(daemon=True) fire-and-forget
# pattern in main.py (V23 Enterprise Amendment 2).
#
# Queue design:
#   - max-attempts = 3      (retry up to 3 times on HTTP 500 from /internal/gcs-dump)
#   - max-backoff  = 60s    (2s → 8s → 60s jitter window)
#   - max-concurrent-dispatches = 20   (matches pipeline-main Gunicorn threads=8 × 2 safety)
#   - routing: DIRECT_PATH to pipeline-main service URL
#
# Usage:
#   bash gcp_config/cloud_tasks_gcs_queue.sh
#
# Prerequisites:
#   - gcloud auth login (or Workload Identity in CI)
#   - PROJECT_ID and LOCATION env vars set, OR defaults below used
# =============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-sideio-leads-v16}"
LOCATION="${LOCATION:-asia-south1}"
QUEUE_NAME="gcs-dump-queue"

echo "[GCS-QUEUE] Provisioning Cloud Tasks queue: ${QUEUE_NAME}"
echo "[GCS-QUEUE] Project: ${PROJECT_ID} | Location: ${LOCATION}"

gcloud tasks queues create "${QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}" \
  --max-attempts=3 \
  --max-backoff=60s \
  --min-backoff=2s \
  --max-concurrent-dispatches=20 \
  --max-dispatches-per-second=10 \
  --log-sampling-ratio=1.0 \
  || echo "[GCS-QUEUE] Queue already exists — updating configuration."

# Update idempotently if it already exists
gcloud tasks queues update "${QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}" \
  --max-attempts=3 \
  --max-backoff=60s \
  --min-backoff=2s \
  --max-concurrent-dispatches=20 \
  --max-dispatches-per-second=10 \
  --log-sampling-ratio=1.0

echo "[GCS-QUEUE] ✅ Queue '${QUEUE_NAME}' ready."
echo "[GCS-QUEUE] Handler route: POST /internal/gcs-dump on pipeline-main service."
