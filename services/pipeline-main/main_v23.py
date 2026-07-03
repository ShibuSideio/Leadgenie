"""
Sideio Lead Sniper — Pipeline Main Service Entrypoint (V25.2.0).

V25.2.0 Blueprint Registry:
  /produce   -> api/routers/produce.py      [FULL IMPL — stub retired]
  /dispatch  -> api/routers/dispatch.py     [FULL IMPL — PRISM engine active v23.3.1]
  /finalize  -> api/routers/dispatch.py     [OIDC hardened]
  /harvest   -> api/routers/harvest.py      [V25.2.0 — 4-hour signal harvest cadence]

V23 Security Amendments (Enterprise Architecture Review 2026-04-18):
  1. Zero-Trust OIDC JWT validation on /produce and /dispatch via @require_tasks_oidc.
  2. GCS raw dump PURGED per EA directive — BigQuery shadow_track is the only intelligence sink.
  3. All gRPC clients via threading.Lock DCL accessors (BQ and Tasks upgraded).

V25.2.0 Additions (2026-07-03):
  + /harvest endpoint — dedicated 4-hour signal harvest (no Serper/QueryBrain).
  + signal_cluster_analyst — Gemini-powered intent clustering from BQ raw_signals.
  + signal_bq_writer — non-blocking BQ write of all scored signals (all tiers).
"""
from __future__ import annotations

import os
import sys

# Shared package path
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICES_ROOT = os.path.dirname(_HERE)
if _SERVICES_ROOT not in sys.path:
    sys.path.insert(0, _SERVICES_ROOT)

from flask import Flask, jsonify
from flask_cors import CORS

from core.logging import get_logger  # type: ignore[import]
from api.routers.produce import bp as produce_bp                        # type: ignore[import]
from api.routers.dispatch import bp as dispatch_bp                      # type: ignore[import]
from api.routers.harvest import bp as harvest_bp                        # type: ignore[import]  # V25.2.0

log = get_logger("pipeline.main")


def create_app() -> Flask:
    """Create and configure the pipeline Flask application."""
    app = Flask(__name__)
    CORS(app)

    @app.route("/", methods=["GET"])
    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "status":  "healthy",
            "version": "25.2.0",
            "arch":    "modular-v25.2-harvest-cluster-passthrough",
        }), 200

    app.register_blueprint(produce_bp)
    app.register_blueprint(dispatch_bp)
    app.register_blueprint(harvest_bp)   # V25.2.0 — 4-hour signal harvest cadence

    @app.errorhandler(Exception)
    def handle_exception(exc: Exception):
        log.error("unhandled_pipeline_exception", error=str(exc), exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500

    log.info("pipeline_started", version="25.2.0", phase="v25.2-harvest-cluster-passthrough")
    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
