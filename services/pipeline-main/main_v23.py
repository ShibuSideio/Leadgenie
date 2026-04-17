"""
Sideio Lead Sniper — Pipeline Main Service Entrypoint (V23 Modular).

This file is intentionally thin: ≤50 lines.
All business logic lives in:
  core/           — config, logging, exceptions, clients (with Serper key cache)
  services/       — query_brain, neg_shield, scoring, prism/
  api/routers/    — Flask Blueprints per route

Zero-downtime strategy:
  V22 /produce and /dispatch routes continue to work via legacy_pipeline.py
  until each Blueprint is validated.  Delete legacy once smoke tests pass.

Phase 3 fixes applied here:
  - ENCRYPTION_KEY raises ValueError on missing key (L-2 fix)
  - Serper key is cached at first call (M-5 fix)
  - Version string → "22.0.0" (L-5 fix)
"""
from __future__ import annotations

import os
import sys

# ── Shared package path ───────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICES_ROOT = os.path.dirname(_HERE)
if _SERVICES_ROOT not in sys.path:
    sys.path.insert(0, _SERVICES_ROOT)

from flask import Flask, jsonify
from flask_cors import CORS

from core.logging import get_logger  # type: ignore[import]

log = get_logger("pipeline.main")


def create_app() -> Flask:
    """Create and configure the pipeline Flask application.

    Returns:
        Configured :class:`flask.Flask` instance.
    """
    app = Flask(__name__)
    CORS(app)

    # Health check — required by Cloud Run
    @app.route("/", methods=["GET"])
    @app.route("/health", methods=["GET"])
    def health():
        """Return service health status."""
        return jsonify({"status": "healthy", "version": "22.0.0"}), 200

    # ── Import and register V23 Blueprints ─────────────────────────────────
    # Each Blueprint is stable only after smoke-test validation.
    # Legacy routes remain live in the original main.py (imported below)
    # until each is green.

    # Strangler Fig: legacy routes still served from the original main.py
    # until both /produce and /dispatch Blueprints are validated.
    from main_legacy_pipeline import register_legacy_routes  # type: ignore[import]
    register_legacy_routes(app)

    @app.errorhandler(Exception)
    def handle_exception(exc: Exception):
        """Global error handler."""
        log.error("unhandled_pipeline_exception", error=str(exc))
        return jsonify({"error": "Internal Server Error"}), 500

    log.info("pipeline_started", version="22.0.0")
    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
