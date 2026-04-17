"""
Pipeline-Main V23 — /dispatch + /finalize Blueprint.

CIRCUIT BREAKER ACTIVE — see produce.py for rationale.
Both routes return 200 immediately with zero I/O.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

bp = Blueprint("dispatch", __name__)
log = logging.getLogger("pipeline.dispatch")


@bp.route("/dispatch", methods=["POST"])
def dispatch():
    """CIRCUIT BREAKER — proof-of-life. Zero gRPC, zero AI."""
    queue_name = request.headers.get("X-CloudTasks-QueueName", "MISSING")
    log.info("CIRCUIT_BREAKER_DISPATCH_HIT: queue=%s", queue_name)
    return jsonify({
        "status":  "circuit_breaker_ok",
        "message": "Dispatch proof-of-life: infrastructure is clear.",
        "queue":   queue_name,
    }), 200


@bp.route("/finalize", methods=["POST"])
def finalize():
    """CIRCUIT BREAKER — proof-of-life. Zero gRPC, zero AI."""
    log.info("CIRCUIT_BREAKER_FINALIZE_HIT")
    return jsonify({
        "status":  "circuit_breaker_ok",
        "message": "Finalize proof-of-life: infrastructure is clear.",
    }), 200
