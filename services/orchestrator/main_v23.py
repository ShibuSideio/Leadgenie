"""
Sideio Lead Sniper — Orchestrator Service Entrypoint (V23 Modular).

This file is intentionally thin: ≤60 lines.
All business logic lives in:
  core/           — config, logging, exceptions, clients
  services/       — auth, analytics, intelligence, quota, campaigns, leads
  repositories/   — Firestore, BigQuery, Cloud Tasks DAL
  api/routers/    — Flask Blueprints (one file per domain)

Zero-downtime migration strategy (Strangler Fig):
  The legacy ``trigger_daily_sweep`` catch-all route is preserved in
  ``main_legacy.py`` as a parallel fallback during migration.
  Once each Blueprint is validated in production (smoke tests + 24h
  monitoring), delete the corresponding if-branch in main_legacy.py.
  When main_legacy.py is empty, delete it.

V22 design invariants observed:
  - All GCP clients initialised via core.clients (lazy lru_cache)
  - ENCRYPTION_KEY raises ValueError if unset (not a silent fallback)
  - CORS is handled per-Blueprint via @require_auth OPTIONS return
"""
from __future__ import annotations

import os
import sys

# ── Ensure shared/ package is on sys.path ────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICES_ROOT = os.path.dirname(_HERE)
if _SERVICES_ROOT not in sys.path:
    sys.path.insert(0, _SERVICES_ROOT)

# ── Flask app ────────────────────────────────────────────────────────────────
from flask import Flask, jsonify
from flask_cors import CORS

from core.config import ALLOWED_ORIGINS  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]

from api.routers.me import bp as me_bp  # type: ignore[import]
from api.routers.analytics import bp as analytics_bp  # type: ignore[import]
from api.routers.data_reads import bp as data_reads_bp  # type: ignore[import]

# Legacy catch-all (Strangler Fig — remove once all routes are migrated)
from main_legacy import app as legacy_app  # type: ignore[import]  # noqa: F401

log = get_logger("orchestrator.main")


def create_app() -> Flask:
    """Create and configure the Flask application.

    Returns:
        Configured :class:`flask.Flask` instance.
    """
    app = Flask(__name__)

    CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=True)

    # Register V23 Enterprise Blueprints
    app.register_blueprint(me_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(data_reads_bp)

    @app.errorhandler(Exception)
    def handle_unhandled_exception(exc: Exception):
        """Global error handler — prevents raw stack traces leaking to users."""
        log.error("unhandled_exception", error=str(exc), exc_type=type(exc).__name__)
        return jsonify({"error": "Internal Server Error"}), 500

    log.info("orchestrator_started", version="23.0.0")
    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
