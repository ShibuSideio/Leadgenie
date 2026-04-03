import os
import json
import urllib.request
import time
import datetime
from flask import Flask, request, jsonify, make_response
from google.cloud import tasks_v2
from cryptography.fernet import Fernet

app = Flask(__name__)
ALLOWED_ORIGINS = ["https://lead-sniper-prod.web.app", "https://lead-sniper-prod.firebaseapp.com"]

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

# Initialize Admin SDK once natively for Thin Client API Authorization
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()
tasks_client = tasks_v2.CloudTasksClient()

PROJECT_ID = os.environ.get("PROJECT_ID", "sideio-leads-v16")
LOCATION = os.environ.get("LOCATION", "asia-south1")
QUEUE = os.environ.get("QUEUE", "lead-pipeline-queue")
PIPELINE_URL = os.environ.get("PIPELINE_URL", "https://lead-pipeline-main-abc.a.run.app/dispatch")

FERNET_KEY = os.environ.get("ENCRYPTION_KEY", "uNqG8Jc-44SjK22N8B5-2GksnE5F_88_V5wQZ02j1A0=")
cipher_suite = Fernet(FERNET_KEY.encode())

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
        if data.get("approval_status") != "approved":
            return False, 403, "Your application is under review. Please wait for L0 approval."
            
        beta_expiry = data.get("beta_expiry")
        if not beta_expiry:
            return False, 401, "Beta access has expired."
            
        now = datetime.datetime.now(datetime.timezone.utc)
        if hasattr(beta_expiry, 'tzinfo') and beta_expiry.tzinfo is None:
             beta_expiry = beta_expiry.replace(tzinfo=datetime.timezone.utc)
             
        if now > beta_expiry:
            return False, 401, "Beta access has expired."

        wallet = data.get("wallet", {})
        if wallet:
            allocated = int(wallet.get("allocated_credits", 0))
            consumed = int(wallet.get("consumed_credits", 0))
            if (allocated - consumed) <= 0:
                return False, 402, "Beta quota exhausted. Contact admin to reload."
        return True, 200, "OK"
    return False, 401, "Unknown identity."

def sanitize_document(doc):
    """
    Statically unpacks and sanitizes Firestore Documents dynamically serializing Timestamps securely.
    """
    data = doc.to_dict()
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
    if request.path in ["/api/campaigns", "/api/leads"] and request.method == "GET":
        try:
            uid, tenant_id, user_role = authenticate_request(request)
            
            if request.path == "/api/campaigns":
                docs = db.collection("campaigns").where(field_path="tenant_id", op_string="==", value=tenant_id).limit(100).stream()
                
            elif request.path == "/api/leads":
                # Apply explicit server-side sorting logic if indexing allows, otherwise stream natively.
                docs = db.collection("leads").where(field_path="tenant_id", op_string="==", value=tenant_id).limit(100).stream()

            elif request.path == "/api/me":
                user_doc = db.collection("users").document(uid).get()
                if user_doc.exists:
                    return jsonify({"status": "success", "data": user_doc.to_dict(), "wallet": user_doc.to_dict().get("wallet", {"allocated_credits": 0, "consumed_credits": 0})}), 200
                return jsonify({"error": "User structure missing"}), 404

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
                
            if request.path == "/api/l0/users" and request.method == "GET":
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
    if request.path.startswith("/api/") and request.method in ["POST", "PUT"]:
        try:
            uid, tenant_id, user_role = authenticate_request(request)
            data = request.json or {}
            
            # Remove any forged tenant injections
            data.pop('tenant_id', None)
            
            if request.path == "/api/campaigns" and request.method == "POST":
                is_valid, status_code, err_msg = check_quota(tenant_id)
                if not is_valid:
                    return jsonify({"error": err_msg}), status_code

                data['tenant_id'] = tenant_id
                data['createdAt'] = firestore.SERVER_TIMESTAMP
                data['updatedAt'] = firestore.SERVER_TIMESTAMP
                update_time, doc_ref = db.collection("campaigns").add(data)
                return jsonify({"status": "success", "id": doc_ref.id}), 201
                
            elif request.path.startswith("/api/campaigns/") and request.method == "PUT":
                doc_id = request.path.split("/")[-1]
                # Secure Authorization Enforcement: Document MUST logically belong to Tenant
                doc_ref = db.collection("campaigns").document(doc_id)
                doc_data = doc_ref.get()
                if doc_data.exists and doc_data.to_dict().get('tenant_id') == tenant_id:
                    data['updatedAt'] = firestore.SERVER_TIMESTAMP
                    doc_ref.update(data)
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
    if request.path == "/purge" and request.method == "POST":
        return handle_purge(request)

    # -----------------------------------------------------------------------------------------
    # Legacy Cloud Scheduler / Manual Execution Triggers
    # -----------------------------------------------------------------------------------------
    manual_camp_id = None
    if request.method == "POST":
        try:
            data = request.json
            if data and "campaign_id" in data:
                manual_camp_id = data["campaign_id"]
        except:
            pass

    print(f"Triggering orchestrator. Manual Mode: {manual_camp_id}")
    
    if manual_camp_id:
        campaigns = [db.collection("campaigns").document(manual_camp_id)]
    else:
        campaigns = list(db.collection("campaigns").where(field_path="status", op_string="==", value="active").limit(100).stream())
    
    queue_path = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
    
    count = 0
    for camp_doc in campaigns:
        if manual_camp_id:
           camp_snap = camp_doc.get()
           if not camp_snap.exists: continue
           campaign_data = camp_snap.to_dict()
           campaign_id = manual_camp_id
        else:
           campaign_data = camp_doc.to_dict()
           campaign_id = camp_doc.id

        tenant_id = campaign_data.get("tenant_id")
        if not tenant_id: continue
        
        is_valid, _, _ = check_quota(tenant_id)
        if not is_valid:
            continue
        
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": PIPELINE_URL,
                "headers": {"Content-type": "application/json"},
                "body": json.dumps({"tenant_id": tenant_id, "campaign_id": campaign_id}).encode()
            }
        }
        
        sa_email = get_service_account_email().strip()
        if sa_email:
            base_url_audience = PIPELINE_URL.split('/dispatch')[0]
            task["http_request"]["oidc_token"] = {
                "service_account_email": sa_email,
                "audience": base_url_audience
            }
        
        tasks_client.create_task(request={"parent": queue_path, "task": task})
        count += 1
        
    return jsonify({"message": f"Successfully queued {count} campaign jobs."}), 200
