import os
import smtplib
from email.message import EmailMessage
import datetime
from flask import Flask, jsonify
from google.cloud import firestore
from google.cloud import secretmanager

app = Flask(__name__)
db = firestore.Client()
project_id = os.environ.get("PROJECT_ID", "sideio-leads-v16")
sm_client = secretmanager.SecretManagerServiceClient()

GMAIL_PASS_SECRET = f"projects/{project_id}/secrets/gmail_app_password/versions/latest"
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "admin@yourdomain.com")

def get_secret(secret_name):
    try:
        response = sm_client.access_secret_version(request={"name": secret_name})
        return response.payload.data.decode("UTF-8")
    except:
        return ""

def send_summary_email(recipient, lead_count, top_leads):
    password = get_secret(GMAIL_PASS_SECRET)
    if not password:
        print("Missing Gmail App Password")
        return
        
    msg = EmailMessage()
    msg.set_content(f"Daily Lead Sniper Summary\n\nGenerated {lead_count} new contact-ready leads today.\n\nTop Leads:\n" + "\n".join(top_leads))
    msg['Subject'] = 'Your Daily Lead Sniper Summary'
    msg['From'] = SENDER_EMAIL
    msg['To'] = recipient

    try:
        # Utilize Gmail SMTP Relay using App Password
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(SENDER_EMAIL, password)
        server.send_message(msg)
        server.quit()
        print(f"Sent summary email to {recipient}")
    except Exception as e:
        print(f"SMTP Error: {e}")

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
            # Query recent leads across all campaigns for this tenant
            leads_query = db.collection("tenants").document(t.id).collection("leads").where("status", "in", ["new", "contacted"]).limit(10).stream()
            
            top_leads = []
            total = 0
            for l in leads_query:
                data = l.to_dict()
                top_leads.append(f"- URL: {data.get('url')} | Score: {data.get('score')} | Pain: {data.get('pain_point')[:50]}...")
                total += 1
            
            if total > 0:
                send_summary_email(email, total, top_leads)
                count += 1
                
    return jsonify({"summaries_sent": count}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
