"""
Sideio Lead Sniper — Orchestrator V23.4 Entrypoint.

All routes are served by V23 Blueprints. main_legacy.py is permanently retired.

V23.4 additions (2026-04-20):
  + POST /api/internal/telemetry/serper-audit  → internal.py  (BQ telemetry broker)
  + GET  /api/admin/telemetry/serper-logs      → serper_telemetry.py  (audit read)
  + POST /api/analyze-website: WAF fail-fast   → settings.py  (httpx 7s + 422 WAF_BLOCKED)
  + GET  /api/l0/shadow-ledger: 500 fixed      → IAM: roles/bigquery.dataViewer granted

Blueprint Registry:
  /api/me, /health                      -> api/routers/me.py
  /api/analytics/*                      -> api/routers/analytics.py
  /api/campaigns* (GET)                 -> api/routers/data_reads.py
  /api/tenant_profiles (GET)            -> api/routers/data_reads.py
  /api/campaigns* (POST/PUT/DELETE/ignite/consume/run)
                                        -> api/routers/campaigns.py
  /api/leads/<id> (PUT)                 -> api/routers/leads.py
  /api/personas*                        -> api/routers/personas.py
  /api/l0/*                             -> api/routers/l0_admin.py
  /api/internal/*                       -> api/routers/internal.py
  /api/admin/telemetry/serper-logs      -> api/routers/serper_telemetry.py
  /api/settings, /api/tenant_profiles (POST), /api/analyze-website
                                        -> api/routers/settings.py
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

# ── Phase 1 Blueprints (stable) ───────────────────────────────────────────────
from api.routers.me import bp as me_bp                  # type: ignore[import]
from api.routers.analytics import bp as analytics_bp    # type: ignore[import]
from api.routers.data_reads import bp as data_reads_bp  # type: ignore[import]

# ── Phase 3 Blueprints (newly fixed) ─────────────────────────────────────────
from api.routers.campaigns import bp as campaigns_bp    # type: ignore[import]
from api.routers.leads import bp as leads_bp            # type: ignore[import]
from api.routers.personas import bp as personas_bp      # type: ignore[import]
from api.routers.l0_admin import bp as l0_admin_bp      # type: ignore[import]
from api.routers.internal import bp as internal_bp      # type: ignore[import]
from api.routers.settings import bp as settings_bp      # type: ignore[import]
from api.routers.serper_telemetry import bp as serper_telemetry_bp  # type: ignore[import]

log = get_logger("orchestrator.v23")


def create_app() -> Flask:
    """Create the V23 Flask application — fully modular, no legacy."""
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
        return jsonify({"status": "healthy", "version": "23.4.0", "arch": "modular-v23.4-serper-telemetry"}), 200

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    app.register_blueprint(me_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(data_reads_bp)

    # ── Phase 3 ───────────────────────────────────────────────────────────────
    app.register_blueprint(campaigns_bp)
    app.register_blueprint(leads_bp)
    app.register_blueprint(personas_bp)
    app.register_blueprint(l0_admin_bp)
    app.register_blueprint(internal_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(serper_telemetry_bp)

    @app.errorhandler(Exception)
    def handle_unhandled(exc: Exception):
        log.error("unhandled_exception", error=str(exc), exc_type=type(exc).__name__)
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    log.info("orchestrator_v23_started", version="23.4.0", phase="v23.4-serper-telemetry-waf-hardening")
    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
