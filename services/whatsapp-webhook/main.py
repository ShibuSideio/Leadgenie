import os
import hashlib
import hmac
from flask import Flask, request, jsonify
from google.cloud import firestore
from google.cloud import secretmanager

app = Flask(__name__)
db = firestore.Client()
project_id = os.environ.get("PROJECT_ID", "sideio-leads-v16")
sm_client = secretmanager.SecretManagerServiceClient()

VERIFY_TOKEN_NAME = f"projects/{project_id}/secrets/whatsapp_webhook_token/versions/latest"

def get_secret(secret_name):
    response = sm_client.access_secret_version(request={"name": secret_name})
    return response.payload.data.decode("UTF-8")

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
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
        if not signature.startswith("sha256="):
            return "Forbidden", 403
            
        payload = request.get_data()
        verify_token = get_secret(VERIFY_TOKEN_NAME)
        expected_hash = hmac.new(verify_token.encode('utf-8'), payload, hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(f"sha256={expected_hash}", signature):
            return "Forbidden", 403

        data = request.json
        print(f"Received Meta Webhook Data: {data}")
        # Parse logic to update message status (delivered, read, replied)
        # Assuming entries contains statuses
        try:
            entries = data.get("entry", [])
            for entry in entries:
                changes = entry.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    statuses = value.get("statuses", [])
                    for status in statuses:
                        message_id = status.get("id")
                        status_type = status.get("status") # sent, delivered, read
                        
                        # Find the lead doc. In reality, you would map WhatsApp Message ID to your lead ID.
                        # This is a sample query that will search across all tenants to update the status.
                        leads_query = db.collection_group("leads").where("wa_message_id", "==", message_id).stream()
                        for lead in leads_query:
                            lead.reference.update({"status": status_type})
                            print(f"Updated lead {lead.id} to {status_type}")
        except Exception as e:
            print(f"Error processing webhook: {e}")
            
        return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
