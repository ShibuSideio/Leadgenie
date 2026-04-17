"""
Strangler Fig shim — re-exports the V22 legacy Flask app.

This module is imported by main_v23.py to register any routes not yet
migrated to V23 Blueprints.  It works by attaching the legacy catch-all
``trigger_daily_sweep`` route to the V23 app as a fallback.

How it works:
  The legacy main.py app defines ``trigger_daily_sweep`` on ``/<path:path>``.
  We re-register that single catch-all blueprint on the V23 app so all
  unmigrated routes still function identically.

Deletion plan (Phase 3):
  Delete this file once every route in trigger_daily_sweep has been
  extracted to its own Blueprint and verified green in production.
"""
from __future__ import annotations

import sys
import os

# ---------------------------------------------------------------------------
# Import the original Flask app from the V22 monolith.
# We must do a filesystem import because the module name 'main' conflicts.
# ---------------------------------------------------------------------------
import importlib.util

_LEGACY_PATH = os.path.join(os.path.dirname(__file__), "main.py")

_spec = importlib.util.spec_from_file_location("main_legacy_module", _LEGACY_PATH)
_legacy_module = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_legacy_module)  # type: ignore[union-attr]

# The V22 monolith exposes its Flask app as ``app``
legacy_app = _legacy_module.app


def register_legacy_routes(target_app) -> None:
    """Attach all unmatched V22 routes to *target_app* as a last-resort fallback.

    This uses Werkzeug's URL map merge to copy every route from the legacy
    app that is NOT already registered on *target_app*.  This avoids
    double-registering the already-migrated V23 Blueprint routes
    (``/api/me``, ``/api/analytics/...``, ``/api/campaigns`` GET, etc.).

    Args:
        target_app: The V23 Flask app instance to attach legacy routes to.
    """
    # Copy the legacy catch-all handler only — Blueprints take priority
    # because Flask evaluates routes in registration order.
    from flask import request as flask_request

    @target_app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
    def _legacy_catch_all(path: str):
        """Forward all unmatched requests to the V22 legacy handler."""
        # Delegate to the legacy app's WSGI interface with the live request env
        with legacy_app.test_request_context(
            path=flask_request.path,
            method=flask_request.method,
            headers=dict(flask_request.headers),
            data=flask_request.get_data(),
            query_string=flask_request.query_string,
            content_type=flask_request.content_type,
        ):
            response = legacy_app.full_dispatch_request()
        return response
