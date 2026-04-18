"""
Pipeline-main — Zero-Trust OIDC task validation middleware.

Cloud Run services with ``--no-allow-unauthenticated`` enforce IAM at the
infrastructure level.  This decorator adds a **cryptographic application-layer
defence** on top: it verifies the OIDC JWT issued by the pipeline service
account and rejects any request whose token is absent, expired, or issued for
the wrong audience/issuer.

Amendment 1 (V23 Enterprise Architecture Review):
  Checking ``X-CloudTasks-QueueName`` alone is insufficient.  The header is
  trivially forgeable by any caller who can reach the Cloud Run service URL.
  We must cryptographically verify the JWT signed by Google's OIDC service.

Usage::

    from middleware.oidc import require_tasks_oidc

    @bp.route("/produce", methods=["POST"])
    @require_tasks_oidc
    def produce():
        ...

Environment variables (must be set in Cloud Run console):
  PIPELINE_SA_EMAIL  — the service account the orchestrator uses to mint tokens
                       (e.g. ``lead-pipeline-sa@<project>.iam.gserviceaccount.com``)
  PIPELINE_MAIN_URL  — this service's Cloud Run base URL (the OIDC audience,
                       e.g. ``https://lead-pipeline-main-abc-uc.a.run.app``)
"""
from __future__ import annotations

import os
import functools
from typing import Callable

from flask import request, jsonify

from core.logging import get_logger  # type: ignore[import]

log = get_logger("pipeline.middleware.oidc")

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
_PIPELINE_SA_EMAIL: str = os.environ.get("PIPELINE_SA_EMAIL", "")
_PIPELINE_MAIN_URL: str = os.environ.get("PIPELINE_MAIN_URL", "")

# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------

def _verify_token(token: str) -> tuple[bool, str]:
    """Cryptographically verify a Google OIDC JWT.

    Uses ``google.oauth2.id_token.verify_oauth2_token`` which:
      1. Fetches Google's public certificates (cached for ~1 hour).
      2. Verifies the RSA signature.
      3. Validates ``iss``, ``aud``, and ``exp`` claims.

    Args:
        token: Raw Bearer token string from Authorization header.

    Returns:
        ``(True, "")`` on success.
        ``(False, reason_string)`` on any failure.
    """
    if not _PIPELINE_MAIN_URL:
        log.critical(
            "oidc_audience_missing",
            message="PIPELINE_MAIN_URL env var not set — cannot validate OIDC audience.",
            resolution="Set PIPELINE_MAIN_URL to this service's Cloud Run URL in the console.",
        )
        return False, "PIPELINE_MAIN_URL not configured"

    try:
        from google.oauth2 import id_token as _id_token  # type: ignore[import]
        from google.auth.transport import requests as _g_requests  # type: ignore[import]

        request_obj = _g_requests.Request()
        claims = _id_token.verify_oauth2_token(token, request_obj, _PIPELINE_MAIN_URL)

        # Validate issuer
        if claims.get("iss") not in (
            "https://accounts.google.com",
            "accounts.google.com",
        ):
            return False, f"Invalid issuer: {claims.get('iss')}"

        # Validate service account email (if configured)
        if _PIPELINE_SA_EMAIL:
            token_email = claims.get("email", "")
            if token_email != _PIPELINE_SA_EMAIL:
                log.warning(
                    "oidc_wrong_service_account",
                    expected=_PIPELINE_SA_EMAIL,
                    received=token_email,
                )
                return False, f"Wrong service account: {token_email}"

        return True, ""

    except Exception as exc:
        log.warning("oidc_token_verification_failed", error=str(exc))
        return False, str(exc)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def require_tasks_oidc(fn: Callable) -> Callable:
    """Decorator: reject requests without a valid Cloud Tasks OIDC token.

    Checks (in order):
      1. ``Authorization: Bearer <token>`` header present.
      2. Token cryptographically valid for this service's audience.
      3. Queue name header present (defense-in-depth, not auth).

    Returns:
      HTTP 401 on missing / invalid token.
      HTTP 403 if queue name header is absent (after token passes).

    Args:
        fn: The Flask route function to protect.

    Returns:
        Wrapped function that performs auth before calling ``fn``.
    """
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        # ── Step 1: Extract bearer token ──────────────────────────────────────
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            log.warning(
                "oidc_missing_token",
                path=request.path,
                remote_addr=request.remote_addr,
                note="Request rejected: no Authorization Bearer header.",
            )
            return jsonify({
                "error": "Unauthorized",
                "code":  "MISSING_OIDC_TOKEN",
                "message": "Authorization: Bearer <token> header is required.",
            }), 401

        token = auth_header[len("Bearer "):].strip()

        # ── Step 2: Cryptographic verification ────────────────────────────────
        valid, reason = _verify_token(token)
        if not valid:
            log.warning(
                "oidc_token_rejected",
                path=request.path,
                reason=reason,
                remote_addr=request.remote_addr,
            )
            return jsonify({
                "error":  "Unauthorized",
                "code":   "INVALID_OIDC_TOKEN",
                "message": reason,
            }), 401

        # ── Step 3: Queue name header (defense-in-depth) ──────────────────────
        if not request.headers.get("X-CloudTasks-QueueName"):
            log.warning(
                "tasks_queue_header_missing",
                path=request.path,
                remote_addr=request.remote_addr,
                note="OIDC valid but X-CloudTasks-QueueName absent — possible replay attack.",
            )
            return jsonify({
                "error":   "Forbidden",
                "code":    "MISSING_QUEUE_HEADER",
                "message": "X-CloudTasks-QueueName header required.",
            }), 403

        return fn(*args, **kwargs)

    return _wrapper
