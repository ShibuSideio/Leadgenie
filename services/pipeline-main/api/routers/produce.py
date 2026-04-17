"""
Pipeline-Main V23 — /produce Blueprint.

The Producer is the 24-hour Serper fetch + unprocessed_queue population step.
This Blueprint wraps the produce() function from the legacy module so that
main_v23.py can register it as a proper Flask Blueprint.

The actual logic remains in the legacy pipeline module during the cutover
transition. A future services/shared/ sprint will inline the helpers here.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

# Import the legacy produce function directly so we get a zero-regression shim.
# When the legacy module is eventually deleted, this import is replaced with
# the inline implementation.
try:
    from main_legacy_pipeline import _legacy_app as _legacy  # type: ignore[import]
    _produce_fn = _legacy.view_functions.get("produce")
except Exception:
    _produce_fn = None

bp = Blueprint("produce", __name__)


@bp.route("/produce", methods=["POST"])
def produce():
    """
    24-hour Serper Producer.
    Fetches search results from Serper, filters candidates, and populates
    campaigns/{id}.unprocessed_queue for the downstream Consumer (dispatch).
    """
    if _produce_fn is not None:
        # Delegate to the legacy implementation — zero business-logic change.
        return _produce_fn()

    # Fallback: should never reach here after Phase 3 legacy deletion.
    return jsonify({"error": "Producer function not available", "code": "shim_error"}), 503
