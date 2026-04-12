import os
import json
import urllib.request
import urllib.parse
from urllib.parse import urlparse
import time
import datetime
import hashlib
import uuid

import httpx
from google.protobuf import timestamp_pb2
from flask import Flask, request, jsonify, make_response
from google.cloud import tasks_v2
from google.cloud import secretmanager, firestore, bigquery, storage
import PyPDF2
import io
from google.cloud import kms
from cryptography.fernet import Fernet
import base64
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

app = Flask(__name__)
ALLOWED_ORIGINS = ["https://lead-sniper-prod.web.app", "https://lead-sniper-prod.firebaseapp.com"]

vertexai.init(location="us-central1")

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        res = make_response()
        origin = request.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            # STRICT OVERWRITE - DO NOT USE .add()
            res.headers['Access-Control-Allow-Origin'] = origin
            res.headers['Access-Control-Allow-Headers'] = "Content-Type, Authorization"
            res.headers['Access-Control-Allow-Methods'] = "GET, POST, PUT, DELETE, OPTIONS"
        return res, 204

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        # STRICT OVERWRITE - DO NOT USE .add()
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = "Content-Type, Authorization"
        response.headers['Access-Control-Allow-Methods'] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


@app.errorhandler(Exception)
def handle_exception(e):
    # Log the error safely here
    import sys
    print(f"GLOBAL CONTAINER EXCEPTION: {str(e)}", file=sys.stderr)
    return jsonify({"error": "Internal Server Error", "message": str(e)}), 500

import firebase_admin
from firebase_admin import credentials, firestore, auth
from google.cloud.firestore_v1.base_query import FieldFilter

# Initialize Admin SDK once natively for Thin Client API Authorization
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()
tasks_client = tasks_v2.CloudTasksClient()

MAX_CHILD_CAMPAIGNS = int(os.environ.get("MAX_CHILD_CAMPAIGNS", 5))

# Use the explicitly deployed Vertex GenAI region
PROJECT_ID = os.environ.get("PROJECT_ID", "sideio-leads-v16")
LOCATION = os.environ.get("LOCATION", "asia-south1")
QUEUE            = os.environ.get("QUEUE",            "lead-pipeline-queue")
# V18: Self-referential URL for internal Cloud Tasks (BQ telemetry handler).
# Set via --update-env-vars in cloudbuild.yaml after first deploy.
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "")
PIPELINE_URL = os.environ.get("PIPELINE_URL", "https://lead-pipeline-main-abc.a.run.app/dispatch")

FERNET_KEY = os.environ.get("ENCRYPTION_KEY", "uNqG8Jc-44SjK22N8B5-2GksnE5F_88_V5wQZ02j1A0=")
cipher_suite = Fernet(FERNET_KEY.encode())


# ---------------------------------------------------------------------------
# ONTOLOGY MAP HELPERS (Phase 1)
# Stateless pure functions — duplicated from pipeline-main (separate services).
# ---------------------------------------------------------------------------
_SOCIAL_ONTOLOGY_DOMAINS = {
    "reddit.com", "facebook.com", "linkedin.com", "quora.com",
    "kaggle.com", "instagram.com", "twitter.com", "x.com", "youtube.com"
}

def parse_base_path(url: str) -> str:
    """
    Dynamic base_path key for the ontology_map collection.
    Social domains  → domain + 2 path segments (e.g., reddit.com/r/Entrepreneur).
    B2B/news        → root domain only (e.g., techcrunch.com).
    """
    try:
        parsed   = urlparse(url if url.startswith('http') else f'https://{url}')
        hostname = parsed.hostname or ''
        domain   = hostname.removeprefix('www.')
        if not domain:
            return 'unknown'
        if any(domain.endswith(s) for s in _SOCIAL_ONTOLOGY_DOMAINS):
            segments  = [s for s in parsed.path.split('/') if s]
            key_parts = [domain] + segments[:2]
            return '/'.join(key_parts)
        return domain
    except Exception:
        return 'unknown'


# ---------------------------------------------------------------------------
# EPSILON-GREEDY ROUTER HELPERS (Phase 4)
# ---------------------------------------------------------------------------

def _get_router_config(db) -> dict:
    """
    Fetches exploit_ratio and discovery_allocation from system_config/router.
    Auto-initializes the document with safe defaults on first run.
    Defaults: exploit_ratio=0.10 (10% Autonomous), discovery_allocation=0.15.
    """
    ref = db.collection('system_config').document('router')
    doc = ref.get()
    if not doc.exists:
        # Node 1: Initialize on first call (idempotent)
        defaults = {
            'exploit_ratio':        0.10,
            'discovery_allocation': 0.15,
            'initialized_at':       firestore.SERVER_TIMESTAMP,
        }
        ref.set(defaults)
        print("[ROUTER] system_config/router initialized with defaults.")
        return defaults
    return doc.to_dict()


def _pop_from_predictive_cache(tenant_id: str, db, count: int) -> list:
    """
    Pops up to `count` leads from users/{tenant_id}/predictive_cache.
    Sort: score DESC (highest quality first).
    Semantics: TRUE MOVE — each popped doc is written to main leads collection
    and immediately deleted from the cache. No stale copies left.

    Returns list of lead dicts that were successfully moved.
    """
    if count <= 0:
        return []

    cache_col = (
        db.collection('users')
        .document(tenant_id)
        .collection('predictive_cache')
    )

    try:
        # Exclude already-expired entries and sort by score DESC
        cache_docs = (
            cache_col
            .where(filter=FieldFilter('status', '==', 'new'))
            .order_by('score', direction='DESCENDING')
            .limit(count)
            .stream()
        )
        cache_docs = list(cache_docs)
    except Exception as e:
        print(f"[ROUTER] predictive_cache query failed for {tenant_id}: {e}")
        return []

    promoted = []
    batch    = db.batch()

    for cache_doc in cache_docs:
        lead_data = cache_doc.to_dict()
        lead_id   = cache_doc.id

        # Promote to main leads collection with status=new
        lead_data['status']    = 'new'
        lead_data['promotedAt'] = firestore.SERVER_TIMESTAMP
        # Extend TTL from 72h cache to 90-day DPDP window
        lead_data['expire_at'] = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=90)
        )

        leads_ref = db.collection('leads').document(lead_id)
        batch.set(leads_ref, lead_data, merge=True)

        # Immediately delete from predictive_cache (true move)
        batch.delete(cache_col.document(lead_id))
        promoted.append(lead_data)

    try:
        batch.commit()
        print(f"[ROUTER] Promoted {len(promoted)} autonomous leads from cache -> leads for {tenant_id}")
    except Exception as e:
        print(f"[ROUTER] Batch commit failed for {tenant_id}: {e}")
        return []

    return promoted

def get_service_account_email():
    url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.read().decode('utf-8')
        except Exception as e:
            print(f"Failed to fetch metadata SA email on attempt {attempt}: {e}")
            if attempt < 3:
                time.sleep(1.5 ** attempt) # Exponential backoff limit
    print("Critical Failure: OIDC token metadata fetch permanently dropped.")
    return ""

