import os
import json
from google.cloud import firestore
from google.cloud import tasks_v2

db = firestore.Client()
tasks_client = tasks_v2.CloudTasksClient()

PROJECT_ID = os.environ.get("PROJECT_ID", "sideio-leads-v16")
LOCATION = os.environ.get("LOCATION", "asia-south1")
QUEUE = os.environ.get("QUEUE", "lead-pipeline-queue")
PIPELINE_URL = os.environ.get("PIPELINE_URL", "https://lead-pipeline-main-abc.a.run.app/dispatch")

def trigger_daily_sweep(request):
    """
    HTTP Cloud Function triggered by Cloud Scheduler daily at 6AM or via manual UI proxy.
    """
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
        
        # Enqueue to Cloud Tasks
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": PIPELINE_URL,
                "headers": {"Content-type": "application/json"},
                "body": json.dumps({"tenant_id": tenant_id, "campaign_id": campaign_id}).encode()
            }
        }
        
        response = tasks_client.create_task(request={"parent": queue_path, "task": task})
        count += 1
        
    return f"Successfully queued {count} campaign jobs.", 200, headers
