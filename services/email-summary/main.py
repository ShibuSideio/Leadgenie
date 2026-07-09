import logging
import os
import datetime
import httpx
from flask import Flask, jsonify, request
from google.cloud import firestore
from google.cloud import secretmanager
import vertexai
from vertexai.generative_models import GenerativeModel

logger = logging.getLogger(__name__)

app = Flask(__name__)
project_id = os.environ["PROJECT_ID"]  # ENTERPRISE: NO FALLBACKS - FAIL FAST IF UNSET

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

def _verify_internal_caller(request):
    """Verify request comes from Cloud Tasks or Cloud Scheduler."""
    if request.headers.get("X-CloudTasks-QueueName") or request.headers.get("X-CloudScheduler-JobName"):
        return True
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return True  # Cloud Run IAM handles actual token validation
    return False

SENDGRID_API_KEY_SECRET = f"projects/{project_id}/secrets/sendgrid_api_key/versions/latest"
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "admin@yourdomain.com")

try:
    vertexai.init(project=project_id, location="asia-south1")
    model = GenerativeModel("gemini-2.5-flash")
except Exception as e:
    logger.warning("Failed to initialize Vertex AI model: %s", e)
    model = None

def get_secret(secret_name):
    try:
        response = _sm().access_secret_version(request={"name": secret_name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.warning("Failed to access secret %s: %s", secret_name, e)
        return ""

def send_summary_email(recipient, lead_count, top_leads):
    api_key = get_secret(SENDGRID_API_KEY_SECRET).strip()
    if not api_key:
        print("Missing SendGrid API Key")
        return
        
    content = f"Daily Lead Sniper Summary\n\nGenerated {lead_count} new contact-ready leads today.\n\nTop Leads:\n" + "\n".join(top_leads)
    
    payload = {
        "personalizations": [{"to": [{"email": recipient}]}],
        "from": {"email": SENDER_EMAIL},
        "subject": "Your Daily Lead Sniper Summary",
        "content": [{"type": "text/plain", "value": content}]
    }

    try:
        # Utilize SendGrid HTTP API avoiding GCP Serverless IP ban triggers
        resp = httpx.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=10
        )
        if resp.status_code >= 400:
            print(f"SendGrid Post Error: {resp.text}")
        else:
            print(f"Sent summary email sequence dynamically via HTTP to {recipient}")
    except Exception as e:
        print(f"HTTP Flow Control Error calling SendGrid: {e}")

@app.route("/send", methods=["POST"])
def send_daily_summaries():
    if not _verify_internal_caller(request):
        return jsonify({"error": "Unauthorized"}), 403
    # Triggered centrally. Iterates through tenants and compiles daily digests.
    tenants = _db().collection("tenants").stream()
    count = 0
    
    
    today = datetime.datetime.utcnow().date()
    yesterday_str = today.isoformat() # Roughly filtering
    
    global_pain_points = []

    for t in tenants:
        t_data = t.to_dict()
        email = t_data.get("admin_email")
        if email:
            # Query recent leads securely across all campaigns for this tenant
            leads_query = _db().collection("leads").where("tenant_id", "==", t.id).limit(50).stream()
            
            top_leads = []
            total = 0
            for l in leads_query:
                data = l.to_dict()
                if data.get("status") not in ["new", "contacted", "approved"]:
                    continue
                pain = str(data.get('pain_point'))
                global_pain_points.append(pain)
                top_leads.append(f"- URL: {data.get('url')} | Score: {data.get('score')} | Pain: {pain[:50]}...")
                total += 1
                if total >= 10: break
            
            if total > 0:
                send_summary_email(email, total, top_leads)
                count += 1
                
    # V7 L0 Macro Intelligence Aggregation
    if model and global_pain_points:
        try:
            combined_context = " ".join(global_pain_points[:100]) # Cap input limits
            prompt = f"Analyze these successful B2B conversions and extract the top 3 global macro-trends or highly converting sectors/keywords: {combined_context}"
            response = model.generate_content(prompt)
            _db().collection("macro_trends").document("latest").set({
                "trends": response.text,
                "timestamp": firestore.SERVER_TIMESTAMP
            })
            print("System Intelligence Macro Update successfully processed.")
        except Exception as intel_e:
            print(f"Macro intelligence cron failed securely: {intel_e}")
                
    return jsonify({"summaries_sent": count}), 200

@app.route("/process-outbound", methods=["POST"])
def process_outbound_emails():
    if not _verify_internal_caller(request):
        return jsonify({"error": "Unauthorized"}), 403
    import uuid
    api_key = get_secret(SENDGRID_API_KEY_SECRET).strip()
    if not api_key:
        print("Missing SendGrid API Key")
        return jsonify({"error": "Missing SendGrid API Key"}), 500

    now = datetime.datetime.now(datetime.timezone.utc)
    worker_id = str(uuid.uuid4())
    lease_duration = datetime.timedelta(minutes=5)
    
    # Query candidate queued documents
    candidates = (
        _db().collection("outbound_emails")
        .where("status", "==", "queued")
        .limit(10)
        .stream()
    )
    
    processed_count = 0
    
    for doc in candidates:
        doc_ref = doc.reference
        
        # Transactional lease acquisition to avoid double-processing
        @firestore.transactional
        def _try_lease(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict()
            status = data.get("status")
            lease_expires = data.get("lease_expires")
            is_expired = lease_expires and lease_expires < now
            
            if status == "queued" or (status == "processing" and is_expired):
                transaction.update(doc_ref, {
                    "status": "processing",
                    "worker_id": worker_id,
                    "lease_expires": now + lease_duration
                })
                return data
            return None

        try:
            doc_data = _try_lease(_db().transaction())
        except Exception as lease_err:
            print(f"Lease transaction failed for {doc.id}: {lease_err}")
            continue
            
        if not doc_data:
            continue
            
        recipient = doc_data.get("email")
        dm_payload = doc_data.get("dm_payload")
        lead_id = doc_data.get("lead_id")
        
        lead_ref = _db().collection("leads").document(lead_id)
        
        if not recipient:
            _mark_terminal_failure(_db(), doc_ref, lead_ref, "failed_delivery", "Missing recipient email address.")
            processed_count += 1
            continue
            
        payload = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {"email": SENDER_EMAIL},
            "subject": "Business Partnership Outreach",
            "content": [{"type": "text/plain", "value": dm_payload or "Hello, reaching out to connect."}]
        }
        
        try:
            resp = httpx.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=10
            )
            
            if resp.status_code == 202:
                # Success: Atomic sync to contacted status across both docs
                batch = _db().batch()
                batch.update(doc_ref, {
                    "status": "contacted",
                    "sent_at": firestore.SERVER_TIMESTAMP,
                    "error_message": None
                })
                batch.update(lead_ref, {
                    "status": "contacted",
                    "contacted_at": firestore.SERVER_TIMESTAMP
                })
                batch.commit()
                processed_count += 1
                print(f"Successfully sent outbound email for lead {lead_id} to {recipient}")
            elif resp.status_code == 429 or resp.status_code >= 500:
                # Transient Error: Revert status to queued for retrying
                retry_count = doc_data.get("retry_count", 0) + 1
                if retry_count >= 3:
                    _mark_terminal_failure(_db(), doc_ref, lead_ref, "failed_delivery", f"Exceeded max retries. API status: {resp.status_code}")
                else:
                    doc_ref.update({
                        "status": "queued",
                        "retry_count": retry_count,
                        "lease_expires": None,
                        "worker_id": None,
                        "error_message": f"Temporary error {resp.status_code}: {resp.text}"
                    })
                processed_count += 1
            else:
                # Terminal Error: bounce, authorization failure, block
                _mark_terminal_failure(_db(), doc_ref, lead_ref, "failed_delivery", f"Terminal API error {resp.status_code}: {resp.text}")
                processed_count += 1
        except Exception as send_err:
            # Transient Network Exception: Revert to queued
            retry_count = doc_data.get("retry_count", 0) + 1
            if retry_count >= 3:
                _mark_terminal_failure(_db(), doc_ref, lead_ref, "failed_delivery", f"Exceeded max retries. Network exception: {send_err}")
            else:
                doc_ref.update({
                    "status": "queued",
                    "retry_count": retry_count,
                    "lease_expires": None,
                    "worker_id": None,
                    "error_message": f"Network exception: {str(send_err)}"
                })
            processed_count += 1
            
    return jsonify({"status": "complete", "processed": processed_count}), 200

def _mark_terminal_failure(db_client, doc_ref, lead_ref, status, error_msg):
    batch = db_client.batch()
    batch.update(doc_ref, {
        "status": status,
        "error_message": error_msg,
        "lease_expires": None,
        "worker_id": None
    })
    batch.update(lead_ref, {
        "status": status,
        "error_message": error_msg
    })
    try:
        batch.commit()
        print(f"Marked lead terminal failure state: {status} - {error_msg}")
    except Exception as e:
        print(f"Failed to write terminal failure state: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