def authenticate_request(request):
    """
    Extract Bearer token mathematically validating the user and extracting their strictly mapped UI scope.
    Returns: User UID and Tenant ID dynamically synthesized from the Custom Claims.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        raise ValueError("Missing or incorrectly formatted Authorization header.")
    
    token = auth_header.split('Bearer ')[1]
    
    try:
        decoded_token = auth.verify_id_token(token)
    except Exception as e:
        import sys
        print(f"FATAL TOKEN VERIFICATION ERROR: {str(e)}", file=sys.stderr)
        raise ValueError(f"Token verification strictly failed: {str(e)}")
    
    uid = decoded_token.get('uid')
    if not uid:
        raise ValueError("Critical Security Anomaly: Invalid structural decoding.")
        
    user_ref = db.collection('users').document(uid)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        # Fallback Creation: Brand new user registration
        tenant_id = uid
        user_role = 'admin'
        user_ref.set({
            'tenant_id': tenant_id,
            'role': user_role,
            'email': decoded_token.get('email', 'unknown'),
            'is_active': True,
            'approval_status': 'pending',
            'beta_expiry': None,
            'wallet': {
                'allocated_credits': 0,
                'consumed_credits': 0
            },
            'createdAt': firestore.SERVER_TIMESTAMP
        })
    else:
        user_data = user_doc.to_dict()
        tenant_id = user_data.get('tenant_id') or uid
        user_role = user_data.get('role', 'admin')
        is_active = user_data.get('is_active', True)
        
        email = decoded_token.get('email', '')
        if email and 'email' not in user_data:
            user_ref.update({'email': email})
            
        if not is_active and user_role != 'super_admin':
            raise ValueError("Account suspended by L0 Governance Protocol.")
            
    return uid, tenant_id, user_role

def check_quota(tenant_id):
    user_doc = db.collection("users").document(tenant_id).get()
    if user_doc.exists:
        data = user_doc.to_dict()
        
        if data.get("role") == "super_admin":
            return True, 200, "OK"
        
        if data.get("approval_status") != "approved":
            return False, 403, "Your application is under review. Please wait for L0 approval."
            
        wallet = data.get("wallet", {})
        credits = wallet.get("allocated_credits", 0)
        consumed = wallet.get("consumed_credits", 0)
        
        shard_sum = sum(shard.to_dict().get("consumed_credits", 0) for shard in db.collection("users").document(tenant_id).collection("wallet_shards").stream())
        
        if (credits - consumed - shard_sum) <= 0:
            return False, 402, "Beta quota exhausted. Contact admin to reload."
            
        return True, 200, "OK"
    return False, 401, "Unknown identity."

# ---------------------------------------------------------------------------
# V14: SYNAPTIC ROUTER HELPERS
# ---------------------------------------------------------------------------

def get_vector_weights():
    """
    Reads the global sourcing success weights from system_telemetry/vector_weights.
    Returns a dict, or sensible defaults if the document doesn't exist yet.
    """
    try:
        doc = db.collection("system_telemetry").document("vector_weights").get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        print(f"[VECTOR WEIGHTS] Failed to read system_telemetry: {e}")
    return {
        "Classic B2B": 10,
        "Social/Forum Listening": 8,
        "Review Hijacking": 5,
        "Maps/GMB Targeting": 3
    }

def classify_sourcing_vector(bio, industry_weights):
    """
    V14: Gated LLM call — fires ONLY on campaign create/update, result cached on campaign doc.
    Classifies the optimal sourcing vector for a given bio using global success weights.
    """
    if not bio:
        return "Classic B2B"
    try:
        prompt = f"""Based on this user's business bio: '{bio}', and the global sourcing success weights for this industry: {json.dumps(industry_weights)},
select the single optimal digital sourcing vector to find the most relevant prospects.

Choose STRICTLY ONE of: "Social/Forum Listening", "Review Hijacking", "Classic B2B", "Maps/GMB Targeting".

- Social/Forum Listening: Best for service businesses where prospects post problems on Reddit, Quora, Facebook groups.
- Review Hijacking: Best for local businesses or hospitality where negative reviews signal pain.
- Maps/GMB Targeting: Best for brick-and-mortar or geo-restricted service areas.
- Classic B2B: Best for enterprise, SaaS, or professional services.

