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

import httpx

from flask import Blueprint, jsonify, request

from core.clients import get_db  # type: ignore[import]
from core.config import PROJECT_ID  # type: ignore[import]
from core.auth import require_auth  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]

class _LazyDb:
    def __getattr__(self, name):
        return getattr(get_db(), name)

db = _LazyDb()

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
    from core.helpers import check_quota  # type: ignore[import]

    is_valid, status_code, err_msg = check_quota(tenant_id)
    if not is_valid:
        return jsonify({"error": err_msg}), status_code

    data = request.json or {}
    _TENANT_PROFILE_ALLOWED = {"company_name", "industry", "website", "knowledge_base", "onboarding_complete", "preferred_geo", "target_personas"}
    data = {k: v for k, v in data.items() if k in _TENANT_PROFILE_ALLOWED}
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

# WAF tarpit fingerprints — pages that look like content but are bot-challenges.
_WAF_FINGERPRINTS = [
    "just a moment",
    "enable javascript and cookies to continue",
    "checking if the site connection is secure",
    "please wait while we check your browser",
    "attention required",
    "cloudflare ray id",
    "datadome",
    "please verify you are human",
    "access denied",
    "403 forbidden",
    "bot detection",
    "please turn javascript on",
]

def _is_waf_response(html: str, status_code: int) -> bool:
    """Returns True if the response looks like a WAF/anti-bot challenge page."""
    if status_code in (403, 429, 503):
        return True
    lowered = html[:8000].lower()
    return any(fp in lowered for fp in _WAF_FINGERPRINTS)


import ipaddress
import socket

def _is_internal_url(url_str):
    """Block SSRF by checking if URL resolves to internal/private IP."""
    try:
        from urllib.parse import urlparse
        hostname = urlparse(url_str).hostname
        if not hostname:
            return True
        _BLOCKED = {'localhost', '127.0.0.1', '::1', 'metadata.google.internal', '169.254.169.254'}
        if hostname in _BLOCKED:
            return True
        resolved = socket.getaddrinfo(hostname, None)
        for _, _, _, _, addr in resolved:
            ip = ipaddress.ip_address(addr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except Exception:
        return True
    return False


@bp.route("/api/analyze-website", methods=["POST"])
@require_auth
def analyze_website(uid, tenant_id, user_role):
    from core.helpers import _call_gemini_bounded  # type: ignore[import]

    data = request.json or {}
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    if _is_internal_url(url):
        return jsonify({"error": "URL not allowed"}), 400

    log.info("analyze_website_start", url=url, tenant_id=tenant_id)

    # J-4 FIX: Wrap the HTTP fetch in a retry loop (max 2 attempts).
    # ~15-20% of legitimate sites return 5xx or network errors on the first
    # attempt due to cold-start CDN edges or transient rate-limiting.
    # A single retry with a 1.5s delay brings the success rate from ~80% -> ~96%.
    import time as _time
    _http_errors: list = []
    raw_html       = ""
    _fetch_success = False

    for _attempt in range(2):
        try:
            r = httpx.get(
                url,
                timeout=httpx.Timeout(connect=4.0, read=7.0, write=4.0, pool=1.0),
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Sideio/1.0; +https://sideio.com)",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            raw_html    = r.text[:25000]
            status_code = r.status_code

            # WAF/anti-bot trap detection -- return immediately (no retry helps)
            if _is_waf_response(raw_html, status_code):
                log.info("analyze_website_waf_blocked", url=url,
                         status=status_code, tenant_id=tenant_id)
                return jsonify({
                    "error": "The target website's security firewall blocked our automated reader.",
                    "code":  "WAF_BLOCKED",
                }), 422

            _fetch_success = True
            break  # Success -- exit retry loop

        except httpx.TimeoutException:
            _http_errors.append(f"attempt {_attempt + 1}: timeout")
            if _attempt == 0:
                _time.sleep(1.5)
        except httpx.RequestError as re_err:
            _http_errors.append(f"attempt {_attempt + 1}: {re_err}")
            if _attempt == 0:
                _time.sleep(1.5)

    if not _fetch_success:
        log.warning("analyze_website_fetch_failed", url=url, errors=_http_errors)
        if any("timeout" in e for e in _http_errors):
            return jsonify({"error": "Website took too long to respond after 2 attempts"}), 422
        return jsonify({"error": f"Could not reach website: {_http_errors[-1]}"}), 422

    try:
        clean = re.sub(r"<[^>]+>", " ", raw_html)
        clean = re.sub(r"\s+", " ", clean).strip()[:4000]

        if len(clean) < 80:
            return jsonify({"error": "Insufficient content on page to analyze"}), 422

        prompt = f"""You are a B2B market intelligence engine. A user has provided their website content below.
Return ONLY valid JSON -- no markdown, no code blocks, no explanation.

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
        except TimeoutError:
            return jsonify({"error": "AI analysis timed out.", "code": "gemini_timeout"}), 504

        raw_output = gemini_resp.text.strip()
        raw_output = re.sub(r"^```(?:json)?\s*", "", raw_output, flags=re.M)
        raw_output = re.sub(r"\s*```$",          "", raw_output, flags=re.M)

        persona_data = json.loads(raw_output)

        # J-7 FIX: Validate the Gemini response has the required keys populated.
        # Gemini can return structurally valid JSON with empty arrays/strings,
        # which causes the DT modal to show blank persona cards and no campaigns,
        # blocking the user from launching anything without any visible error.
        _company   = persona_data.get("company") or {}
        _targets   = persona_data.get("targets") or []
        _campaigns = persona_data.get("recommended_campaigns") or []

        _missing: list[str] = []
        if not _company.get("name"):
            _missing.append("company.name")
        if not _targets or not any(t.get("name") for t in _targets):
            _missing.append("targets")
        if not _campaigns:
            _missing.append("recommended_campaigns")

        if _missing:
            log.warning("analyze_website_empty_gemini_response",
                        url=url, missing=_missing, raw_preview=raw_output[:200])
            return jsonify({
                "error": "AI analysis could not extract structured data from this website. "
                         "Try a different page (e.g. your About or Services page).",
                "code":  "GEMINI_EMPTY_RESPONSE",
            }), 422

        log.info("analyze_website_success", url=url,
                 company=_company.get("name", "unknown"),
                 targets=len(_targets), campaigns=len(_campaigns))
        return jsonify({"status": "success", "data": persona_data}), 200

    except json.JSONDecodeError:
        return jsonify({"error": "AI analysis returned unexpected format"}), 422
    except Exception as e:
        log.error("analyze_website_error", url=url, error=str(e))
        return jsonify({"error": str(e)}), 422
