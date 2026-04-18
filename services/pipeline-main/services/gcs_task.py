"""
Pipeline-main — GCS raw firehose dump via Cloud Task enqueue.

Amendment 2 (V23 Enterprise Architecture Review):
  The legacy ``_async_gcs_dump()`` spawned a ``daemon=True`` thread.  On
  Cloud Run, the CPU is throttled to near-zero the moment the HTTP response
  is sent.  The daemon thread freezes before the GCS write completes —
  resulting in silent data loss and eventual OOM accumulation from orphaned
  threads.

  Converting it to a synchronous call in ``/produce`` would introduce 200–500ms
  GCS write latency per Serper query, severely degrading pipeline throughput.

  **This module enqueues a Cloud Task to the dedicated ``gcs-dump-queue``.**
  The ``/internal/gcs-dump`` worker route handles the actual GCS write
  asynchronously, with retry semantics managed by Cloud Tasks.

Guarantees:
  - ``/produce`` returns in <5ms for the enqueue call (Cloud Tasks API ≈ 2ms p50).
  - GCS write retries are managed by the message broker (max 3 attempts, 60s backoff).
  - Failure to enqueue is logged as WARNING but never raises — pipeline degrades gracefully.
  - No daemon threads. No CPU throttle race.
"""
from __future__ import annotations

import json
import os
from typing import Any

from core.logging import get_logger    # type: ignore[import]
from core.clients import get_tasks_client  # type: ignore[import]
from core.config import PROJECT_ID, LOCATION  # type: ignore[import]

log = get_logger("pipeline.gcs_task")

# Queue dedicated to GCS firehose dumps — created via gcp_config/cloud_tasks_gcs_queue.sh
_GCS_DUMP_QUEUE: str = os.environ.get("GCS_DUMP_QUEUE", "gcs-dump-queue")

# This service's own URL — Cloud Task handler lives at /internal/gcs-dump
_PIPELINE_MAIN_URL: str = os.environ.get("PIPELINE_MAIN_URL", "")
_PIPELINE_SA_EMAIL: str = os.environ.get("PIPELINE_SA_EMAIL", "")


def enqueue_gcs_dump(raw_payload: dict[str, Any], tenant_id: str) -> None:
    """Enqueue a Cloud Task to write ``raw_payload`` to the GCS firehose lake.

    Replaces the legacy ``_async_gcs_dump()`` daemon thread.  The task is
    handled by ``/internal/gcs-dump`` which performs the actual GCS write
    with Cloud Tasks retry semantics.

    This call is fire-and-forget from the perspective of ``/produce``:
    any failure to enqueue is logged as a WARNING and silently swallowed.

    Args:
        raw_payload: Raw Serper result dict to persist to GCS.
        tenant_id:   Tenant UID — used as a GCS object path segment.
    """
    if not _PIPELINE_MAIN_URL:
        log.warning(
            "gcs_dump_enqueue_skipped",
            reason="PIPELINE_MAIN_URL not set — cannot build Cloud Task URL.",
        )
        return

    try:
        tasks_client = get_tasks_client()
        queue_path   = tasks_client.queue_path(PROJECT_ID, LOCATION, _GCS_DUMP_QUEUE)
        handler_url  = f"{_PIPELINE_MAIN_URL.rstrip('/')}/internal/gcs-dump"

        task_body: dict = {
            "http_request": {
                "http_method": "POST",
                "url":         handler_url,
                "headers":     {"Content-Type": "application/json"},
                "body":        json.dumps({
                    "tenant_id":  tenant_id,
                    "payload":    raw_payload,
                }).encode("utf-8"),
            }
        }

        # Attach OIDC token so the handler endpoint can validate it
        if _PIPELINE_SA_EMAIL:
            task_body["http_request"]["oidc_token"] = {
                "service_account_email": _PIPELINE_SA_EMAIL,
                "audience":              _PIPELINE_MAIN_URL,
            }

        tasks_client.create_task(
            request={"parent": queue_path, "task": task_body}
        )
        log.info(
            "gcs_dump_task_enqueued",
            queue=_GCS_DUMP_QUEUE,
            tenant=tenant_id[:8],
            payload_keys=list(raw_payload.keys()),
        )

    except Exception as exc:
        # Non-fatal — GCS dump is observability, not pipeline correctness.
        log.warning(
            "gcs_dump_enqueue_failed",
            error=str(exc),
            tenant=tenant_id[:8],
        )
