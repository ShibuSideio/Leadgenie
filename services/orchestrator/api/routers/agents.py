"""
Orchestrator — Research Agents CRUD Router (V24.0)

Endpoints:
    POST   /api/agents          — create a research agent
    GET    /api/agents          — list agents for tenant
    PUT    /api/agents/<id>     — update agent config
    DELETE /api/agents/<id>     — delete (soft) agent
    POST   /api/agents/<id>/run — manual trigger
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

from core.auth import require_auth  # type: ignore[import]
from core.clients import get_db  # type: ignore[import]

agents_bp = Blueprint("agents", __name__)

_AGENT_FIELDS_ALLOWED = {
    "name", "prompt", "schedule", "max_results",
    "persona_id", "status", "updatedAt",
}


@agents_bp.route("/api/agents", methods=["POST"])
@require_auth
def create_agent(uid: str, tenant_id: str, user_role: str):
    """Create a new research agent."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    prompt = (body.get("prompt") or "").strip()

    if not name or not prompt:
        return jsonify({"error": "name and prompt are required"}), 400
    if len(prompt) > 1000:
        return jsonify({"error": "prompt must be under 1000 characters"}), 400

    # P0-2: Safe int() conversion for max_results with try/except
    try:
        max_results_raw = int(body.get("max_results", 10))
    except (ValueError, TypeError):
        return jsonify({"error": "max_results must be a valid integer"}), 400

    db = get_db()

    # Check agent limit per tenant (max 10)
    existing = db.collection("tenant_profiles").document(uid) \
                 .collection("agents").where("status", "!=", "deleted").limit(10).get()
    if len(list(existing)) >= 10:
        return jsonify({"error": "Maximum 10 agents per account"}), 400

    agent_data = {
        "tenant_id": uid,
        "name": name[:128],
        "prompt": prompt[:1000],
        "schedule": body.get("schedule", "weekly"),
        "max_results": min(max_results_raw, 25),
        "persona_id": (body.get("persona_id") or ""),
        "status": "active",
        "last_ran_at": None,
        "next_run_at": None,
        "total_leads_found": 0,
        "last_run_results": [],
        "createdAt": datetime.now(timezone.utc),
        "updatedAt": datetime.now(timezone.utc),
    }

    ref = db.collection("tenant_profiles").document(uid) \
            .collection("agents").add(agent_data)
    agent_id = ref[1].id

    return jsonify({"id": agent_id, **agent_data}), 201


@agents_bp.route("/api/agents", methods=["GET"])
@require_auth
def list_agents(uid: str, tenant_id: str, user_role: str):
    """List all agents for the authenticated tenant."""
    db = get_db()
    docs = db.collection("tenant_profiles").document(uid) \
             .collection("agents").where("status", "!=", "deleted").get()

    agents = []
    for doc in docs:
        d = doc.to_dict()
        d["id"] = doc.id
        # Serialize timestamps
        for ts_field in ["createdAt", "updatedAt", "last_ran_at", "next_run_at"]:
            val = d.get(ts_field)
            if val and hasattr(val, "isoformat"):
                d[ts_field] = val.isoformat()
        agents.append(d)

    return jsonify(agents), 200


@agents_bp.route("/api/agents/<agent_id>", methods=["PUT"])
@require_auth
def update_agent(uid: str, tenant_id: str, user_role: str, agent_id: str):
    """Update an existing agent."""
    body = request.get_json(force=True, silent=True) or {}
    updates = {k: v for k, v in body.items() if k in _AGENT_FIELDS_ALLOWED}

    if not updates:
        return jsonify({"error": "No valid fields to update"}), 400

    # P0-2: Safe int() conversion if max_results is being updated
    if "max_results" in updates:
        try:
            updates["max_results"] = min(int(updates["max_results"]), 25)
        except (ValueError, TypeError):
            return jsonify({"error": "max_results must be a valid integer"}), 400

    updates["updatedAt"] = datetime.now(timezone.utc)

    db = get_db()
    ref = db.collection("tenant_profiles").document(uid) \
            .collection("agents").document(agent_id)

    doc = ref.get()
    if not doc.exists:
        return jsonify({"error": "Agent not found"}), 404
    if doc.to_dict().get("tenant_id") != uid:
        return jsonify({"error": "Unauthorized"}), 403

    ref.update(updates)
    return jsonify({"ok": True}), 200


@agents_bp.route("/api/agents/<agent_id>", methods=["DELETE"])
@require_auth
def delete_agent(uid: str, tenant_id: str, user_role: str, agent_id: str):
    """Soft-delete an agent."""
    db = get_db()
    ref = db.collection("tenant_profiles").document(uid) \
            .collection("agents").document(agent_id)

    doc = ref.get()
    if not doc.exists:
        return jsonify({"error": "Agent not found"}), 404
    if doc.to_dict().get("tenant_id") != uid:
        return jsonify({"error": "Unauthorized"}), 403

    ref.update({"status": "deleted", "updatedAt": datetime.now(timezone.utc)})
    return jsonify({"ok": True}), 200


@agents_bp.route("/api/agents/<agent_id>/run", methods=["POST"])
@require_auth
def run_agent_now(uid: str, tenant_id: str, user_role: str, agent_id: str):
    """Manually trigger an agent run."""
    db = get_db()
    ref = db.collection("tenant_profiles").document(uid) \
            .collection("agents").document(agent_id)

    doc = ref.get()
    if not doc.exists:
        return jsonify({"error": "Agent not found"}), 404

    agent = doc.to_dict()
    if agent.get("tenant_id") != uid:
        return jsonify({"error": "Unauthorized"}), 403

    from services.agent_engine import run_agent
    try:
        result = run_agent(agent_id, agent, db)
        return jsonify(result), 200
    except Exception as agent_err:
        from flask import current_app
        log = current_app.logger
        import logging
        logging.getLogger("orchestrator.agents").error(
            "agent_run_failed",
            extra={"agent_id": agent_id, "error": str(agent_err)},
            exc_info=True,
        )
        # V24.4 (L9-5): Return structured error without exposing traceback
        return jsonify({"error": "Agent execution failed", "agent_id": agent_id}), 500