Output ONLY the chosen vector string. No explanation, no punctuation, no markdown."""

        model = GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(
            prompt,
            generation_config=GenerationConfig(temperature=0.1)
        )
        vector = response.text.strip().strip('"')
        valid_vectors = {"Social/Forum Listening", "Review Hijacking", "Classic B2B", "Maps/GMB Targeting"}
        return vector if vector in valid_vectors else "Classic B2B"
    except Exception as e:
        print(f"[SYNAPTIC ROUTER] Vector classification failed: {e}. Defaulting to Classic B2B.")
        return "Classic B2B"

def sanitize_document(doc):
    """
    Statically unpacks and sanitizes Firestore Documents dynamically serializing Timestamps securely.
    """
    data = doc.to_dict() or {}
    data['id'] = doc.id
    
    # Process Timestamps explicitly bypassing Flask JSONEncoder errors natively.
    for k, v in data.items():
        if hasattr(v, 'timestamp'):  
            data[k] = v.isoformat()
    return data

def handle_purge(request):
    data = request.json or {}
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return jsonify({"error": "Missing tenant_id"}), 400
        
    print(f"INITIATING DATA ERASURE DPDP COMPLIANCE FOR TENANT: {tenant_id}")
    
    campaigns = db.collection("campaigns").where(field_path="tenant_id", op_string="==", value=tenant_id).limit(100).stream()
    for doc in campaigns:
        doc.reference.delete()
        
    leads = db.collection("leads").where(field_path="tenant_id", op_string="==", value=tenant_id).limit(100).stream()
    for doc in leads:
        lead_data = doc.to_dict()
        url = lead_data.get("url")
        if url:
            cache_id = url.replace('/','_')
            db.collection("scraped_cache").document(cache_id).delete()
        doc.reference.delete()
        
    db.collection("tenants").document(tenant_id).delete()
    return jsonify({"message": f"Successfully erased tenant {tenant_id} data completely"}), 200


# =============================================================================
# V18: BIGQUERY RLHF TELEMETRY — CLOUD TASKS ARCHITECTURE
#
# Cloud Run throttles CPU after the HTTP response returns, which kills daemon
# threads mid-execution. This is the correct pattern:
#
#   PUT /api/leads/{id}
#     └─► _enqueue_bq_telemetry_task()   ← enqueues to lead-pipeline-queue
#            └─► Cloud Tasks HTTP POST
#                  └─► /api/internal/telemetry/bq-push   ← new internal endpoint
#                        └─► _handle_bq_push_task()       ← synchronous BQ insert
#
# Guarantees:
#   - PUT response returns in <5ms (task enqueue is fast)
#   - BQ insert runs in a dedicated Cloud Tasks worker lifecycle
#   - Idempotent: event_id is a UUID generated at enqueue time (logged for dedup)
#   - Non-blocking: Cloud Tasks failure never propagates to the user response
# =============================================================================

def _enqueue_bq_telemetry_task(tenant_id: str, lead_dict: dict, status: str):
    """
    Enqueues an RLHF telemetry event to lead-pipeline-queue.
    The task POSTs to /api/internal/telemetry/bq-push on the orchestrator itself.
    Failure is logged and swallowed — telemetry must never degrade UX.
    """
    if not ORCHESTRATOR_URL:
        print("[BQ TASK] ORCHESTRATOR_URL not set — skipping telemetry enqueue.")
        return

    try:
        import hashlib, uuid, datetime

        # Build anonymized intent hash (no PII)
        raw_signal  = (lead_dict.get("intent_signal", "") or "") + "|" + (lead_dict.get("sourcing_vector", "") or "")
        intent_hash = hashlib.sha256(raw_signal.encode()).hexdigest()

        # Stripped signal payload — no email, DM text, URLs, or contact data
        stripped_payload = {
            "score":           lead_dict.get("score"),
            "sourcing_vector": lead_dict.get("sourcing_vector"),
            "prism_mode":      lead_dict.get("prism_mode"),
            "tech_stack":      lead_dict.get("tech_stack_found", []),
            "hiring_intent":   lead_dict.get("hiring_intent_found"),
            "origin_engine":   lead_dict.get("origin_engine"),
            "campaign_id":     lead_dict.get("campaign_id"),
        }

        event_id = str(uuid.uuid4())
        task_payload = {
            "event_id":           event_id,
            "timestamp":          datetime.datetime.utcnow().isoformat() + "Z",
            "tenant_id":          tenant_id,
            "prism_mode":         lead_dict.get("prism_mode") or "GeneralDomain",
            "conversion_status":  status,
            "intent_hash":        intent_hash,
            "raw_signal_payload": json.dumps(stripped_payload),
        }

        queue_path = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
        sa_email   = get_service_account_email().strip()
        target_url = f"{ORCHESTRATOR_URL}/api/internal/telemetry/bq-push"

        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url":         target_url,
                "headers":     {"Content-Type": "application/json"},
                "body":        json.dumps(task_payload).encode(),
            }
        }
        if sa_email:
            task["http_request"]["oidc_token"] = {
                "service_account_email": sa_email,
                "audience":              ORCHESTRATOR_URL,
            }

        tasks_client.create_task(request={"parent": queue_path, "task": task})
        print(f"[BQ TASK] Enqueued event_id={event_id[:8]}... | status={status} | hash={intent_hash[:12]}...")

    except Exception as e:
        # Non-fatal: telemetry must never block or degrade production flow
        print(f"[BQ TASK] Enqueue failed (non-fatal): {e}")


def _handle_bq_push_task(payload: dict):
    """
    Synchronous BigQuery streaming insert.
    Called exclusively from /api/internal/telemetry/bq-push Cloud Tasks handler.
    Runs in its own Cloud Tasks worker lifecycle — CPU is never throttled.
    """
    try:
        from google.cloud import bigquery as bq_client_lib

        bq        = bq_client_lib.Client(project=PROJECT_ID)
        table_ref = f"{PROJECT_ID}.swarm_analytics.rlhf_events"

        row = {
            "event_id":           payload.get("event_id"),
            "timestamp":          payload.get("timestamp"),
            "tenant_id":          payload.get("tenant_id"),
            "prism_mode":         payload.get("prism_mode"),
            "conversion_status":  payload.get("conversion_status"),
            "intent_hash":        payload.get("intent_hash"),
            "raw_signal_payload": payload.get("raw_signal_payload"),
        }

        errors = bq.insert_rows_json(table_ref, [row])
        if errors:
            print(f"[BQ INSERT] Streaming error: {errors}")
            return False
        print(f"[BQ INSERT] event_id={payload.get('event_id','?')[:8]}... streamed OK")
        return True
    except Exception as e:
        print(f"[BQ INSERT] Failed: {e}")
        return False

@app.route('/api/me', methods=['GET', 'PUT', 'OPTIONS'])
def get_me():
    if request.method == 'OPTIONS':
        return '', 204
    try:
        uid, tenant_id, user_role = authenticate_request(request)
    except ValueError as ve:
        return jsonify({"error": "Unauthorized", "message": str(ve)}), 401
    except Exception as e:
        return jsonify({"error": "Internal Error", "message": str(e)}), 500

    if request.method == 'PUT':
        payload = request.json or {}
        updates = {}
        if "agreed_to_terms" in payload:
            from google.cloud import firestore
            updates["agreed_to_terms"] = firestore.SERVER_TIMESTAMP
        if "crm_webhook_url" in payload:
            updates["crm_webhook_url"] = payload.get("crm_webhook_url")
            
        if updates:
            db.collection("users").document(uid).update(updates)
            return jsonify({"status": "success", "message": "Updated details"}), 200
        return jsonify({"status": "success", "message": "No updates applied"}), 200

    user_doc = db.collection("users").document(uid).get()
    if user_doc.exists:
        data = user_doc.to_dict()
        # Null-safe wallet read: 'consumed_credits' may be absent after a DB
        # wipe or schema migration. Use .get() with 0 fallback on every field.
        raw_wallet = data.get("wallet", {})
        allocated  = int(raw_wallet.get("allocated_credits", 0) or 0)
        consumed   = int(raw_wallet.get("consumed_credits",  0) or 0)

        # wallet_shards sub-collection may be empty after cutover — sum() of
        # empty iterator = 0, so this is always safe.
        shard_sum = sum(
            int(shard.to_dict().get("consumed_credits", 0) or 0)
            for shard in db.collection("users")
                         .document(uid)
                         .collection("wallet_shards")
                         .stream()
        )
        consumed += shard_sum

        wallet = {
            "allocated_credits": allocated,
            "consumed_credits":  consumed,
        }

        return jsonify({
            "status": "success",
            "data":   data,
            "wallet": wallet
        }), 200
    return jsonify({"error": "User structure missing"}), 404


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
def trigger_daily_sweep(path):
    """
    Unified Orchestrator API Gateway Module.
    Natively controls Background Task Dispatch arrays and Secure Thin-Client Database Polling.
    """
    # -----------------------------------------------------------------------------------------
    # REST API Gateway Protocol (Frontend Database Reading)
    # -----------------------------------------------------------------------------------------
    if request.method == 'OPTIONS':
        return '', 204

    if request.path in ["/api/campaigns", "/api/leads", "/api/tenant_profiles"] and request.method == "GET":
        try:
            uid, tenant_id, user_role = authenticate_request(request)
            
            if request.path == "/api/campaigns":
                docs = db.collection("campaigns").where(field_path="tenant_id", op_string="==", value=tenant_id).limit(100).stream()
                
            elif request.path == "/api/leads":
                # V15: Server-side CRM filter — honour ?crm= param to avoid full-collection downloads
                # ?crm=true  → CRM board (is_in_crm == True)
                # ?crm=false → Main dashboard feed (is_in_crm == False)
                # (no param) → All leads, for backward compat with any legacy consumers
                crm_param = request.args.get("crm")  # None | "true" | "false"
                q = db.collection("leads").where(field_path="tenant_id", op_string="==", value=tenant_id)
                if crm_param == "true":
                    q = q.where(field_path="is_in_crm", op_string="==", value=True)
                elif crm_param == "false":
                    q = q.where(field_path="is_in_crm", op_string="==", value=False)
                docs = q.limit(200).stream()

            elif request.path == "/api/tenant_profiles":
                # Master Twin fetch explicitly for Dashboard syncing
                doc = db.collection("tenant_profiles").document(tenant_id).get()
                docs = [doc] if doc.exists else []

            results = [sanitize_document(doc) for doc in docs]
            return jsonify({"status": "success", "data": results}), 200
            
        except ValueError as ve:
            return jsonify({"error": "Unauthorized", "message": str(ve)}), 401
        except Exception as e:
            return jsonify({"error": "Internal Error", "message": str(e)}), 500

    # -----------------------------------------------------------------------------------------
    # REST L0 Governance API Protocol 
    # -----------------------------------------------------------------------------------------
    if request.path.startswith("/api/l0/"):
        try:
            uid, tenant_id, user_role = authenticate_request(request)
            if user_role != "super_admin":
                return jsonify({"error": "Forbidden L0 Access"}), 403

            if request.path == "/api/l0/telemetry" and request.method == "GET":
                # Macro Aggregation
                macro_totals = {}
                for st in ["new", "contacted", "ignored", "failed", "processing", "completed"]:
                    q = db.collection("leads").where(field_path="status", op_string="==", value=st)
                    res = q.count().get()
                    macro_totals[st] = res[0][0].value
                
                total_leads = sum(macro_totals.values())
                macro_totals["total_leads"] = total_leads
                
                # Micro Aggregation
                tenants = []
                users = db.collection("users").stream()
                for user in users:
                    u_data = user.to_dict()
                    t_id = u_data.get("tenant_id", user.id)
                    q2 = db.collection("leads").where(field_path="tenant_id", op_string="==", value=t_id)
                    res2 = q2.count().get()
                    leads_gen = res2[0][0].value
                    
                    wallet = u_data.get("wallet", {})
                    shard_sum = sum(shard.to_dict().get("consumed_credits", 0) for shard in db.collection("users").document(t_id).collection("wallet_shards").stream())
                    wallet_balance = wallet.get("allocated_credits", 0) - wallet.get("consumed_credits", 0) - shard_sum
                    
                    tenant_info = u_data.copy()
                    tenant_info.update({
                        "tenant_id": t_id,
                        "wallet_balance": wallet_balance,
                        "total_leads_generated": leads_gen
                    })
                    tenants.append(tenant_info)
                    
                return jsonify({
                    "status": "success",
                    "data": {
                        "macro": macro_totals,
                        "tenants": sorted(tenants, key=lambda x: x.get("total_leads_generated", 0), reverse=True)
                    }
                }), 200

            elif request.path == "/api/l0/trends" and request.method == "GET":
                campaigns = db.collection("campaigns").stream()
                users_stream = db.collection("users").stream()
                user_map = {}
                for u in users_stream:
                    user_map[u.id] = u.to_dict().get("email", "Unknown Email")
                    
                trends = []
                for camp in campaigns:
                    c = camp.to_dict()
                    tenant_id = c.get("tenant_id")
                    if not tenant_id or c.get("status", "paused") != "active": continue
                    
                    q = db.collection("leads").where(field_path="campaign_id", op_string="==", value=camp.id)
                    leads_count = q.count().get()[0][0].value
                    
                    trends.append({
                        "campaign_id": camp.id,
                        "tenant_id": tenant_id,
                        "email": user_map.get(tenant_id, "Unknown Email"),
                        "name": c.get("name", "Unnamed Campaign"),
                        "bio": c.get("bio", "No Bio Provided"),
                        "keywords": c.get("keywords", "No Keywords"),
                        "leads_generated": leads_count
                    })
                
                return jsonify({
                    "status": "success", 
                    "data": {
                        "campaign_trends": sorted(trends, key=lambda x: x["leads_generated"], reverse=True)
                    }
                }), 200
                
            elif request.path == "/api/l0/users" and request.method == "GET":
                docs = db.collection("users").limit(100).stream()
                results = [sanitize_document(doc) for doc in docs]
                # Gather aggregate tracking limits globally
                for res in results:
                    usage_doc = db.collection("usage_metrics").document(res.get("tenant_id", "")).get()
                    res["usage_metrics"] = usage_doc.to_dict() if usage_doc.exists else {}
                return jsonify({"status": "success", "data": results}), 200
                
            elif request.path == "/api/l0/users/suspend" and request.method == "POST":
                data = request.json or {}
                target_uid = data.get("uid")
                target_state = data.get("is_active", False)
                if target_uid:
                    db.collection("users").document(target_uid).update({"is_active": target_state})
                    return jsonify({"status": "success", "message": f"Suspension toggled cleanly."}), 200
                return jsonify({"error": "Missing uid limit"}), 400

            elif request.path.startswith("/api/l0/users/") and request.path.endswith("/mint") and request.method == "POST":
                target_tenant = request.path.split("/")[-2]
                amount = float(request.json.get("amount", 0)) if request.json else 0
                if amount > 0:
                    db.collection("users").document(target_tenant).update(
                        {"wallet.allocated_credits": firestore.Increment(int(amount))}
                    )
                    return jsonify({"status": "success", "message": f"Minted {int(amount)} credits."}), 200
                return jsonify({"error": "Invalid mint amount"}), 400

            elif request.path.startswith("/api/l0/users/") and request.path.endswith("/approve") and request.method == "POST":
                target_tenant = request.path.split("/")[-2]
                payload = request.json or {}
                amount = int(payload.get("amount", 20000))
                days = int(payload.get("days", 180))
                
                new_expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
                db.collection("users").document(target_tenant).update(
                    {
                        "approval_status": "approved",
                        "beta_expiry": new_expiry,
                        "wallet.allocated_credits": firestore.Increment(amount)
                    }
                )
                return jsonify({"status": "success", "message": f"Approved identity with {amount} credits for {days} days."}), 200
                
        except ValueError as ve:
            return jsonify({"error": "Unauthorized", "message": str(ve)}), 401
        except Exception as e:
            return jsonify({"error": "Internal Error", "message": str(e)}), 500

    # -----------------------------------------------------------------------------------------
    # REST API Gateway Protocol (Frontend Database Mutations)
    # -----------------------------------------------------------------------------------------
    if request.path.startswith("/api/") and not request.path.startswith("/api/internal/") and request.method in ["POST", "PUT"]:
        try:
            uid, tenant_id, user_role = authenticate_request(request)
            data = request.json or {}
            
            # Remove any forged tenant injections
            data.pop('tenant_id', None)
            
            # ── /api/analyze-website ───────────────────────────────────────────
            # V18 Digital Twin Onboarding: Scrapes the user's website and uses
            # Gemini to extract a structured company persona + target audience
            # profile. Called by dtStartAnalysis() in app.js (View A → B → C).
            #
            # Returns JSON matching dtPopulatePersonas() schema:
            #   { company: {name, description, value},
            #     targets: [{name, description}, ...],
            #     detected_gl: "us",
            #     recommended_campaigns: [{product_name, market_trend_hook, unfair_advantage}, ...] }
            # ──────────────────────────────────────────────────────────────────
            if request.path == "/api/analyze-website" and request.method == "POST":
                import re as _re
                url = data.get("url", "").strip()
                if not url:
                    return jsonify({"error": "Missing url"}), 400
                if not url.startswith(("http://", "https://")):
                    url = f"https://{url}"

                print(f"[ANALYZE-WEBSITE] Starting analysis for: {url} | tenant: {tenant_id}")

                try:
                    # Step 1: Fetch website HTML with a 10s timeout
                    r = httpx.get(
                        url, timeout=10, follow_redirects=True,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; Sideio/1.0; +https://sideio.com)"}
                    )
                    raw_html = r.text[:25000]

                    # Step 2: Strip HTML tags and collapse whitespace
                    clean_text = _re.sub(r'<[^>]+>', ' ', raw_html)
                    clean_text = _re.sub(r'\s+', ' ', clean_text).strip()[:4000]

                    if len(clean_text) < 80:
                        print(f"[ANALYZE-WEBSITE] Insufficient content extracted from {url}")
                        return jsonify({"error": "Insufficient content on page to analyze"}), 422

                    # Step 3: Gemini extraction prompt
                    prompt = f"""You are a B2B market intelligence engine. A user has provided their website content below.

