"""
Orchestrator V23 — Settings & Tenant Profiles Blueprint.

Routes:
  POST /api/settings                    — BYOT WhatsApp token vault
  POST /api/tenant_profiles             — upsert tenant master twin profile
  POST /api/tenant_profiles/extract-kb  — extract & append PDF/TXT knowledge base
  POST /api/analyze-website             — AI website scraper + persona extractor
"""
from __future__ import annotations

import io
import json
import os
import re

from flask import Blueprint, jsonify, request

from core.config import db, PROJECT_ID  # type: ignore[import]
from core.auth import require_auth  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]

import httpx

bp = Blueprint("settings", __name__)
log = get_logger("orchestrator.v23.settings")


# =============================================================================
# POST /api/settings  (BYOT Vault — WhatsApp token encryption)
# =============================================================================
@bp.route("/api/settings", methods=["POST"])
@require_auth
def update_settings(uid, tenant_id, user_role):
    from google.cloud import firestore, secretmanager, kms
    import base64

    data         = request.json or {}
    user_ref     = db.collection("users").document(uid)
    wa_token_raw = data.get("wa_token")
    wa_phone_id  = data.get("wa_phone_id")
    admin_phone  = data.get("admin_phone")

    settings_update: dict = {}
    if wa_phone_id:
        settings_update["wa_phone_id"] = wa_phone_id
    if admin_phone:
        settings_update["admin_phone"] = admin_phone

    if wa_token_raw:
        try:
            sm_client = secretmanager.SecretManagerServiceClient()
            project_id_conf = os.environ.get("PROJECT_ID", PROJECT_ID)
            key_name = sm_client.access_secret_version(
                request={"name": f"projects/{project_id_conf}/secrets/kms_wa_key_path/versions/latest"}
            ).payload.data.decode("UTF-8").strip()
            kms_client    = kms.KeyManagementServiceClient()
            response      = kms_client.encrypt(request={"name": key_name, "plaintext": wa_token_raw.encode("utf-8")})
            encrypted_tok = base64.b64encode(response.ciphertext).decode("utf-8")
            settings_update["wa_token"] = encrypted_tok
        except Exception as e:
            log.warning("kms_encryption_failed_fernet_fallback", error=str(e))
            from core.config import cipher_suite  # type: ignore[import]
            settings_update["wa_token"] = cipher_suite.encrypt(wa_token_raw.encode()).decode()

    if settings_update:
        settings_update["updatedAt"] = firestore.SERVER_TIMESTAMP
        user_ref.update(settings_update)

    return jsonify({"status": "success"}), 200


# =============================================================================
# POST /api/tenant_profiles
# =============================================================================
@bp.route("/api/tenant_profiles", methods=["POST"])
@require_auth
def upsert_tenant_profile(uid, tenant_id, user_role):
    from google.cloud import firestore
    from services.shared.helpers import check_quota  # type: ignore[import]

    is_valid, status_code, err_msg = check_quota(tenant_id)
    if not is_valid:
        return jsonify({"error": err_msg}), status_code

    data = request.json or {}
    data.pop("tenant_id", None)
    data["tenant_id"] = tenant_id
    data["createdAt"] = firestore.SERVER_TIMESTAMP
    data["updatedAt"] = firestore.SERVER_TIMESTAMP

    db.collection("tenant_profiles").document(tenant_id).set(data, merge=True)
    return jsonify({"status": "success", "id": tenant_id}), 201


