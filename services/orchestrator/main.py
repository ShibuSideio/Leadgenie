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
    HTTP Cloud Function triggered by Cloud Scheduler daily at 6AM.
    Retrieves all active campaigns and enqueues tasks into Cloud Tasks.
    """
    print("Triggering daily sweep orchestrator...")
    campaigns = db.collection("campaigns").where("status", "==", "active").stream()
    
    queue_path = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
    
    count = 0
    for camp_doc in campaigns:
        campaign_data = camp_doc.to_dict()
        tenant_id = campaign_data.get("tenant_id")
        campaign_id = camp_doc.id
        
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
        print(f"Created task {response.name} for campaign {campaign_id}")
        count += 1
        
    return f"Successfully queued {count} campaign jobs.", 200
