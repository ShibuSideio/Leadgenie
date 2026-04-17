"""
Orchestrator V23 — Persona Vault Blueprint.

Routes:
  GET    /api/personas                 — list all personas for tenant
  POST   /api/personas                 — create persona
  PUT    /api/personas/<id>            — update + surgical cache invalidation
  DELETE /api/personas/<id>            — delete persona (blocked if active campaigns)
  POST   /api/migrate-personas         — silent legacy migration (login hook)
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from google.cloud.firestore_v1.base_query import FieldFilter

from core.config import db  # type: ignore[import]
from core.auth import require_auth  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]

bp = Blueprint("personas", __name__)
log = get_logger("orchestrator.v23.personas")


# =============================================================================
# GET /api/personas
# =============================================================================
@bp.route("/api/personas", methods=["GET"])
@require_auth
def list_personas(uid, tenant_id, user_role):
    from google.cloud import firestore
    try:
        p_docs = (
            db.collection("tenant_profiles")
              .document(tenant_id)
              .collection("personas")
              .order_by("createdAt", direction=firestore.Query.DESCENDING)
              .stream()
        )
        out = []
        for doc in p_docs:
            d = doc.to_dict() or {}
            out.append({
                "id":        doc.id,
                "name":      d.get("name", ""),
                "bio":       d.get("bio", ""),
                "keywords":  d.get("keywords", ""),
                "createdAt": d["createdAt"].isoformat() if d.get("createdAt") and hasattr(d["createdAt"], "isoformat") else None,
                "updatedAt": d["updatedAt"].isoformat() if d.get("updatedAt") and hasattr(d["updatedAt"], "isoformat") else None,
            })
        return jsonify({"status": "success", "data": out}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# POST /api/personas
# =============================================================================
@bp.route("/api/personas", methods=["POST"])
@require_auth
def create_persona(uid, tenant_id, user_role):
    from google.cloud import firestore
    data   = request.json or {}
    p_name = (data.get("name") or "").strip()
    p_bio  = (data.get("bio")  or "").strip()
    p_keys = (data.get("keywords") or "").strip()
    if not p_name or not p_bio:
        return jsonify({"error": "name and bio are required"}), 400
    _, p_ref = (
        db.collection("tenant_profiles")
          .document(tenant_id)
          .collection("personas")
          .add({
              "name": p_name, "bio": p_bio, "keywords": p_keys,
              "tenant_id": tenant_id,
              "createdAt": firestore.SERVER_TIMESTAMP,
              "updatedAt": firestore.SERVER_TIMESTAMP,
          })
    )
    log.info("persona_created", persona_id=p_ref.id, name=p_name, tenant_id=tenant_id)
    return jsonify({"status": "success", "id": p_ref.id}), 201


# =============================================================================
# PUT /api/personas/<persona_id>
# =============================================================================
@bp.route("/api/personas/<string:persona_id>", methods=["PUT"])
@require_auth
def update_persona(uid, tenant_id, user_role, persona_id):
    from google.cloud import firestore
    data   = request.json or {}
    p_name = (data.get("name") or "").strip()
    p_bio  = (data.get("bio")  or "").strip()
    p_keys = (data.get("keywords") or "").strip()
    if not p_name or not p_bio:
        return jsonify({"error": "name and bio are required"}), 400

    p_ref = (
        db.collection("tenant_profiles")
          .document(tenant_id)
          .collection("personas")
          .document(persona_id)
    )
    p_ref.set({"name": p_name, "bio": p_bio, "keywords": p_keys,
               "tenant_id": tenant_id, "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)
    log.info("persona_updated", persona_id=persona_id, name=p_name)

    # Surgical cache invalidation
    linked_camps: list = []
    try:
        linked_camps = list(
            db.collection("campaigns")
              .where(filter=FieldFilter("tenant_id",  "==", tenant_id))
              .where(filter=FieldFilter("persona_id", "==", persona_id))
              .where(filter=FieldFilter("status",     "==", "active"))
              .stream()
        )
        wiped = 0
        for camp_doc in linked_camps:
            cache_docs = list(
                db.collection("predictive_cache")
                  .where(filter=FieldFilter("campaign_id", "==", camp_doc.id))
                  .limit(200).stream()
            )
            if cache_docs:
                batch = db.batch()
                for cdoc in cache_docs:
                    batch.delete(cdoc.reference)
                batch.commit()
            wiped += len(cache_docs)
            camp_doc.reference.update({
                "persona_bio":      p_bio,
                "persona_keywords": p_keys,
                "updatedAt":        firestore.SERVER_TIMESTAMP,
            })
        log.info("persona_cache_invalidated", wiped=wiped, campaigns=len(linked_camps))
    except Exception as inv_err:
        log.warning("persona_invalidation_error", error=str(inv_err))

    return jsonify({"status": "success", "id": persona_id, "linked_campaigns": len(linked_camps)}), 200


# =============================================================================
# DELETE /api/personas/<persona_id>
# =============================================================================
@bp.route("/api/personas/<string:persona_id>", methods=["DELETE"])
@require_auth
def delete_persona(uid, tenant_id, user_role, persona_id):
    from google.cloud.firestore_v1.base_query import FieldFilter as FF
    linked = list(
        db.collection("campaigns")
          .where(filter=FF("persona_id", "==", persona_id))
          .where(filter=FF("status",     "==", "active"))
          .limit(5).stream()
    )
    if linked:
        names = [d.to_dict().get("name", d.id) for d in linked]
        return jsonify({"error": "Persona is in use by active campaigns", "campaigns": names}), 409
    (
        db.collection("tenant_profiles")
          .document(tenant_id)
          .collection("personas")
          .document(persona_id)
          .delete()
    )
    log.info("persona_deleted", persona_id=persona_id, tenant_id=tenant_id)
    return jsonify({"status": "success"}), 200


# =============================================================================
# POST /api/migrate-personas  (silent login hook — idempotent)
# =============================================================================
@bp.route("/api/migrate-personas", methods=["POST"])
@require_auth
def migrate_personas(uid, tenant_id, user_role):
    from google.cloud import firestore
    try:
        personas_ref = (
            db.collection("tenant_profiles")
              .document(tenant_id)
              .collection("personas")
        )
        existing = list(personas_ref.limit(1).stream())
        if existing:
            return jsonify({"migrated": False, "reason": "personas_exist"}), 200

        profile_doc = db.collection("tenant_profiles").document(tenant_id).get()
        if not profile_doc.exists:
            return jsonify({"migrated": False, "reason": "no_profile"}), 200

        profile         = profile_doc.to_dict() or {}
        legacy_bio      = (profile.get("bio") or "").strip()
        legacy_keywords = (profile.get("keywords") or "").strip()

        if not legacy_bio:
            return jsonify({"migrated": False, "reason": "no_bio"}), 200

        _, p_ref = personas_ref.add({
            "name":       "Default Persona (Legacy)",
            "bio":        legacy_bio,
            "keywords":   legacy_keywords,
            "tenant_id":  tenant_id,
            "is_legacy":  True,
            "createdAt":  firestore.SERVER_TIMESTAMP,
            "updatedAt":  firestore.SERVER_TIMESTAMP,
        })
        log.info("legacy_persona_migrated", persona_id=p_ref.id, tenant_id=tenant_id)
        return jsonify({"migrated": True, "persona_id": p_ref.id, "name": "Default Persona (Legacy)"}), 200

    except Exception as e:
        log.error("persona_migration_error", tenant_id=tenant_id, error=str(e))
        return jsonify({"migrated": False, "error": str(e)}), 500