# =============================================================================
# POST /api/tenant_profiles/extract-kb
# =============================================================================
@bp.route("/api/tenant_profiles/extract-kb", methods=["POST"])
@require_auth
def extract_kb(uid, tenant_id, user_role):
    from google.cloud import firestore, storage

    data     = request.json or {}
    filepath = data.get("filepath")
    if not filepath:
        return jsonify({"error": "Missing filepath"}), 400

    bucket_name = os.environ.get("FIREBASE_STORAGE_BUCKET", f"{PROJECT_ID}.appspot.com")
    log.info("kb_extraction_start", tenant_id=tenant_id, filepath=filepath)

    try:
        storage_client = storage.Client()
        blob           = storage_client.bucket(bucket_name).blob(filepath)
        file_bytes     = blob.download_as_bytes()

        extracted_text = ""
        if filepath.lower().endswith(".pdf"):
            import PyPDF2  # type: ignore[import]
            pdf            = PyPDF2.PdfReader(io.BytesIO(file_bytes))
            extracted_text = "\n".join(p.extract_text() for p in pdf.pages if p.extract_text())
        elif filepath.lower().endswith(".txt"):
            extracted_text = file_bytes.decode("utf-8", errors="ignore")
        else:
            return jsonify({"error": "Unsupported file format. Use PDF or TXT."}), 400

        if extracted_text.strip():
            extracted_text = extracted_text.strip()[:10000]
            db.collection("tenant_profiles").document(tenant_id).update(
                {"knowledge_base_text": firestore.ArrayUnion([extracted_text])}
            )
            return jsonify({"status": "success", "message": "Knowledge base appended"}), 200
        return jsonify({"error": "No textual content extracted"}), 400

    except Exception as e:
        log.error("kb_extraction_failed", tenant_id=tenant_id, error=str(e))
        return jsonify({"error": f"Extraction failed: {str(e)}"}), 500


# =============================================================================
# POST /api/analyze-website  (V18 Digital Twin Onboarding)
# =============================================================================
@bp.route("/api/analyze-website", methods=["POST"])
@require_auth
def analyze_website(uid, tenant_id, user_role):
    from services.shared.helpers import _call_gemini_bounded  # type: ignore[import]

    data = request.json or {}
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    log.info("analyze_website_start", url=url, tenant_id=tenant_id)

    try:
        r        = httpx.get(url, timeout=10, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; Sideio/1.0; +https://sideio.com)"})
        raw_html = r.text[:25000]
        clean    = re.sub(r"<[^>]+>", " ", raw_html)
        clean    = re.sub(r"\s+", " ", clean).strip()[:4000]

        if len(clean) < 80:
            return jsonify({"error": "Insufficient content on page to analyze"}), 422

        prompt = f"""You are a B2B market intelligence engine. A user has provided their website content below.
Return ONLY valid JSON — no markdown, no code blocks, no explanation.

--- WEBSITE CONTENT ---
{clean}
--- END CONTENT ---

Return a JSON object with this EXACT structure:
{{
  "company": {{"name": "Short company name", "description": "2-3 sentences", "value": "Core value proposition in 8 words or less"}},
  "targets": [
    {{"name": "Target Persona 1 Name", "description": "Who they are and why they need this"}},
    {{"name": "Target Persona 2 Name", "description": "..."}},
    {{"name": "Target Persona 3 Name", "description": "..."}}
  ],
  "detected_gl": "ISO 2-letter country code (e.g. 'in', 'us'). Use 'us' if unknown.",
  "recommended_campaigns": [
    {{"product_name": "...", "market_trend_hook": "...", "unfair_advantage": "..."}},
    {{"product_name": "...", "market_trend_hook": "...", "unfair_advantage": "..."}}
  ]
}}
Rules: All values must be strings. No nulls. Return ONLY the JSON object."""

        try:
            gemini_resp = _call_gemini_bounded(prompt, timeout_s=15.0)
        except TimeoutError as te:
            return jsonify({"error": "AI analysis timed out.", "code": "gemini_timeout"}), 504

        raw_output = gemini_resp.text.strip()
        raw_output = re.sub(r"^```(?:json)?\s*", "", raw_output, flags=re.M)
        raw_output = re.sub(r"\s*```$", "", raw_output, flags=re.M)

        persona_data = json.loads(raw_output)
        log.info("analyze_website_success", url=url, company=persona_data.get("company", {}).get("name", "unknown"))
        return jsonify({"status": "success", "data": persona_data}), 200

    except httpx.TimeoutException:
        return jsonify({"error": "Website took too long to respond"}), 422
    except httpx.RequestError as e:
        return jsonify({"error": f"Could not reach website: {str(e)}"}), 422
    except json.JSONDecodeError as e:
        return jsonify({"error": "AI analysis returned unexpected format"}), 422
    except Exception as e:
        log.error("analyze_website_error", url=url, error=str(e))
        return jsonify({"error": str(e)}), 422
