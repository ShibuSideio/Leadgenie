"""
Pipeline-Main V23 — /dispatch + /finalize Blueprint.

The Consumer (dispatch) pops URLs from unprocessed_queue and runs the
Gemini Confidence Gate + Playwright Scraper.

The Finalize webhook receives completed scraper-heavy payloads and writes
the final lead document.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

# Shim pattern — delegates to legacy implementations for zero-regression cutover.
try:
    from main_legacy_pipeline import _legacy_app as _legacy  # type: ignore[import]
    _dispatch_fn = _legacy.view_functions.get("dispatch")
    _finalize_fn = _legacy.view_functions.get("finalize")
except Exception:
    _dispatch_fn = None
    _finalize_fn = None

bp = Blueprint("dispatch", __name__)


@bp.route("/dispatch", methods=["POST"])
def dispatch():
    """
    4-hour Consumer / Drip Processor.
    Pops 10 URLs from campaigns/{id}.unprocessed_queue, runs confidence
    tiering (Gemini), and Playwright scraping (PRISM engine).
    """
    if _dispatch_fn is not None:
        return _dispatch_fn()
    return jsonify({"error": "Dispatch function not available", "code": "shim_error"}), 503


@bp.route("/finalize", methods=["POST"])
def finalize():
    """
    Scraper-heavy webhook receiver.
    Receives fully-scraped DOM text + contact data from the async scraper,
    runs final scoring, and persists the accepted lead document.
    """
    if _finalize_fn is not None:
        return _finalize_fn()
    return jsonify({"error": "Finalize function not available", "code": "shim_error"}), 503
