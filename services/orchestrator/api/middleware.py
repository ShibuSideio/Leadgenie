"""
Orchestrator — API middleware: authentication decorators and CORS guards.

``@require_auth`` and ``@require_super_admin`` replace the manual
``authenticate_request()`` call repeated in every route handler of the
monolith.  Using decorators ensures no route can accidentally be deployed
without authentication (M-6 audit finding).

Usage::

    from api.middleware import require_auth, require_super_admin

    @bp.route("/api/campaigns", methods=["GET"])
    @require_auth
    def list_campaigns(uid: str, tenant_id: str, user_role: str):
        ...

    @bp.route("/api/l0/telemetry", methods=["GET"])
    @require_super_admin
    def l0_telemetry(uid: str, tenant_id: str, user_role: str):
        ...
"""
from __future__ import annotations

import functools
from typing import Callable

from flask import request, jsonify

from core.exceptions import AuthError, ForbiddenError
from core.clients import get_db
from services.auth_service import authenticate_request


def require_auth(fn: Callable) -> Callable:
    """Authenticate the request and inject ``uid``, ``tenant_id``, ``user_role``.

    Wraps a route handler so it receives identity args as positional parameters.
    Returns HTTP 401/500 structured JSON on any auth failure.

    Args:
        fn: The route handler function to wrap.

    Returns:
        Decorated function.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method == "OPTIONS":
            return "", 204
        try:
            uid, tenant_id, user_role = authenticate_request(request, get_db())
        except AuthError as exc:
            return jsonify({"error": "Unauthorized", "message": exc.message}), exc.http_status
        except Exception as exc:
            return jsonify({"error": "Internal Error", "message": str(exc)}), 500
        return fn(uid, tenant_id, user_role, *args, **kwargs)
    return wrapper


def require_super_admin(fn: Callable) -> Callable:
    """Authenticate the request AND assert ``user_role == 'super_admin'``.

    Returns HTTP 403 if the authenticated user lacks the super_admin role.
    Layers on top of ``require_auth`` so authentication failures still
    return a clean 401.

    Args:
        fn: The route handler function to wrap.

    Returns:
        Decorated function.
    """
    @functools.wraps(fn)
    @require_auth
    def wrapper(uid: str, tenant_id: str, user_role: str, *args, **kwargs):
        if user_role != "super_admin":
            return jsonify({
                "error": "Forbidden",
                "message": "L0 access requires super_admin role.",
            }), 403
        return fn(uid, tenant_id, user_role, *args, **kwargs)
    return wrapper
