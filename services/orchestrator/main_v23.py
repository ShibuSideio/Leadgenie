"""
Sideio Lead Sniper — Orchestrator V23 Entrypoint.

Thin entrypoint: registers V23 Blueprints, then attaches the V22 legacy
catch-all for any routes not yet migrated.  V22 production routes remain
100% live throughout the migration.

V23 Blueprints (migrated, smoke-tested):
  /api/me                    → api/routers/me.py
  /api/campaigns (GET)       → api/routers/data_reads.py
  /api/leads (GET)           → api/routers/data_reads.py
  /api/tenant_profiles (GET) → api/routers/data_reads.py
  /api/analytics/roi         → api/routers/analytics.py
  /api/analytics/unit-economics → api/routers/analytics.py

All other routes (L0, campaign mutations, lead updates, personas, etc.)
handled by main_legacy.py → original main.py until extracted to Blueprints.

Zero-downtime guarantee: original main.py UNTOUCHED, still the production
entrypoint until gcloud --entrypoint switch is confirmed green.
"""
from __future__ import annotations

import os
import sys

# ── sys.path bootstrap ────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICES_ROOT = os.path.dirname(_HERE)
for p in (_HERE, _SERVICES_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from flask import Flask, jsonify, make_response, request

from core.config import ALLOWED_ORIGINS  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]

from api.routers.me import bp as me_bp  # type: ignore[import]
from api.routers.analytics import bp as analytics_bp  # type: ignore[import]
from api.routers.data_reads import bp as data_reads_bp  # type: ignore[import]

log = get_logger("orchestrator.v23")


def create_app() -> Flask:
    """Create the V23 Flask application.

    Returns:
        Configured :class:`flask.Flask` instance with V23 Blueprints
        registered and V22 legacy catch-all attached as fallback.
    """
    app = Flask(__name__)

    # ── CORS (mirrors V22 exactly) ────────────────────────────────────────────
    @app.before_request
    def handle_preflight():
        if request.method == "OPTIONS":
            res = make_response()
            origin = request.headers.get("Origin", "")
            if origin in ALLOWED_ORIGINS:
                res.headers["Access-Control-Allow-Origin"]   = origin
                res.headers["Access-Control-Allow-Headers"]  = "Content-Type, Authorization"
                res.headers["Access-Control-Allow-Methods"]  = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
                res.headers["Access-Control-Max-Age"]        = "600"
                res.headers["Access-Control-Expose-Headers"] = "Content-Type, X-Request-Id"
            return res, 204

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"]   = origin
            response.headers["Access-Control-Allow-Headers"]  = "Content-Type, Authorization"
            response.headers["Access-Control-Allow-Methods"]  = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
            response.headers["Access-Control-Max-Age"]        = "600"
            response.headers["Access-Control-Expose-Headers"] = "Content-Type, X-Request-Id"
        return response

    # ── Health check ──────────────────────────────────────────────────────────
    @app.route("/", methods=["GET"])
    @app.route("/health", methods=["GET"])
    def health():
        """Cloud Run health probe endpoint."""
        return jsonify({"status": "healthy", "version": "23.0.0", "arch": "modular"}), 200

    # ── V23 Blueprints (migrated routes) ─────────────────────────────────────
    app.register_blueprint(me_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(data_reads_bp)

    # ── V22 legacy catch-all (Strangler Fig — unmigrated routes) ─────────────
    from main_legacy import register_legacy_routes  # type: ignore[import]
    register_legacy_routes(app)

    # ── Global error handler ──────────────────────────────────────────────────
    @app.errorhandler(Exception)
    def handle_unhandled(exc: Exception):
        """Return structured JSON — never raw HTML stack traces."""
        log.error("unhandled_exception", error=str(exc), exc_type=type(exc).__name__)
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    log.info("orchestrator_v23_started", version="23.0.0")
    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
