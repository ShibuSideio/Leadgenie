"""
Orchestrator — Authentication & Authorization service.

Extracted from the monolithic ``authenticate_request()`` in main.py.
Raises typed exceptions from ``core.exceptions`` — route handlers
convert these to structured HTTP responses.

Single Responsibility:
  This module's only job is to verify a Firebase ID token, look up the
  user document, and return ``(uid, tenant_id, user_role)``.
  It does NOT talk to BigQuery, Cloud Tasks, or the pipeline.
"""
from __future__ import annotations

from typing import Tuple

from firebase_admin import auth
from google.cloud import firestore as fs

from core.exceptions import (
    AuthError,
    TokenVerificationError,
    AccountSuspendedError,
)
from core.logging import get_logger

log = get_logger(__name__)


def authenticate_request(
    request,
    db,
) -> Tuple[str, str, str]:
    """Verify a Firebase Bearer token and return user identity.

    Reads the ``Authorization: Bearer <token>`` header, verifies it via
    the Firebase Admin SDK, and fetches the user document to resolve
    ``tenant_id`` and ``role``.

    Creates a new user document with safe defaults if this is the user's
    first API call (idempotent registration hook).

    Args:
        request: Flask ``Request`` object.
        db:      Firestore client (injected by caller).

    Returns:
        Tuple of ``(uid, tenant_id, user_role)``.

    Raises:
        TokenVerificationError: Token missing, malformed, or expired.
        AccountSuspendedError:  Account suspended by L0 Governance.
        AuthError:              Any other auth failure.
    """
    auth_header: str | None = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise TokenVerificationError(
            "Missing or incorrectly formatted Authorization header."
        )

    token = auth_header.split("Bearer ")[1]

    try:
        decoded = auth.verify_id_token(token)
    except Exception as exc:
        log.warning("token_verification_failed", error=str(exc))
        raise TokenVerificationError(
            f"Token verification failed: {exc}"
        ) from exc

    uid: str = decoded.get("uid") or ""
    if not uid:
        raise TokenVerificationError("Decoded token contains no uid.")

    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()

    if not user_doc.exists:
        # First-visit: register with safe defaults
        user_ref.set({
            "tenant_id":       uid,
            "role":            "admin",
            "email":           decoded.get("email", "unknown"),
            "is_active":       True,
            "approval_status": "pending",
            "beta_expiry":     None,
            "wallet": {"allocated_credits": 0, "consumed_credits": 0},
            "createdAt": fs.SERVER_TIMESTAMP,
        })
        log.info("new_user_registered", uid=uid[:8])
        return uid, uid, "admin"

    data = user_doc.to_dict() or {}
    tenant_id: str = data.get("tenant_id") or uid
    user_role: str = data.get("role", "admin")
    is_active: bool = data.get("is_active", True)

    # Propagate email if not yet stored
    email = decoded.get("email", "")
    if email and "email" not in data:
        user_ref.update({"email": email})

    if not is_active and user_role != "super_admin":
        raise AccountSuspendedError(
            "Account suspended by L0 Governance Protocol."
        )

    return uid, tenant_id, user_role
