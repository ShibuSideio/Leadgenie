"""
Pipeline-Main V23 — /produce Blueprint.

CIRCUIT BREAKER ACTIVE
======================
The legacy shim (from main_legacy_pipeline import _legacy_app) was importing
the entire monolith at module load time inside the Gunicorn master process.
This dragged all gRPC C-extensions (firestore, secretmanager, vertexai) into
the master before fork(), causing child workers to inherit dead gRPC channels
that deadlocked on the first .get() call.

This stub returns 200 with zero I/O so we can confirm:
  1. Cloud Tasks can reach this service (OIDC + IAM clear)
  2. Flask is running and routing correctly (no import deadlock)
  3. The frozen thread was caused by the legacy import, not infrastructure

Once a 200 is confirmed in Cloud Logging, the full inline implementation
will be wired in here without importing from main.py.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

bp = Blueprint("produce", __name__)

log = logging.getLogger("pipeline.produce")


@bp.route("/produce", methods=["POST"])
def produce():
    """
    CIRCUIT BREAKER — proof-of-life endpoint.
    Zero gRPC. Zero AI. Zero database calls.
    Returns 200 immediately to confirm infrastructure is operational.
    """
    queue_name = request.headers.get("X-CloudTasks-QueueName", "MISSING")
    log.info(
        "CIRCUIT_BREAKER_PRODUCE_HIT: request received. "
        "queue=%s remote_addr=%s content_type=%s",
        queue_name,
        request.remote_addr,
        request.content_type,
    )
    return jsonify({
        "status":  "circuit_breaker_ok",
        "message": "Proof-of-life: infrastructure is clear. Pipeline logic is stubbed.",
        "queue":   queue_name,
    }), 200
