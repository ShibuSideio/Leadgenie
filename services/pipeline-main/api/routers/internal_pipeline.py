"""
Pipeline-main — Internal route handlers.

Routes in this blueprint are NOT exposed via Cloud Tasks OIDC enforcement
(they run within the same Cloud Run container).  They are NOT public.
Cloud Run's own service URL restriction prevents external access.

/internal/gcs-dump — GCS firehose writer (Cloud Task handler — Amendment 2)
"""
from __future__ import annotations

import datetime
import json
import uuid

from flask import Blueprint, jsonify, request

from core.logging import get_logger  # type: ignore[import]
from core.config import GCS_FIREHOSE_BUCKET  # type: ignore[import]

bp  = Blueprint("internal_pipeline", __name__)
log = get_logger("pipeline.internal")


@bp.route("/internal/gcs-dump", methods=["POST"])
def gcs_dump():
    """Receive and write a raw Serper payload to the GCS firehose lake.

    Called by Cloud Tasks (gcs-dump-queue).  Validates a Cloud Tasks header
    for defense-in-depth (cryptographic OIDC is handled by Cloud Tasks IAM).
    Performs the actual GCS write synchronously within the worker.

    Amendment 2 — replaces ``threading.Thread(daemon=True)`` in main.py.
    Cloud Tasks handles retries (max 3, 60s backoff) if GCS is unavailable.
    """
    if not request.headers.get("X-CloudTasks-QueueName"):
        log.warning("gcs_dump_missing_queue_header", remote_addr=request.remote_addr)
        return jsonify({"error": "Forbidden"}), 403

    body = request.json or {}
    tenant_id   = body.get("tenant_id", "unknown")
    raw_payload = body.get("payload", {})

    try:
        from google.cloud import storage as gcs_lib  # type: ignore[import]

        gcs     = gcs_lib.Client()
        bucket  = gcs.bucket(GCS_FIREHOSE_BUCKET)

        date_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
        object_id = str(uuid.uuid4())
        blob_name = f"raw/{tenant_id}/{date_str}/{object_id}.json"

        dump = {
            "_dump_id":   object_id,
            "_dumped_at": datetime.datetime.utcnow().isoformat() + "Z",
            "_tenant_id": tenant_id,
            **raw_payload,
        }
        bucket.blob(blob_name).upload_from_string(
            json.dumps(dump, default=str),
            content_type="application/json",
        )
        log.info(
            "gcs_dump_complete",
            bucket=GCS_FIREHOSE_BUCKET,
            blob=blob_name,
            tenant=tenant_id[:8],
        )
        return jsonify({"status": "ok", "blob": blob_name}), 200

    except Exception as exc:
        log.error("gcs_dump_write_failed", error=str(exc), tenant=tenant_id[:8],
                  exc_info=True)
        # Return 500 so Cloud Tasks retries the write (up to 3 attempts)
        return jsonify({"error": "GCS write failed", "details": str(exc)}), 500
