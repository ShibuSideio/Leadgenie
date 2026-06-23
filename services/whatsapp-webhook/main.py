"""
WhatsApp Webhook Service — LeadGenie

⚠️  FEATURE DISABLED: This service is currently disabled via the
    WHATSAPP_WEBHOOK_ENABLED feature flag. All endpoints return 503.
    Set WHATSAPP_WEBHOOK_ENABLED=true to re-enable after full security review.

Fixes applied (2026-06-23):
  P0-5/P0-7: HMAC now uses dedicated App Secret (not verify token)
  P0-6/P0-2: collection_group query replaced with tenant-scoped query
  P0-4: Lead updates now verify tenant ownership
  P0-5b: WA token lookup scoped to lead's tenant_id
  P0-8: Module-level clients replaced with lazy-init singletons
"""
import os
import hashlib
import hmac
import logging
import httpx
from flask import Flask, request, jsonify
from google.cloud import firestore
from google.cloud import secretmanager

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1MB max

# ── Feature Kill Switch ─────────────────────────────────────────────
WHATSAPP_WEBHOOK_ENABLED = os.environ.get("WHATSAPP_WEBHOOK_ENABLED", "false").lower() == "true"

# ── Structured Logging ──────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-webhook")

# ── Lazy-Init Singletons (P0-8 fix: prevent pre-fork gRPC contention) ──
_db_instance = None
def _db():
    global _db_instance
    if _db_instance is None:
        _db_instance = firestore.Client()
    return _db_instance

_sm_instance = None
def _sm():
    global _sm_instance
    if _sm_instance is None:
        _sm_instance = secretmanager.SecretManagerServiceClient()
    return _sm_instance

project_id = os.environ.get("PROJECT_ID", "sideio-leads-v16")
VERIFY_TOKEN_NAME = f"projects/{project_id}/secrets/whatsapp_webhook_token/versions/latest"
# P0-5/P0-7 fix: Use dedicated App Secret for HMAC, NOT the verify token
APP_SECRET_NAME = f"projects/{project_id}/secrets/whatsapp_app_secret/versions/latest"

