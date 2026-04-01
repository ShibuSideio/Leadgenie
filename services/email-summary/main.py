import os
import datetime
import httpx
from flask import Flask, jsonify
from google.cloud import firestore
from google.cloud import secretmanager

app = Flask(__name__)
db = firestore.Client()
project_id = os.environ.get("PROJECT_ID", "sideio-leads-v16")
sm_client = secretmanager.SecretManagerServiceClient()

SENDGRID_API_KEY_SECRET = f"projects/{project_id}/secrets/sendgrid_api_key/versions/latest"
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "admin@yourdomain.com")

def get_secret(secret_name):
    try:
        response = sm_client.access_secret_version(request={"name": secret_name})
        return response.payload.data.decode("UTF-8")
    except:
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
    # Triggered centrally. Iterates through tenants and compiles daily digests.
    tenants = db.collection("tenants").stream()
    count = 0
    
    today = datetime.datetime.utcnow().date()
    yesterday_str = today.isoformat() # Roughly filtering

    for t in tenants:
        t_data = t.to_dict()
        email = t_data.get("admin_email")
        if email:
            # Query recent leads securely across all campaigns for this tenant
            leads_query = db.collection("leads").where("tenant_id", "==", t.id).limit(50).stream()
            
            top_leads = []
            total = 0
            for l in leads_query:
                data = l.to_dict()
                if data.get("status") not in ["new", "contacted"]:
                    continue
                top_leads.append(f"- URL: {data.get('url')} | Score: {data.get('score')} | Pain: {str(data.get('pain_point'))[:50]}...")
                total += 1
                if total >= 10: break
            
            if total > 0:
                send_summary_email(email, total, top_leads)
                count += 1
                
    return jsonify({"summaries_sent": count}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