Your job is to extract structured intelligence from it and return ONLY valid JSON — no markdown, no code blocks, no explanation.

--- WEBSITE CONTENT ---
{clean_text}
--- END CONTENT ---

Return a JSON object with this EXACT structure:
{{
  "company": {{
    "name": "Short company name",
    "description": "2-3 sentence description of what the company does and the value it provides",
    "value": "Core value proposition in 8 words or less"
  }},
  "targets": [
    {{"name": "Target Persona 1 Name", "description": "Who they are and why they need this company's services"}},
    {{"name": "Target Persona 2 Name", "description": "Who they are and why they need this company's services"}},
    {{"name": "Target Persona 3 Name", "description": "Who they are and why they need this company's services"}}
  ],
  "detected_gl": "ISO 2-letter country code based on company location (e.g. 'in', 'us', 'uk'). Use 'us' if unknown.",
  "recommended_campaigns": [
    {{
      "product_name": "Specific product or service to campaign for",
      "market_trend_hook": "Current market trend or pain point driving demand for this product",
      "unfair_advantage": "Why this company specifically wins against alternatives"
    }},
    {{
      "product_name": "Second product or service",
      "market_trend_hook": "Current market trend or pain point",
      "unfair_advantage": "Why this company specifically wins"
    }}
  ]
}}

