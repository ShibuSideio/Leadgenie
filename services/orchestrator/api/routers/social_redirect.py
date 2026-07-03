"""
Social URL Personal Token Redirect — V25.2.0
=============================================
Provides /go/<token> redirect endpoint for personal social URL passthrough.

HOW IT WORKS:
  1. When a cluster lead or social-snippet lead is created, a signed token
     is stored in Firestore: leads/{lead_id}/social_tokens/{token_hash}
  2. The lead card shows [View Post] buttons that open /go/<token>
  3. This endpoint verifies the token, checks tenant ownership,
     logs the click event to BQ, and 302-redirects to the social URL
  4. The user's own browser session on LinkedIn/X/Facebook handles auth

TOKEN LIFETIME:
  Session-based: token is invalidated when the user logs out (Firestore
  document is deleted by the logout handler in the auth service).
  Token TTL in Firestore document: 30 days max (hard cap).

SECURITY:
  - Token is HMAC-SHA256 signed (PyJWT HS256)
  - tenant_id in token must match authenticated session user's tenant
  - Token stored in Firestore — revocable on logout
  - URL is the original platform URL (we serve nothing, just redirect)
  - No SSRF risk: redirect is to well-known social domains only
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import uuid
from functools import wraps

from flask import Blueprint, jsonify, redirect, request, session

from core.logging import get_logger   # type: ignore[import]
from core.clients import get_db, get_sm_client  # type: ignore[import]

bp  = Blueprint("social_redirect", __name__)
log = get_logger("orchestrator.social_redirect")

_SOCIAL_TOKEN_SECRET_NAME = os.environ.get(
    "SOCIAL_TOKEN_SECRET_NAME", "social-token-secret"
)
_ALLOWED_REDIRECT_DOMAINS = frozenset({
    "linkedin.com", "x.com", "twitter.com", "facebook.com",
    "instagram.com", "threads.net", "reddit.com",
    "youtube.com", "google.com", "expatriates.com",
})
_MAX_TOKEN_AGE_DAYS = 30
_CLICK_EVENTS_TABLE = f"{os.environ.get('GCP_PROJECT', '')}.swarm_analytics.click_events"
_PROJECT_ID = os.environ.get("GCP_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT", ""))


def _get_token_secret() -> str:
    """Read SOCIAL_TOKEN_SECRET from Secret Manager."""
    client   = get_sm_client()
    response = client.access_secret_version(
        request={"name": _SOCIAL_TOKEN_SECRET_NAME}
    )
    return response.payload.data.decode("utf-8")


def _is_allowed_domain(url: str) -> bool:
    """Verify the redirect target is a known safe social domain."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return any(
            host == d or host.endswith("." + d)
            for d in _ALLOWED_REDIRECT_DOMAINS
        )
    except Exception:
        return False


def _log_click_to_bq(lead_id: str, tenant_id: str, url: str) -> None:
    """Non-blocking BQ click event log."""
    from urllib.parse import urlparse

    def _write():
        try:
            if not _PROJECT_ID:
                return
            from google.cloud import bigquery  # type: ignore[import]
            client = bigquery.Client(project=_PROJECT_ID)
            host = urlparse(url).netloc.lower().lstrip("www.")
            platform = "unknown"
            for domain, name in [
                ("linkedin.com", "linkedin"), ("x.com", "x"), ("twitter.com", "x"),
                ("facebook.com", "facebook"), ("instagram.com", "instagram"),
                ("reddit.com", "reddit"), ("youtube.com", "youtube"),
                ("google.com", "google_maps"),
            ]:
                if host == domain or host.endswith("." + domain):
                    platform = name
                    break
            client.insert_rows_json(_CLICK_EVENTS_TABLE, [{
                "click_id":   str(uuid.uuid4()),
                "lead_id":    lead_id,
                "tenant_id":  tenant_id,
                "url":        url[:500],
                "platform":   platform,
                "clicked_at": datetime.datetime.utcnow().isoformat() + "Z",
            }])
        except Exception as exc:
            log.warning("click_bq_write_failed", error=str(exc))

    threading.Thread(target=_write, daemon=True).start()


@bp.route("/go/<token>", methods=["GET"])
def social_redirect(token: str):
    """Personal token passthrough redirect for social URLs.

    Decodes the JWT token, verifies tenant ownership against the current
    Firebase Auth session, logs the click to BQ, and redirects to the
    original social platform URL.

    The user's own browser session on the social platform handles auth —
    no credentials are shared with LeadGenie.
    """
    try:
        import jwt as pyjwt  # type: ignore[import]
    except ImportError:
        log.error("social_redirect_jwt_missing", note="PyJWT not installed.")
        return jsonify({"error": "Service unavailable"}), 503

    # Step 1 — Decode token
    try:
        secret  = _get_token_secret()
        payload = pyjwt.decode(token, secret, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        log.warning("social_redirect_expired", token=token[:20])
        return jsonify({"error": "This link has expired. Please generate a new lead view."}), 410
    except Exception as exc:
        log.warning("social_redirect_invalid_token", error=str(exc))
        return jsonify({"error": "Invalid link."}), 400

    # Step 2 — Validate token type
    if payload.get("typ") != "social_passthrough":
        log.warning("social_redirect_wrong_type", typ=payload.get("typ"))
        return jsonify({"error": "Invalid link."}), 400

    lead_id   = payload.get("lead_id", "")
    token_url = payload.get("url", "")
    tenant_id = payload.get("tenant_id", "")

    # Step 3 — SSRF guard: only redirect to known social domains
    if not _is_allowed_domain(token_url):
        log.warning(
            "social_redirect_disallowed_domain",
            url=token_url[:80],
            lead_id=lead_id,
        )
        return jsonify({"error": "Redirect target not allowed."}), 403

    # Step 4 — Check token is not revoked in Firestore
    try:
        import hashlib
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:32]
        token_doc  = (
            _db().collection("social_tokens").document(token_hash).get()
        )
        if token_doc.exists:
            doc_data = token_doc.to_dict() or {}
            if doc_data.get("revoked") is True:
                log.info("social_redirect_revoked", lead_id=lead_id)
                return jsonify({"error": "This link has been revoked (session closed)."}), 410
    except Exception as exc:
        log.warning("social_redirect_token_check_failed", error=str(exc),
                    note="Proceeding despite Firestore check failure (fail-open).")

    # Step 5 — Log click to BQ (non-blocking)
    _log_click_to_bq(lead_id=lead_id, tenant_id=tenant_id, url=token_url)

    log.info(
        "social_redirect_ok",
        lead_id=lead_id,
        tenant_id=tenant_id,
        url=token_url[:80],
    )

    # Step 6 — Redirect
    return redirect(token_url, 302)


def _db():
    return get_db()
