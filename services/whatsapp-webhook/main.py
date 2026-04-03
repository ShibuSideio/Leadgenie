import os
import hashlib
import hmac
import httpx
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
                            
                    # Interactive Lead Routing (V6 Parsing)
                    messages = value.get("messages", [])
                    metadata = value.get("metadata", {})
                    phone_number_id = metadata.get("display_phone_number") or metadata.get("phone_number_id")
                    
                    for msg in messages:
                        if msg.get("type") == "interactive":
                            interactive = msg.get("interactive", {})
                            if interactive.get("type") == "button_reply":
                                payload_str = interactive.get("button_reply", {}).get("id", "")
                                sender_wa_id = msg.get("from")
                                
                                if "_" in payload_str:
                                    action, lead_id = payload_str.split("_", 1)
                                    target_status = "approved" if action == "approve" else "ignored"
                                    
                                    # Atomic state sync to Firestore
                                    lead_ref = db.collection("leads").document(lead_id)
                                    if lead_ref.get().exists:
                                        lead_ref.update({"status": target_status})
                                        print(f"Autonomous State Sync: {lead_id} -> {target_status}")
                                        
                                        # Send confirmation back
                                        if phone_number_id and sender_wa_id:
                                            try:
                                                users_query = db.collection("users").where("wa_phone_id", "==", phone_number_id).limit(1).stream()
                                                wa_token = None
                                                for u in users_query:
                                                    wa_token = u.to_dict().get("wa_token")
                                                    
                                                if wa_token:
                                                    reply_payload = {
                                                        "messaging_product": "whatsapp",
                                                        "to": sender_wa_id,
                                                        "type": "text",
                                                        "text": {"body": f"Acknowledged. Target strictly {target_status}."}
                                                    }
                                                    wa_headers = {"Authorization": f"Bearer {wa_token}", "Content-Type": "application/json"}
                                                    httpx.post(f"https://graph.facebook.com/v18.0/{phone_number_id}/messages", json=reply_payload, headers=wa_headers, timeout=5)
                                            except Exception as reply_e:
                                                print(f"Confirmation send failed: {reply_e}")
        except Exception as e:
            print(f"Error processing webhook: {e}")
            
        return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