Rules:
- All values must be strings. No nulls.
- Return ONLY the JSON object. No other text.
- If the website is in a non-English language, still return English output."""

                    model = GenerativeModel("gemini-2.5-flash")
                    gemini_resp = model.generate_content(
                        prompt,
                        generation_config=GenerationConfig(temperature=0.2)
                    )
                    raw_output = gemini_resp.text.strip()

                    # Strip any accidental markdown fences
                    raw_output = _re.sub(r'^```(?:json)?\s*', '', raw_output, flags=_re.M)
                    raw_output = _re.sub(r'\s*```$', '', raw_output, flags=_re.M)

                    persona_data = json.loads(raw_output)
                    print(f"[ANALYZE-WEBSITE] Successfully extracted persona for {url} | company: {persona_data.get('company', {}).get('name', 'unknown')}")
                    return jsonify({"status": "success", "data": persona_data}), 200

                except httpx.TimeoutException:
                    print(f"[ANALYZE-WEBSITE] Timeout fetching {url}")
                    return jsonify({"error": "Website took too long to respond"}), 422
                except httpx.RequestError as e:
                    print(f"[ANALYZE-WEBSITE] Network error fetching {url}: {e}")
                    return jsonify({"error": f"Could not reach website: {str(e)}"}), 422
                except json.JSONDecodeError as e:
                    print(f"[ANALYZE-WEBSITE] Gemini returned non-JSON for {url}: {e}")
                    return jsonify({"error": "AI analysis returned unexpected format"}), 422
                except Exception as e:
                    print(f"[ANALYZE-WEBSITE] Unexpected error for {url}: {e}")
                    return jsonify({"error": str(e)}), 422

            elif request.path == "/api/tenant_profiles" and request.method == "POST":
                is_valid, status_code, err_msg = check_quota(tenant_id)
                if not is_valid:
                    return jsonify({"error": err_msg}), status_code

                data['tenant_id'] = tenant_id
                data['createdAt'] = firestore.SERVER_TIMESTAMP
                data['updatedAt'] = firestore.SERVER_TIMESTAMP
                
                # Master Twin is one-time root level document
                db.collection("tenant_profiles").document(tenant_id).set(data, merge=True)
                return jsonify({"status": "success", "id": tenant_id}), 201

            elif request.path == "/api/tenant_profiles/extract-kb" and request.method == "POST":
                filepath = data.get("filepath")
                if not filepath:
                    return jsonify({"error": "Missing filepath"}), 400
                
                bucket_name = os.environ.get("FIREBASE_STORAGE_BUCKET", f"{PROJECT_ID}.appspot.com")
                print(f"[KB] Extracting document from gs://{bucket_name}/{filepath}")
                
                try:
                    storage_client = storage.Client()
                    bucket = storage_client.bucket(bucket_name)
                    blob = bucket.blob(filepath)
                    file_bytes = blob.download_as_bytes()
                    
                    extracted_text = ""
                    if filepath.lower().endswith('.pdf'):
                        pdf = PyPDF2.PdfReader(io.BytesIO(file_bytes))
                        extracted_text = "\\n".join(page.extract_text() for page in pdf.pages if page.extract_text())
                    elif filepath.lower().endswith('.txt'):
                        extracted_text = file_bytes.decode('utf-8', errors='ignore')
                    else:
                        return jsonify({"error": "Unsupported file format. Use PDF or TXT."}), 400
                        
                    if extracted_text.strip():
                        # Cap the length to avoid firing massive arrays into Firestore limits blindly
                        extracted_text = extracted_text.strip()[:10000] 
                        # Use ArrayUnion to push to knowledge_base_text
                        db.collection("tenant_profiles").document(tenant_id).update({
                            "knowledge_base_text": firestore.ArrayUnion([extracted_text])
                        })
                        
                        return jsonify({"status": "success", "message": "Knowledge base appended"}), 200
                    return jsonify({"error": "No textual content extracted"}), 400
                except Exception as e:
                    print(f"[KB] Failed extraction: {e}")
                    return jsonify({"error": f"Extraction failed: {str(e)}"}), 500
                
            elif request.path == "/api/campaigns" and request.method == "POST":
                is_valid, status_code, err_msg = check_quota(tenant_id)
                if not is_valid:
                    return jsonify({"error": err_msg}), status_code
                    
                # Schema Map: Location -> GL Logic
                loc_raw = (data.get('location') or '').strip().lower()
                gl_map = {
                    "usa": "us", "united states": "us", "uk": "uk", 
                    "united kingdom": "uk", "canada": "ca", "australia": "au",
                    "germany": "de", "singapore": "sg", "uae": "ae", 
                    "dubai": "ae", "india": "in"
                }
                
                # If explicit match, set GL. If not, Serper defaults to 'us' but 
                # loc_raw remains in 'location' string to be appended to Vertex Search Context.
                if loc_raw in gl_map:
                    data['gl'] = gl_map[loc_raw]
                elif loc_raw == "worldwide" or not loc_raw:
                    data['gl'] = "us" # default fallback
                else:
                    data['gl'] = "us" # custom cities fallback to US GL, append loc string elsewhere

                # Hard limit N active product/service campaigns per tenant
                active_campaigns_count = len(list(db.collection("campaigns")
                    .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                    .where(filter=FieldFilter("status", "==", "active"))
                    .stream()))
                
                if active_campaigns_count >= MAX_CHILD_CAMPAIGNS:
                    return jsonify({"error": f"Maximum of {MAX_CHILD_CAMPAIGNS} active product/service campaigns allowed per tenant."}), 403

                # --- RLHF Telemetry Sync ---
                human_edited = data.get("human_edited", False)
                if human_edited:
                    product_name = data.get("name", "").strip()
                    orig_hook = data.pop("orig_hook", "")
                    orig_adv = data.pop("orig_adv", "")
                    target_angle_hook = data.pop("target_angle_hook", orig_hook)
                    target_angle_adv = data.pop("target_angle_adv", orig_adv)
                    data.pop("human_edited", None)
                    
                    if product_name:
                        doc_id = ''.join(c for c in product_name.lower() if c.isalnum() or c in ['-', '_'])[:100]
                        if doc_id:
                            print(f"[RLHF] Human-edited feedback received for product '{product_name}'. Capturing telemetry...")
                            try:
                                db.collection("market_trend_cache").document(doc_id).set({
                                    "market_trend_hook": target_angle_hook,
                                    "unfair_advantage": target_angle_adv,
                                    "updatedAt": firestore.SERVER_TIMESTAMP,
                                    "rlhf_source_tenant": tenant_id
                                }, merge=True)
                            except Exception as e:
                                print(f"[RLHF] Failed to capture telemetry to market_trend_cache: {e}")
                # ---------------------------

                data['tenant_id'] = tenant_id
                data['createdAt'] = firestore.SERVER_TIMESTAMP
                data['updatedAt'] = firestore.SERVER_TIMESTAMP
                update_time, doc_ref = db.collection("campaigns").add(data)

                # V14: Gate the LLM vector classification to campaign creation only
                bio = data.get("bio", "")
                if bio:
                    weights = get_vector_weights()
                    vector = classify_sourcing_vector(bio, weights)
                    doc_ref.update({"sourcing_vector": vector})
                    print(f"[SYNAPTIC ROUTER] Campaign {doc_ref.id} classified as: '{vector}'")

                # ── V19: ZERO-WAIT DIRECT ENQUEUE ────────────────────────────────────────
                # Do NOT wait for the cron sweep. Push the Day-1 producer task directly
                # into Cloud Tasks immediately after document creation.
                #
                # Timestamp safety contract (prevents cron double-firing):
                #   next_produce_due = now + 24h  → cron locked out until Day 2
                #   next_drip_due    = now + 4h   → consumer fires on next 4-hour tick
                #   unprocessed_queue = []         → clean state; producer will populate it
                # ─────────────────────────────────────────────────────────────────────────
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                doc_ref.update({
                    "unprocessed_queue": [],
                    "next_produce_due":  now_utc + datetime.timedelta(hours=24),
                    "next_drip_due":     now_utc + datetime.timedelta(hours=4),
                })

                _zero_wait_enqueue_error = None
                try:
                    _base_url    = PIPELINE_URL.split("/dispatch")[0]
                    _produce_url = f"{_base_url}/produce"
                    _queue_path  = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
                    _sa_email    = get_service_account_email().strip()

                    # 5-second jitter prevents thundering-herd on bulk campaign creates
                    import random as _random
                    _jitter   = _random.randint(1, 5)
                    _sched_ts = timestamp_pb2.Timestamp()
                    _sched_ts.FromDatetime(now_utc + datetime.timedelta(seconds=_jitter))

                    _task = {
                        "http_request": {
                            "http_method": tasks_v2.HttpMethod.POST,
                            "url":         _produce_url,
                            "headers":     {"Content-Type": "application/json"},
                            "body":        json.dumps({
                                "tenant_id":   tenant_id,
                                "campaign_id": doc_ref.id,
                            }).encode(),
                        },
                        "schedule_time": _sched_ts,
                    }
                    if _sa_email:
                        _task["http_request"]["oidc_token"] = {
                            "service_account_email": _sa_email,
                            "audience":              _base_url,
                        }

                    tasks_client.create_task(request={"parent": _queue_path, "task": _task})
                    print(f"[ZERO-WAIT] Enqueued Day-1 producer for campaign {doc_ref.id} "
                          f"(jitter={_jitter}s, next_produce_due=+24h)")

                except Exception as _enq_err:
                    # Non-fatal: cron will pick this up within 5 minutes as a fallback.
                    # We never block campaign creation on an infrastructure failure.
                    _zero_wait_enqueue_error = str(_enq_err)
                    print(f"[ZERO-WAIT] Direct enqueue failed (non-fatal, cron fallback active): {_enq_err}")

                return jsonify({
                    "status":              "success",
                    "id":                  doc_ref.id,
                    "zero_wait_enqueued":  _zero_wait_enqueue_error is None,
                    "enqueue_error":       _zero_wait_enqueue_error,
                }), 201
                
            elif (request.path.startswith("/api/campaigns/")
                  and request.path.endswith("/run")
                  and request.method == "POST"):
                # ── EPSILON-GREEDY ROUTER (Phase 4) ──────────────────────────
                # Intercepts the user-triggered "Find Clients" action.
                # Blends V16 Autonomous (predictive_cache) + V14 Cartographer (Serper).
                campaign_id = request.path.split("/")[-2]  # /api/campaigns/{id}/run
                camp_ref    = db.collection("campaigns").document(campaign_id)
                camp_doc    = camp_ref.get()

                if not camp_doc.exists or camp_doc.to_dict().get("tenant_id") != tenant_id:
                    return jsonify({"error": "Forbidden"}), 403

                camp_data   = camp_doc.to_dict()
                batch_size  = int(camp_data.get("lead_target", data.get("batch_size", 10)))

                # Quota check
                role = (db.collection("users").document(tenant_id).get().to_dict() or {}).get("role")
                if role != "super_admin":
                    is_valid, status_code, err_msg = check_quota(tenant_id)
                    if not is_valid:
                        return jsonify({"error": err_msg}), status_code

                # Fetch router config (auto-initializes system_config/router if missing)
                router_cfg     = _get_router_config(db)
                exploit_ratio  = float(router_cfg.get("exploit_ratio", 0.10))

                # Epsilon-Greedy quota math
                autonomous_target  = max(0, round(batch_size * exploit_ratio))
                cartographer_target = batch_size - autonomous_target

                print(f"[ROUTER] batch={batch_size} | "
                      f"autonomous_target={autonomous_target} | "
                      f"cartographer_target={cartographer_target} | "
                      f"exploit_ratio={exploit_ratio}")

                audit_trail = []

                # ── Step A: EXPLOIT — pop from predictive_cache ──────────────
                promoted = []
                if autonomous_target > 0:
                    promoted = _pop_from_predictive_cache(tenant_id, db, autonomous_target)
                    deficit  = autonomous_target - len(promoted)
                    if deficit > 0:
                        # Cache miss: dynamically reallocate deficit to Cartographer
                        cartographer_target += deficit
                        print(f"[ROUTER] Cache deficit={deficit}. "
                              f"Cartographer target adjusted to {cartographer_target}")
                        audit_trail.append(
                            f"Cache deficit: {deficit} reallocated to Cartographer."
                        )
                    audit_trail.append(
                        f"Autonomous: {len(promoted)}/{autonomous_target} leads promoted from cache."
                    )

                # ── Step B: EXPLORE — enqueue Cartographer for remainder ──────
                produce_dispatched = 0
                if cartographer_target > 0:
                    try:
                        sa_email    = get_service_account_email().strip()
                        base_url    = PIPELINE_URL.split('/dispatch')[0]
                        queue_path  = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
                        import random
                        jitter      = random.randint(1, 30)
                        sched_t     = timestamp_pb2.Timestamp()
                        sched_t.FromDatetime(
                            datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(seconds=jitter)
                        )
                        task_body = {
                            "tenant_id":   tenant_id,
                            "campaign_id": campaign_id,
                            "lead_target": cartographer_target,
                        }
                        t = {
                            "http_request": {
                                "http_method": tasks_v2.HttpMethod.POST,
                                "url": f"{base_url}/produce",
                                "headers": {"Content-Type": "application/json"},
                                "body": json.dumps(task_body).encode()
                            }
                        }
                        if sa_email:
                            t["http_request"]["oidc_token"] = {
                                "service_account_email": sa_email,
                                "audience": base_url
                            }
                        t["schedule_time"] = sched_t
                        tasks_client.create_task(request={"parent": queue_path, "task": t})
                        produce_dispatched = 1
                        audit_trail.append(
                            f"Cartographer: producer queued for {cartographer_target} leads "
                            f"(jitter={jitter}s)."
                        )
                    except Exception as task_err:
                        print(f"[ROUTER] Cloud Tasks enqueue failed: {task_err}")
                        audit_trail.append(f"Cartographer enqueue failed: {task_err}")

                return jsonify({
                    "status":               "router_dispatched",
                    "batch_size":           batch_size,
                    "exploit_ratio":        exploit_ratio,
                    "autonomous_promoted":  len(promoted),
                    "cartographer_queued":  cartographer_target,
                    "producer_dispatched":  produce_dispatched,
                    "audit_trail":          audit_trail,
                }), 200

            elif request.path.startswith("/api/campaigns/") and request.method == "PUT":
                doc_id = request.path.split("/")[-1]
                # Secure Authorization Enforcement: Document MUST logically belong to Tenant
                doc_ref = db.collection("campaigns").document(doc_id)
                doc_data = doc_ref.get()
                if doc_data.exists and doc_data.to_dict().get('tenant_id') == tenant_id:
                    data['updatedAt'] = firestore.SERVER_TIMESTAMP
                    # V14: Re-classify sourcing vector if bio is being updated
                    if 'bio' in data and data['bio']:
                        weights = get_vector_weights()
                        data['sourcing_vector'] = classify_sourcing_vector(data['bio'], weights)
                        print(f"[SYNAPTIC ROUTER] Campaign {doc_id} re-classified as: '{data['sourcing_vector']}'")
                    doc_ref.update(data)
                    # V18: Cloud Tasks RLHF telemetry enqueue — guaranteed execution outside
                    # the HTTP lifecycle. CPU is never throttled by Cloud Run mid-insert.
                    _bq_status = data.get("status") or data.get("crm_status") or "updated"
                    _bq_lead   = doc_data.to_dict()
                    _enqueue_bq_telemetry_task(tenant_id, _bq_lead, _bq_status)
                    return jsonify({"status": "success"}), 200
                return jsonify({"error": "Forbidden"}), 403
                
            elif request.path.startswith("/api/leads/") and request.method == "PUT":
                doc_id = request.path.split("/")[-1]
                doc_ref = db.collection("leads").document(doc_id)
                doc_data = doc_ref.get()
                if doc_data.exists and doc_data.to_dict().get('tenant_id') == tenant_id:
                    data['updatedAt'] = firestore.SERVER_TIMESTAMP
                    if 'interactions' in data:
                        # Prevent client array mutation overwrites
                        db_interaction = {"action": data.get("interactions", "") , "date": firestore.SERVER_TIMESTAMP}
                        doc_ref.update({"status": data.get("status"), "updatedAt": firestore.SERVER_TIMESTAMP, "interactions": firestore.ArrayUnion([db_interaction])})
                    else:
                        doc_ref.update(data)
                        
                    # RLHF Backpropagation logic (Zero-cost ML loop)
                    status = data.get("status")
                    if status in ["converted", "ignored"]:
                        delta = 1 if status == "converted" else -1
                        user_ref = db.collection("users").document(tenant_id)
                        pref_updates = {}
                        
                        lead_dict = doc_data.to_dict()
                        tech_stack = lead_dict.get("tech_stack_found", [])
                        hiring_intent = lead_dict.get("hiring_intent_found", "")
                        
                        if hiring_intent and hiring_intent.lower() != "none":
                            pref_updates["preferences_weights.hiring_intent"] = firestore.Increment(delta)
                            
                        for tech in tech_stack:
                            pref_updates[f"preferences_weights.tech_{tech}"] = firestore.Increment(delta)
                            
                        if status == "ignored":
                            import re
                            pain_point = lead_dict.get("pain_point", "")
                            words = list(set(re.findall(r'\b\w{4,}\b', pain_point.lower())))
                            extracted = words[:3]
                            if isinstance(tech_stack, list) and tech_stack:
                                extracted.extend([t.lower() for t in tech_stack[:2]])
                            if extracted:
                                pref_updates["dynamic_blocklist"] = firestore.ArrayUnion(extracted)
                            
                        if pref_updates:
                            try:
                                user_ref.set(pref_updates, merge=True)
                            except Exception as e:
                                print(f"RLHF Backprop Native Error: {e}")

                    # ── Phase 1: Ontology Map RLHF Hooks ───────────────────────────
                    # Reward: Won / Negotiating  → +0.15 to baseline_weight
                    # Penalty: Lost             → -0.05 to baseline_weight
                    # Burn-in guard: only apply math if total_yield >= 50
                    crm_status = data.get("crm_status")
                    if crm_status in ["won", "negotiating", "lost"]:
                        lead_dict     = doc_data.to_dict()
                        source_url    = lead_dict.get("source_url", lead_dict.get("url", ""))
                        base_path_key = parse_base_path(source_url)
                        if base_path_key and base_path_key != 'unknown':
                            try:
                                ontology_ref  = db.collection('ontology_map').document(base_path_key)
                                ontology_snap = ontology_ref.get()
                                if ontology_snap.exists:
                                    total_yield = ontology_snap.to_dict().get('total_yield', 0)
                                    if total_yield >= 50:
                                        # Burn-in guardrail cleared — apply real delta
                                        if crm_status in ["won", "negotiating"]:
                                            delta_weight = 0.15
                                            print(f"[ONTOLOGY RLHF] Reward +{delta_weight} → {base_path_key}")
                                        else:  # lost
                                            delta_weight = -0.05
                                            print(f"[ONTOLOGY RLHF] Penalty {delta_weight} → {base_path_key}")
                                        ontology_ref.update({
                                            'baseline_weight': firestore.Increment(delta_weight),
                                            'last_seen':       firestore.SERVER_TIMESTAMP
                                        })
                                    else:
                                        # Burn-in period: log interaction, keep weight at 1.0
                                        print(f"[ONTOLOGY RLHF] Burn-in ({total_yield}/50 yields) — "
                                              f"{crm_status} logged for {base_path_key}, weight unchanged.")
                                else:
                                    print(f"[ONTOLOGY RLHF] No ontology doc for {base_path_key} yet.")
                            except Exception as re:
                                print(f"[ONTOLOGY RLHF] Write failed for {base_path_key}: {re}")

                    # V14: Headless CRM Egress — stripped payload, no raw DOM
                    if status == "converted":
                        try:
                            user_crm_doc = db.collection("users").document(tenant_id).get().to_dict() or {}
                            crm_webhook_url = user_crm_doc.get("crm_webhook_url")
                            if crm_webhook_url:
                                crm_payload = {
                                    "lead_id":          doc_id,
                                    "score":            lead_dict.get("score"),
                                    "dm":               lead_dict.get("dm"),
                                    "intent_signal":    lead_dict.get("intent_signal", ""),
                                    "contact_endpoints":lead_dict.get("contact_endpoints", [])
                                }
                                httpx.post(crm_webhook_url, json=crm_payload, timeout=5)
                                print(f"[CRM EGRESS] Stripped payload fired for lead {doc_id}.")
                        except Exception as crm_e:
                            print(f"[CRM EGRESS] Webhook failed: {crm_e}")

                    return jsonify({"status": "success"}), 200
                return jsonify({"error": "Forbidden"}), 403
                
            elif request.path == "/api/settings" and request.method == "POST":
                # BYOT Vault implementation natively tracking symmetric cryptography
                user_ref = db.collection("users").document(uid)
                wa_token_raw = data.get("wa_token")
                wa_phone_id = data.get("wa_phone_id")
                admin_phone = data.get("admin_phone")
                
                settings_update = {}
                if wa_phone_id: settings_update["wa_phone_id"] = wa_phone_id
                if admin_phone: settings_update["admin_phone"] = admin_phone
                if wa_token_raw:
                    encrypted_token = None
                    try:
                        sm_client = secretmanager.SecretManagerServiceClient()
                        # Fallback to hardcoded pipeline if not set
                        project_id_conf = os.environ.get("PROJECT_ID", "sideio-leads-v16")
                        key_name = sm_client.access_secret_version(request={"name": f"projects/{project_id_conf}/secrets/kms_wa_key_path/versions/latest"}).payload.data.decode("UTF-8").strip()
                        
                        kms_client = kms.KeyManagementServiceClient()
                        response = kms_client.encrypt(
                            request={'name': key_name, 'plaintext': wa_token_raw.encode('utf-8')}
                        )
                        encrypted_token = base64.b64encode(response.ciphertext).decode('utf-8')
                        settings_update["wa_token"] = encrypted_token
                    except Exception as e:
                        print(f"KMS Encryption Failed: {e}. Falling back to symmetric Fernet.")
                        encrypted_token = cipher_suite.encrypt(wa_token_raw.encode()).decode()
                        settings_update["wa_token"] = encrypted_token
                
                if settings_update:
                    settings_update["updatedAt"] = firestore.SERVER_TIMESTAMP
                    user_ref.update(settings_update)
                return jsonify({"status": "success"}), 200
                
        except ValueError as ve:
            return jsonify({"error": "Unauthorized", "message": str(ve)}), 401
        except Exception as e:
            return jsonify({"error": "Internal Error", "message": str(e)}), 500

    # -----------------------------------------------------------------------------------------
    # Legacy Internal Triggers (Admin/Purge/Sweep)
    # -----------------------------------------------------------------------------------------
    # ── V18: Internal telemetry BQ push (dispatched via Cloud Tasks) ──────────
    # Auth: Cloud Tasks attaches an OIDC token. We verify it came from our own
    # queue by checking the task header — full OIDC verify handled by IAP/Cloud Run.
    # No user auth needed: this endpoint is NOT unauthenticated-accessible because
    # the orchestrator Cloud Run service has allow-unauthenticated BUT Cloud Tasks
    # always sends a valid OIDC token; without it, the BQ payload would be missing.
    if request.path == "/api/internal/telemetry/bq-push" and request.method == "POST":
        # Lightweight guard: reject requests without a Cloud-Tasks header
        if not request.headers.get("X-CloudTasks-QueueName"):
            return jsonify({"error": "Forbidden — direct access not allowed"}), 403

        payload = request.json or {}
        if not payload.get("event_id") or not payload.get("tenant_id"):
            return jsonify({"error": "Invalid payload"}), 400

        success = _handle_bq_push_task(payload)
        status_code = 200 if success else 500
        return jsonify({"ok": success}), status_code

    if request.path == "/purge" and request.method == "POST":
        return handle_purge(request)

    # -----------------------------------------------------------------------------------------
    # Master Cron Continuous Sweep (5-Minute Interval)
    # -----------------------------------------------------------------------------------------
    if request.path == "/api/internal/cron/sweep" and request.method == "POST":
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing OIDC token"}), 401
            
        token = auth_header.split("Bearer ")[1]
        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests
            claim = id_token.verify_oauth2_token(token, google_requests.Request())
        except Exception as e:
            return jsonify({"error": "Invalid OIDC token", "details": str(e)}), 403
            
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # Pull active campaigns with an aggressive limit of 500
        campaigns = list(db.collection("campaigns").where(filter=FieldFilter("status", "==", "active")).limit(500).stream())

        audit_trail = ["Executed V14.4 Dual-Mode Sweep (Producer=24h / Consumer=4h)."]
        queue_path  = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
        sa_email    = get_service_account_email().strip()
        base_url    = PIPELINE_URL.split('/dispatch')[0]   # base URL without /dispatch
        produce_url = f"{base_url}/produce"
        consume_url = f"{base_url}/dispatch"

        produce_dispatched = 0
        consume_dispatched = 0
        import random

        for camp_doc in campaigns:
            campaign_data = camp_doc.to_dict()
            campaign_id   = camp_doc.id
            tenant_id     = campaign_data.get("tenant_id")
            if not tenant_id:
                continue

            # Wallet / quota check
            role = (db.collection("users").document(tenant_id).get().to_dict() or {}).get("role")
            if role != "super_admin":
                is_valid, _, err_msg = check_quota(tenant_id)
                if not is_valid:
                    audit_trail.append(f"🚫 SKIPPED {campaign_id}: {err_msg}")
                    continue

            def _oidc_task(url, payload):
                t = {
                    "http_request": {
                        "http_method": tasks_v2.HttpMethod.POST,
                        "url": url,
                        "headers": {"Content-Type": "application/json"},
                        "body": json.dumps(payload).encode()
                    }
                }
                if sa_email:
                    t["http_request"]["oidc_token"] = {
                        "service_account_email": sa_email,
                        "audience": base_url
                    }
                return t

            # ── PRODUCER: fire once every 24 hours ───────────────────────────
            PRODUCE_INTERVAL_H = 24
            next_produce_due   = campaign_data.get("next_produce_due")
            produce_due = True
            if next_produce_due and hasattr(next_produce_due, "timestamp"):
                npd = next_produce_due
                if npd.tzinfo is None:
                    npd = npd.replace(tzinfo=datetime.timezone.utc)
                if npd > now_utc:
                    produce_due = False

            if produce_due:
                jitter  = random.randint(1, 120)
                sched_t = timestamp_pb2.Timestamp()
                sched_t.FromDatetime(now_utc + datetime.timedelta(seconds=jitter))
                task    = _oidc_task(produce_url, {"tenant_id": tenant_id, "campaign_id": campaign_id})
                task["schedule_time"] = sched_t
                tasks_client.create_task(request={"parent": queue_path, "task": task})
                camp_doc.reference.update({
                    "next_produce_due": now_utc + datetime.timedelta(hours=PRODUCE_INTERVAL_H)
                })
                produce_dispatched += 1
                audit_trail.append(f"🏭 PRODUCER queued for {campaign_id} (jitter={jitter}s, next in {PRODUCE_INTERVAL_H}h)")

            # ── CONSUMER: fire every 4 hours, only if queue has items ────────
            DRIP_INTERVAL_H  = 4
            next_drip_due    = campaign_data.get("next_drip_due")
            drip_due = True
            if next_drip_due and hasattr(next_drip_due, "timestamp"):
                ndd = next_drip_due
                if ndd.tzinfo is None:
                    ndd = ndd.replace(tzinfo=datetime.timezone.utc)
                if ndd > now_utc:
                    drip_due = False

            queue_depth = len(campaign_data.get("unprocessed_queue", []))

            if drip_due:
                if queue_depth == 0:
                    audit_trail.append(f"⏸ CONSUMER skipped {campaign_id}: queue empty (depth=0)")
                else:
                    jitter  = random.randint(1, 290)
                    sched_t = timestamp_pb2.Timestamp()
                    sched_t.FromDatetime(now_utc + datetime.timedelta(seconds=jitter))
                    task    = _oidc_task(consume_url, {"tenant_id": tenant_id, "campaign_id": campaign_id})
                    task["schedule_time"] = sched_t
                    tasks_client.create_task(request={"parent": queue_path, "task": task})
                    camp_doc.reference.update({
                        "next_drip_due":          now_utc + datetime.timedelta(hours=DRIP_INTERVAL_H),
                        "drip_interval_minutes":  DRIP_INTERVAL_H * 60
                    })
                    consume_dispatched += 1
                    audit_trail.append(f"⚙️ CONSUMER queued for {campaign_id} (queue_depth={queue_depth}, jitter={jitter}s, next in {DRIP_INTERVAL_H}h)")

        return jsonify({
            "message":            f"V14.4 Sweep: {produce_dispatched} producers + {consume_dispatched} consumers dispatched.",
            "produce_dispatched": produce_dispatched,
            "consume_dispatched": consume_dispatched,
            "audit_trail":        audit_trail
        }), 200

    # -----------------------------------------------------------------------------------------
    # V14: REVERSE-RLHF — Headless Conversion Feedback Endpoint
    # Auth: X-API-Key (static key from Secret Manager, bypasses Firebase UI Auth)
    # -----------------------------------------------------------------------------------------
    if request.path == "/api/telemetry/conversion_feedback" and request.method == "POST":
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return jsonify({"error": "Unauthorized", "message": "Missing X-API-Key header."}), 401
        try:
            stored_key = sm_client.access_secret_version(
                request={"name": f"projects/{PROJECT_ID}/secrets/api_gateway_key/versions/latest"}
            ).payload.data.decode("UTF-8").strip()
        except Exception as e:
            print(f"[REVERSE-RLHF] Secret Manager fetch failed: {e}")
            return jsonify({"error": "Internal Error", "message": "Key validation unavailable."}), 500

        if api_key != stored_key:
            return jsonify({"error": "Forbidden", "message": "Invalid API key."}), 403

        data       = request.json or {}
        lead_id    = data.get("lead_id")
        status     = data.get("status")
        if not lead_id or status not in ["converted", "rejected"]:
            return jsonify({"error": "Bad Request", "message": "Requires lead_id and status: converted|rejected"}), 400

        lead_doc = db.collection("leads").document(lead_id).get()
        if not lead_doc.exists:
            return jsonify({"error": "Not Found", "message": f"Lead {lead_id} not found."}), 404

        lead_dict       = lead_doc.to_dict()
        tenant_id       = lead_dict.get("tenant_id")
        tech_stack      = lead_dict.get("tech_stack_found", [])
        sourcing_vector = lead_dict.get("sourcing_vector", "Classic B2B")
        hiring_intent   = lead_dict.get("hiring_intent_found", "No")
        delta           = 1 if status == "converted" else -1

        # 1. Tenant RLHF backpropagation
        pref_updates = {}
        if hiring_intent == "Yes":
            pref_updates["preferences_weights.hiring_intent"] = firestore.Increment(delta)
        for tech in tech_stack:
            pref_updates[f"preferences_weights.tech_{tech}"] = firestore.Increment(delta)
        if status == "rejected":
            import re
            pain_point = lead_dict.get("pain_point", "")
            words = list(set(re.findall(r'\b\w{4,}\b', pain_point.lower())))[:3]
            if words:
                pref_updates["dynamic_blocklist"] = firestore.ArrayUnion(words)
        if tenant_id and pref_updates:
            try:
                db.collection("users").document(tenant_id).set(pref_updates, merge=True)
            except Exception as e:
                print(f"[REVERSE-RLHF] Tenant backprop failed: {e}")

        # 2. Global vector_weights update
        try:
            db.collection("system_telemetry").document("vector_weights").set(
                {sourcing_vector: firestore.Increment(delta)}, merge=True
            )
        except Exception as e:
            print(f"[REVERSE-RLHF] Global vector update failed: {e}")

        # 3. Update lead status
        try:
            db.collection("leads").document(lead_id).update(
                {"status": status, "updatedAt": firestore.SERVER_TIMESTAMP}
            )
        except Exception as e:
            print(f"[REVERSE-RLHF] Lead status update failed: {e}")

        return jsonify({"status": "ok", "delta": delta, "vector": sourcing_vector}), 200

    # -----------------------------------------------------------------------------------------
    # V14: AI REFLECTION LOOP — Weekly Global Auto-Tuning (OIDC-protected cron)
    # -----------------------------------------------------------------------------------------
    if request.path == "/api/internal/cron/reflection" and request.method == "POST":
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing OIDC token"}), 401
        token = auth_header.split("Bearer ")[1]
        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests
            id_token.verify_oauth2_token(token, google_requests.Request())
        except Exception as e:
            return jsonify({"error": "Invalid OIDC token", "details": str(e)}), 403

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        cutoff  = now_utc - datetime.timedelta(days=7)

        # Sample global converted and failed leads (past 7 days) — strictly PII-scrubbed
        scrubbed = []
        for outcome_status in ["converted", "failed"]:
            try:
                docs = (
                    db.collection("leads")
                    .where("status", "==", outcome_status)
                    .where("updatedAt", ">=", cutoff)
                    .limit(50)
                    .stream()
                )
                for doc in docs:
                    d = doc.to_dict()
                    scrubbed.append({
                        "outcome":          d.get("status"),
                        "score":            d.get("score"),
                        "sourcing_vector":  d.get("sourcing_vector", "Classic B2B"),
                        "confidence_tier":  d.get("confidence_tier", "High"),
                        "hiring_intent":    d.get("hiring_intent_found", "No"),
                        "tech_stack":       d.get("tech_stack_found", []),
                        "company_size":     d.get("company_size_tier", "Unknown"),
                        # Truncated pain point — no names, emails, URLs, phones
                        "pain_theme":       (d.get("pain_point") or "")[:80]
                    })
            except Exception as e:
                print(f"[REFLECTION] Sampling failed for status={outcome_status}: {e}")

        if not scrubbed:
            return jsonify({"status": "no_data", "message": "Insufficient sample for reflection."}), 200

        current_weights = get_vector_weights()

        reflection_prompt = f"""You are a global B2B outreach intelligence system performing a weekly strategic audit.

