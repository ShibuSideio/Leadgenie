"""
Sideio Lead Sniper — Orchestrator V23 Entrypoint (Phase 3 — Stabilized).

HOTFIX (post-deploy 503): Phase 3 Blueprints (campaigns, leads, personas,
l0_admin, internal, settings) reference `services.shared.helpers` and
`core.config.db` which do not yet exist as importable modules. They are
defined inside the legacy main.py module.

Stabilization approach (Strangler Fig — Phase 2 stable state):
  - Phase 1 Blueprints (me, analytics, data_reads) remain live — they have
    correct imports and are smoke-tested.
  - Phase 3 Blueprints are DISABLED at registration time to prevent the
    ImportError crash loop.
  - The legacy Strangler Fig catch-all is re-enabled to serve all routes
    that Phase 3 was meant to cover — zero production regression.

Next sprint: extract helpers into core/config.py and core/helpers.py so
Phase 3 Blueprints can be re-enabled one by one.

V23 Blueprint Registry (LIVE):
  /api/me                       -> api/routers/me.py
  /api/campaigns GET            -> api/routers/data_reads.py
  /api/leads GET                -> api/routers/data_reads.py
  /api/tenant_profiles GET      -> api/routers/data_reads.py
  /api/analytics/*              -> api/routers/analytics.py

All other routes -> main_legacy.py (Strangler Fig catch-all -> main.py)
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

# Phase 1 Blueprints — stable, correct imports
from api.routers.me import bp as me_bp                  # type: ignore[import]
from api.routers.analytics import bp as analytics_bp    # type: ignore[import]
from api.routers.data_reads import bp as data_reads_bp  # type: ignore[import]

log = get_logger("orchestrator.v23")


def create_app() -> Flask:
    """Create the V23 Flask application (Phase 2 stable / hotfix state)."""
    app = Flask(__name__)

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

    @app.route("/", methods=["GET"])
    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "healthy", "version": "23.1.1", "arch": "modular-v23-hotfix"}), 200

    # ── Phase 1 Blueprints (live) ─────────────────────────────────────────────
    app.register_blueprint(me_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(data_reads_bp)

    # ── Strangler Fig: legacy catch-all for all Phase 3 routes ───────────────
    # Re-enabled post-503 hotfix. Phase 3 blueprints will be re-connected
    # once services/shared/helpers.py is extracted from main.py.
    from main_legacy import register_legacy_routes  # type: ignore[import]
    register_legacy_routes(app)

    @app.errorhandler(Exception)
    def handle_unhandled(exc: Exception):
        log.error("unhandled_exception", error=str(exc), exc_type=type(exc).__name__)
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    log.info("orchestrator_v23_started", version="23.1.1", phase="phase2-stable-hotfix")
    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