def get_secret(secret_name):
    try:
        response = _sm().access_secret_version(request={"name": secret_name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.error(f"Failed to retrieve secret {secret_name}: {e}")
        return ""

def _verify_hmac_signature(payload, signature):
    """P0-5/P0-7 fix: Verify X-Hub-Signature-256 using Facebook App Secret."""
    if not signature.startswith("sha256="):
        return False
    app_secret = get_secret(APP_SECRET_NAME)
    if not app_secret:
        logger.error("whatsapp_app_secret not available — cannot verify signature")
        return False
    expected_hash = hmac.new(app_secret.encode('utf-8'), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected_hash}", signature)

def _resolve_tenant_from_phone(phone_number_id):
    """Resolve tenant_id from a WhatsApp phone_number_id."""
    if not phone_number_id:
        return None
    users_query = _db().collection("users").where("wa_phone_id", "==", phone_number_id).limit(1).stream()
    for u in users_query:
        user_data = u.to_dict()
        return user_data.get("tenant_id") or u.id
    return None

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # ── Feature Kill Switch ──────────────────────────────────────
    if not WHATSAPP_WEBHOOK_ENABLED:
        return jsonify({"error": "WhatsApp webhook is currently disabled"}), 503

    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        verify_token = get_secret(VERIFY_TOKEN_NAME)
        
        if mode and token:
            if mode == "subscribe" and token == verify_token:
                return challenge, 200
            else:
                return "Forbidden", 403
    
    if request.method == "POST":
        signature = request.headers.get("X-Hub-Signature-256", "")
        payload = request.get_data()
        
        # P0-5/P0-7 fix: Use App Secret for HMAC verification
        if not _verify_hmac_signature(payload, signature):
            logger.warning("webhook_signature_verification_failed")
            return "Forbidden", 403

        data = request.json
        logger.info(f"Received Meta Webhook Data")

        try:
            entries = data.get("entry", [])
            for entry in entries:
                changes = entry.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    metadata = value.get("metadata", {})
                    phone_number_id = metadata.get("display_phone_number") or metadata.get("phone_number_id")

                    # P0-2/P0-6 fix: Resolve tenant from phone number FIRST
                    tenant_id = _resolve_tenant_from_phone(phone_number_id)
                    if not tenant_id:
                        logger.warning("webhook_no_tenant_resolved", extra={"phone_number_id": phone_number_id})
                        continue

                    # ── Status Updates (delivered, read, etc.) ──
                    statuses = value.get("statuses", [])
                    for status in statuses:
                        message_id = status.get("id")
                        status_type = status.get("status")  # sent, delivered, read
                        
                        # P0-2/P0-6 fix: Tenant-scoped query instead of collection_group
                        _ALLOWED_WA_STATUSES = {"sent", "delivered", "read", "failed"}
                        if status_type not in _ALLOWED_WA_STATUSES:
                            logger.warning("webhook_invalid_status", extra={"status": status_type})
                            continue

                        leads_query = (
                            _db().collection("leads")
                            .where("tenant_id", "==", tenant_id)
                            .where("wa_message_id", "==", message_id)
                            .limit(1)
                            .stream()
                        )
                        for lead in leads_query:
                            lead.reference.update({"wa_status": status_type})
                            logger.info(f"Updated lead {lead.id} wa_status to {status_type}")
                            
                    # ── Interactive Lead Routing (Button Replies) ──
                    messages = value.get("messages", [])
                    
                    for msg in messages:
                        if msg.get("type") == "interactive":
                            interactive = msg.get("interactive", {})
                            if interactive.get("type") == "button_reply":
                                payload_str = interactive.get("button_reply", {}).get("id", "")
                                sender_wa_id = msg.get("from")
                                
                                if "_" in payload_str:
                                    action, lead_id = payload_str.split("_", 1)
                                    target_status = "approved" if action == "approve" else "ignored"
                                    
                                    # P0-4 fix: Verify lead belongs to this tenant
                                    lead_ref = _db().collection("leads").document(lead_id)
                                    lead_doc = lead_ref.get()
                                    if not lead_doc.exists:
                                        logger.warning("webhook_lead_not_found", extra={"lead_id": lead_id})
                                        continue
                                    
                                    lead_data = lead_doc.to_dict()
                                    if lead_data.get("tenant_id") != tenant_id:
                                        logger.warning("webhook_tenant_mismatch",
                                                       extra={"lead_id": lead_id, "expected": tenant_id,
                                                              "actual": lead_data.get("tenant_id")})
                                        continue
                                    
                                    previous_status = lead_data.get("status")
                                    lead_ref.update({"status": target_status})
                                    logger.info(f"Autonomous State Sync: {lead_id} -> {target_status}")
                                    
                                    # V7 Meta Compliance Queue Staging
                                    if target_status == "approved" and previous_status != "approved":
                                        _db().collection("outbound_emails").document(lead_id).set({
                                            "lead_id": lead_id,
                                            "tenant_id": tenant_id,
                                            "url": lead_data.get("url"),
                                            "dm_payload": lead_data.get("dm"),
                                            "email": lead_data.get("email"),
                                            "status": "queued",
                                            "timestamp": firestore.SERVER_TIMESTAMP
                                        })
                                        
                                    # Send confirmation back
                                    # P0-5b fix: Use tenant-scoped token lookup
                                    if phone_number_id and sender_wa_id:
                                        try:
                                            user_doc = _db().collection("users").document(tenant_id).get()
                                            wa_token = user_doc.to_dict().get("wa_token") if user_doc.exists else None
                                                
                                            if wa_token:
                                                reply_payload = {
                                                    "messaging_product": "whatsapp",
                                                    "to": sender_wa_id,
                                                    "type": "text",
                                                    "text": {"body": f"Acknowledged. Target strictly {target_status}."}
                                                }
                                                wa_headers = {"Authorization": f"Bearer {wa_token}", "Content-Type": "application/json"}
                                                httpx.post(f"https://graph.facebook.com/v18.0/{phone_number_id}/messages",
                                                           json=reply_payload, headers=wa_headers, timeout=5)
                                        except Exception as reply_e:
                                            logger.error(f"Confirmation send failed: {reply_e}")
        except Exception as e:
            logger.error(f"Error processing webhook: {e}")
            
        return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