Analyze these {len(scrubbed)} anonymized lead outcomes from the past 7 days to identify emerging platform intent trends.

CURRENT VECTOR WEIGHTS:
{json.dumps(current_weights, indent=2)}

LEAD OUTCOMES (fully PII-scrubbed — no names, emails, URLs, or phone numbers):
{json.dumps(scrubbed, indent=2)}

Based on which sourcing_vector values are correlated with more 'converted' outcomes vs 'failed' outcomes:
1. Identify which vectors are over-performing and which are under-performing.
2. Output ONLY a valid JSON object with updated integer weights for each vector.

Vector keys MUST be exactly: "Classic B2B", "Social/Forum Listening", "Review Hijacking", "Maps/GMB Targeting".
Example output: {{"Classic B2B": 14, "Social/Forum Listening": 11, "Review Hijacking": 4, "Maps/GMB Targeting": 6}}"""

        try:
            model    = GenerativeModel("gemini-2.5-flash")
            response = model.generate_content(
                reflection_prompt,
                generation_config=GenerationConfig(response_mime_type="application/json")
            )
            new_weights = json.loads(response.text)
            if not isinstance(new_weights, dict):
                raise ValueError("Reflection output is not a JSON object.")
            # Validate keys
            valid_keys = {"Classic B2B", "Social/Forum Listening", "Review Hijacking", "Maps/GMB Targeting"}
            new_weights = {k: int(v) for k, v in new_weights.items() if k in valid_keys}
        except Exception as e:
            return jsonify({"error": "Reflection LLM failed", "details": str(e)}), 500

        db.collection("system_telemetry").document("vector_weights").set(new_weights)
        print(f"[REFLECTION] vector_weights updated: {new_weights}")
        return jsonify({"status": "reflection_complete", "sample_size": len(scrubbed), "new_weights": new_weights}), 200

    # ──────────────────────────────────────────────────────────────────────────────────────
    # Phase 1: ONTOLOGY DECAY CRON — Monthly Regression-to-the-Mean
    # OIDC-protected. Triggered by Cloud Scheduler on a 1/month schedule.
    # Math: new_weight = weight - (weight - 1.0) * 0.10
    #   e.g.  1.5  →  1.5  - (0.5 * 0.10)  = 1.45
    #          0.8  →  0.8  - (-0.2 * 0.10) = 0.82
    # ──────────────────────────────────────────────────────────────────────────────────────
    if request.path == "/api/internal/cron/ontology-decay" and request.method == "POST":
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing OIDC token"}), 401
        token = auth_header.split("Bearer ")[1]
        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests
            id_token.verify_oauth2_token(token, google_requests.Request())
        except Exception as e:
            return jsonify({"error": "Invalid OIDC token", "details": str(e)}), 403

        # Iterate every document in ontology_map and apply decay
        updated_count  = 0
        skipped_count  = 0
        errored_count  = 0
        decay_log      = []

        try:
            docs = db.collection('ontology_map').stream()
            for doc in docs:
                d      = doc.to_dict()
                weight = d.get('baseline_weight', 1.0)
                diff   = weight - 1.0

                if abs(diff) < 0.001:  # already at or within float epsilon of 1.0
                    skipped_count += 1
                    continue

                # Regression-to-mean: reduce the deviation from 1.0 by 10%
                new_weight = round(weight - diff * 0.10, 6)

                try:
                    db.collection('ontology_map').document(doc.id).update({
                        'baseline_weight': new_weight,
                        'last_decayed':    firestore.SERVER_TIMESTAMP
                    })
                    decay_log.append({"path": doc.id, "old": weight, "new": new_weight})
                    updated_count += 1
                    print(f"[DECAY] {doc.id}: {weight:.4f} → {new_weight:.4f}")
                except Exception as write_err:
                    print(f"[DECAY] Write failed for {doc.id}: {write_err}")
                    errored_count += 1

        except Exception as scan_err:
            return jsonify({"error": "Ontology scan failed", "details": str(scan_err)}), 500

        print(f"[DECAY] Complete. updated={updated_count} skipped={skipped_count} errors={errored_count}")
        return jsonify({
            "status":        "decay_complete",
            "updated":       updated_count,
            "skipped":       skipped_count,
            "errors":        errored_count,
            "decay_applied": decay_log
        }), 200

    return jsonify({"error": "Not Found"}), 404
