import os
import json
import urllib.request
import time
from google.cloud import firestore
from google.cloud import tasks_v2

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

db = firestore.Client()
tasks_client = tasks_v2.CloudTasksClient()

PROJECT_ID = os.environ.get("PROJECT_ID", "sideio-leads-v16")
LOCATION = os.environ.get("LOCATION", "asia-south1")
QUEUE = os.environ.get("QUEUE", "lead-pipeline-queue")
PIPELINE_URL = os.environ.get("PIPELINE_URL", "https://lead-pipeline-main-abc.a.run.app/dispatch")

def handle_purge(request):
    data = request.json or {}
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return "Missing tenant_id", 400
        
    print(f"INITIATING DATA ERASURE DPDP COMPLIANCE FOR TENANT: {tenant_id}")
    
    # 1. Purge Campaigns
    campaigns = db.collection("campaigns").where("tenant_id", "==", tenant_id).stream()
    for doc in campaigns:
        doc.reference.delete()
        
    # 2. Purge Leads & Linked Scraped Caches
    leads = db.collection("leads").where("tenant_id", "==", tenant_id).stream()
    for doc in leads:
        lead_data = doc.to_dict()
        url = lead_data.get("url")
        if url:
            cache_id = url.replace('/','_')
            db.collection("scraped_cache").document(cache_id).delete()
        doc.reference.delete()
        
    # 3. Purge Tenant Baseline
    db.collection("tenants").document(tenant_id).delete()
    
    return f"Successfully erased tenant {tenant_id} data completely", 200

def trigger_daily_sweep(request):
    """
    HTTP Cloud Function triggered by Cloud Scheduler daily at 6AM or via manual UI proxy.
    """
    if request.path == "/purge" and request.method == "POST":
        return handle_purge(request)

    if request.method == "OPTIONS":
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '3600'
        }
        return ('', 204, headers)

    headers = {'Access-Control-Allow-Origin': '*'}
    
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
        campaigns = list(db.collection("campaigns").where("status", "==", "active").stream())
    
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
        
        # Securely bypass internal OIDC firewall constraints natively
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
            # Cloud Run strictly rejects OIDC tokens if the audience string includes the HTTP path.
            # We must aggressively strip '/dispatch' from the dynamic PIPELINE_URL to build a clean Base Target Audience.
            base_url_audience = PIPELINE_URL.split('/dispatch')[0]
            
            task["http_request"]["oidc_token"] = {
                "service_account_email": sa_email,
                "audience": base_url_audience
            }
        
        response = tasks_client.create_task(request={"parent": queue_path, "task": task})
        count += 1
        
    return f"Successfully queued {count} campaign jobs.", 200, headers
