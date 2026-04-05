import os
import json
import httpx
import hashlib
import datetime
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from flask import Flask, request, jsonify
from google.cloud import firestore
import google.auth
from google.cloud import secretmanager
from google.api_core.exceptions import AlreadyExists
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

app = Flask(__name__)
db = firestore.Client()
project_id = os.environ.get("PROJECT_ID", "sideio-leads-v16")
sm_client = secretmanager.SecretManagerServiceClient()

SCRAPER_HEAVY_URL = os.environ.get("SCRAPER_HEAVY_URL", "https://scraper-heavy-abc.a.run.app/scrape")
SERPER_API_KEY_NAME = f"projects/{project_id}/secrets/serper_api_key/versions/latest"
FERNET_KEY = os.environ.get("ENCRYPTION_KEY", "uNqG8Jc-44SjK22N8B5-2GksnE5F_88_V5wQZ02j1A0=")
cipher_suite = Fernet(FERNET_KEY.encode())

# Global initialization explicitly routed to the central US cluster
vertexai.init(location="us-central1")

def call_gemini_2_5(prompt: str, expect_json: bool = True):
    model = GenerativeModel("gemini-2.5-flash")
    config = GenerationConfig(response_mime_type="application/json") if expect_json else None
    
    response = model.generate_content(prompt, generation_config=config)
    
    if expect_json:
        # Native JSON mode eliminates the need for regex/markdown stripping
        return json.loads(response.text)
    return response.text

def get_secret(secret_name):
    response = sm_client.access_secret_version(request={"name": secret_name})
    return response.payload.data.decode("UTF-8")

def search_serper(query, location=None, gl=None):
    api_key = get_secret(SERPER_API_KEY_NAME).strip()
    url = "https://google.serper.dev/search"
    payload_dict = {"q": query, "num": 20}
    if location:
        payload_dict["location"] = location
    if gl:
        payload_dict["gl"] = gl
    payload = json.dumps(payload_dict)
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    
    response = httpx.post(url, headers=headers, data=payload, timeout=30)
    if response.status_code == 200:
        return response.json().get("organic", [])
    
    print(f"SERPER API AUTH OR RATE LIMIT CRASH HTTP {response.status_code}: {response.text}")
    return []

def safe_truncate(text: str) -> str:
    """Enforce strict 100KB text truncation to prevent Firestore 1MB document crashes."""
    return text[:100000]
def generate_smart_query(user_keywords, tenant_id, bio):
    historical_phrases = []
    try:
        # Require composite index, graceful fallback to Global
        query = db.collection("leads").where("tenant_id", "==", tenant_id).where("status", "in", ["contacted", "converted"]).limit(20)
        docs = list(query.stream())
        if not docs:
            query = db.collection("leads").where("status", "in", ["contacted", "converted"]).limit(20)
            docs = list(query.stream())
            
        pain_points = [d.to_dict().get("pain_point", "") for d in docs if d.to_dict().get("pain_point")]
        if pain_points:
            prompt = f"Analyze these successful lead extractions. Extract exactly 3 short conceptual B2B phrases identifying high-value trends. Your output must strictly be comma separated only. Do not use quotes or bullets.\n\nData: {json.dumps(pain_points)}"
            resp_text = call_gemini_2_5(prompt, expect_json=False)
            historical_phrases = [p.strip() for p in resp_text.split(',') if p.strip()]
    except Exception as e:
        print(f"Historical Composite Mining Exception: {e}")
        historical_phrases = []

    # New: Symptom Discovery Funnel
    symptom_dorks = []
    if bio:
        symptom_prompt = f"The user solves this business problem: '{bio}'. Generate 3 highly specific Google Search operators (using OR/AND/site:) to find companies publicly experiencing symptoms of this problem. Return ONLY a JSON list of 3 strings. Example: [\"operator 1\", \"operator 2\", \"operator 3\"]"
        try:
            symptom_dorks = call_gemini_2_5(symptom_prompt, expect_json=True)
        except Exception as e:
            print(f"Symptom Extraction Exception: {e}")
            pass

    smart_queries = []
    blacklist = "-wiki -jobs -careers -investors -support -\"login\" -www.zoominfo.com -www.ibm.com -www.amazon.com"
    historical_str = ""
    if historical_phrases:
        phrases_escaped = [f'"{p}"' for p in historical_phrases[:3]]
        historical_str = " AND (" + " OR ".join(phrases_escaped) + ")"
        
    for kw in user_keywords:
        q = f'("{kw}"){historical_str} {blacklist}'
        smart_queries.append(q)
        
    for sd in symptom_dorks:
        smart_queries.append(f'{sd} {blacklist}')
        
    return smart_queries

def filter_serper_noise(serper_results):
    clean_results = []
    enterprise_domains = ["ibm.com", "amazon.com", "microsoft.com", "g2.com", "capterra.com", "zoominfo.com", "linkedin.com"]
    noise_paths = ["/legal", "/pricing", "/docs", "/author/", "/login"]
    noise_snippets = ["sign in", "access denied", "forgot password", "please enable cookies"]
    
    for r in serper_results:
        link = r.get("link", "").lower()
        snippet = r.get("snippet", "").lower()
        if any(d in link for d in enterprise_domains): continue
        if any(p in link for p in noise_paths): continue
        if any(s in snippet for s in noise_snippets): continue
        clean_results.append(r)
        
    return clean_results

def pre_filter_gemini(snippets, bio, location_target):
    if not snippets:
        return []
    
    prompt = f"Review these {len(snippets)} search snippets. Based on the user's product bio: '{bio}', discard low-signal companies. \n\nCRITICAL: Evaluate the business location. If the target location is '{location_target}', and this website explicitly serves a different geographic region (e.g., a Dubai business for a Kochi search), you MUST reject the URL immediately. Return a failed state.\n\nYOUR OUTPUT MUST BE STRICTLY A LINE-BY-LINE LIST OF ONLY URLs matching high-value leads. Do NOT output markdown. Do NOT output bullet points. Every line must start precisely with 'http'.\n\nSnippets: {json.dumps(snippets)}"
    response_text = call_gemini_2_5(prompt, expect_json=False)
    
    urls = []
    for line in response_text.split('\n'):
        clean_url = line.strip().replace('- ', '').replace('* ', '').replace('`', '').replace('"', '')
        if clean_url.startswith('http'):
             urls.append(clean_url)
    print(f"Gemini approved {len(urls)} URLs matching the B2B criteria.")
    return urls

def extract_root_domain(url):
    try:
        netloc = urlparse(url).netloc.lower()
        if not netloc:
             netloc = urlparse('http://' + url).netloc.lower()
        netloc = netloc.replace('www.', '')
        return netloc
    except:
        return ""

def deep_context_serper_dork(domain, tenant_id):
    if not domain: return "", False
    
    api_key = get_secret(SERPER_API_KEY_NAME).strip()
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    
    context_data = []
    
    def fetch_serper(url, payload):
        try:
             db.collection("usage_metrics").document(tenant_id).set({"serper_searches": firestore.Increment(1)}, merge=True)
             resp = httpx.post(url, headers=headers, json=payload, timeout=15)
             if resp.status_code == 200: return resp.json()
        except:
             pass
        return {}

    # Vector A: GMB / Local
    gmb_data = fetch_serper("https://google.serper.dev/places", {"q": domain, "num": 3})
    for place in gmb_data.get("places", []):
         context_data.append(f"[GMB] Rating: {place.get('rating', 'N/A')}, Reviews: {place.get('ratingCount', 'N/A')}, Address: {place.get('address', 'N/A')}")

    # Vector B: Social
    social_query = f"site:linkedin.com/company OR site:facebook.com \"{domain}\""
    social_data = fetch_serper("https://google.serper.dev/search", {"q": social_query, "num": 3})
    for org in social_data.get("organic", []):
         context_data.append(f"[SOCIAL] {org.get('snippet', '')}")

    # Vector C: Hiring Intent
    hiring_query = f"site:naukri.com/job-listings OR site:instahyre.com/job OR site:linkedin.com/jobs OR site:indeed.com/cmp \"{domain}\""
    hiring_data = fetch_serper("https://google.serper.dev/search", {"q": hiring_query, "num": 3})
    
    hiring_signatures = ["we are hiring", "job description", "apply today", "openings", "careers", "looking for", "lakh", "lpa", "fresher"]
    native_hiring_intent_found = False
    
    for job in hiring_data.get("organic", []):
         snippet_lower = job.get('snippet', '').lower()
         context_data.append(f"[HIRING] {snippet_lower}")
         if any(sig in snippet_lower for sig in hiring_signatures):
             native_hiring_intent_found = True
         
    return " | ".join(context_data)[:3000], native_hiring_intent_found


def scrape_url(url):
    # Lightweight scrape
    try:
        resp = httpx.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Smart WAF Heuristics Check (Cloudflare / Incapsula)
        title_str = soup.title.string.lower() if soup.title and soup.title.string else ""
        body_str = soup.get_text(separator=' ', strip=True)[:2000].lower()
        search_blob = f"{title_str} {body_str}"
        
        waf_fingerprints = ["just a moment...", "attention required!", "access denied", "cloudflare"]
        for fingerprint in waf_fingerprints:
            if fingerprint in search_blob:
                raise ValueError(f"WAF block explicitly detected ({fingerprint})")
                
        # Tech Stack X-Ray (Zero Cost)
        html_blob = str(soup).lower()
        tech_signatures = {
             "wordpress": "wp-content",
             "shopify": "cdn.shopify.com",
             "stripe": "js.stripe.com",
             "react": "react-root",
             "hubspot": "js.hs-scripts.com",
             "salesforce": "force.com",
             "google analytics": "google-analytics.com",
             "segment": "cdn.segment.com",
             "intercom": "widget.intercom.io"
        }
        found_tech = [name for name, sig in tech_signatures.items() if sig in html_blob]
                
        text = soup.get_text(separator=' ', strip=True)
        if len(text) < 500: # Potential JS Heavy page
            raise ValueError("Too little content, likely JS framework")
            
        extracted_emails = list({a['href'].replace('mailto:', '').split('?')[0].strip() for a in soup.find_all('a', href=True) if a['href'].startswith('mailto:')})
        extracted_phones = list({a['href'].replace('tel:', '').strip() for a in soup.find_all('a', href=True) if a['href'].startswith('tel:')})

        return safe_truncate(text), found_tech, extracted_emails, extracted_phones # Strict truncation
    except Exception as e:
        print(f"Fallback to heavy scraper for {url} due to {str(e)}")
        # Call heavy scraper
        heavy_resp = httpx.post(SCRAPER_HEAVY_URL, json={"url": url}, timeout=45)
        if heavy_resp.status_code == 200:
            data = heavy_resp.json()
            return safe_truncate(data.get("text", "")), ["Fallback Scraper Used"], data.get("emails", []), data.get("phones", [])
        return "", [], [], []

def final_score_and_dm(text, bio, context_payload, tech_stack):
    prompt = f"""You are an Elite B2B Profiler. Score this lead 1-10 based on campaign goals and product bio: '{bio}'. 

You MUST extract contact information. You MUST identify a specific human decision-maker (Name). If the extracted text is just a generic corporate homepage, an advertisement, or lacks a specific human contact, you MUST score it 0. Do not recommend generic info@ or sales@ emails without a named target. 

For the "hiring_intent_found" field: Return ONLY the string 'Yes' or 'No'. Do not include any explanation, context, or reasoning. If unknown, return 'No'.

CONTEXTUAL DORKING DATA:
{context_payload}

DETECTED TECH STACK:
{', '.join(tech_stack) if tech_stack else 'None extracted'}

Using all of this context, specifically weigh the Hiring Intent and Tech Stack to write a hyper-personalized "Trojan Horse" Icebreaker. Format JSON strictly with exact keys.

Output schema:
{{
  "score": <int>,
  "pain_point": "<str>",
  "hiring_intent_found": "<str>",
  "tech_stack_found": [<list>],
  "icebreaker_angle": "<str>",
  "whatsapp_draft": "<str>",
  "email": "<str>",
  "phone": "<str>",
  "linkedin": "<str>"
}}

Text DOM: {text}
"""
    data = call_gemini_2_5(prompt, expect_json=True)
    return {
        "score": data.get("score", 0),
        "pain_point": data.get("pain_point", "Unknown"),
        "hiring_intent_found": data.get("hiring_intent_found", "None"),
        "tech_stack_found": data.get("tech_stack_found", []),
        "icebreaker_angle": data.get("icebreaker_angle", ""),
        "dm": data.get("whatsapp_draft", "Failed to generate DM"),
        "email": data.get("email", ""),
        "phone": data.get("phone", ""),
        "linkedin": data.get("linkedin", "")
    }

@app.route("/dispatch", methods=["POST"])
def dispatch():
    lead_data = request.json
    tenant_id = lead_data.get("tenant_id")
    
    target_campaign_id = lead_data.get("campaign_id") or (lead_data.get("matched_campaigns")[0] if lead_data.get("matched_campaigns") else None)
    if not target_campaign_id:
        print("CRITICAL: Dropping Eventarc trigger, no identifiable campaign context.")
        return jsonify({"error": "Missing campaign_id context"}), 400
        
    campaign_id = target_campaign_id
    
    campaign_ref = db.collection("campaigns").document(campaign_id)
    campaign = campaign_ref.get().to_dict()
    bio = campaign.get("bio", "")
    
    location = campaign.get("location", "").strip()
    gl = campaign.get("gl", "").strip()
    
    raw_keywords = campaign.get("keywords", "")
    if isinstance(raw_keywords, str):
        keywords = [k.strip() for k in raw_keywords.split(',') if k.strip()]
    else:
        keywords = raw_keywords
    
    if not keywords:
        print(f"CRITICAL ERROR: Campaign {campaign_id} has empty keywords matrix. Pipeline aborted.")
        return jsonify({"error": "Empty keywords matrix"}), 400
        
    all_results = []
    
    # Smart BD Query Injector Native Integration
    smart_keywords = generate_smart_query(keywords, tenant_id, bio)
    
    # Telemetry Billing Check
    db.collection("usage_metrics").document(tenant_id).set({
        "serper_searches": firestore.Increment(len(smart_keywords))
    }, merge=True)
    
    user_doc = db.collection("users").document(tenant_id).get()
    preferences_weights = user_doc.to_dict().get("preferences_weights", {}) if user_doc.exists else {}
    
    for kw in smart_keywords:
        # Campaign geo-target query appending
        search_query = f"{kw} AND {location}" if location and location != "all" else kw
        
        # Step 1: Augmented Sweep
        raw_results = search_serper(search_query, location=location if location else None, gl=gl if gl else None)
        
        # Step 2: Ruthless Post-Flight Filter
        filtered_results = filter_serper_noise(raw_results)
        
        # URL Deduplication
        unique_results = []
        seen = set()
        for r in filtered_results:
            if r.get("link") not in seen:
                seen.add(r.get("link"))
                unique_results.append(r)
                
        # Step 3: LLM Pre-Filter
        filtered_urls = pre_filter_gemini(unique_results, bio, location)
        
        # Step 3, 4, 5
        for url in filtered_urls[:30]:
            target_domain = extract_root_domain(url)
            if not target_domain: continue
            
            # --- GLOBAL EXCLUSIVITY LOCK ---
            lock_ref = db.collection("global_lead_locks").document(target_domain)
            try:
                lock_doc = lock_ref.get()
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if lock_doc.exists:
                    locked_until = lock_doc.to_dict().get("locked_until")
                    if locked_until and locked_until > now_utc:
                        print(f"[EXCLUSIVITY] Silently dropping {url}. Account {target_domain} locked.")
                        continue
                
                lock_ref.set({"locked_until": now_utc + datetime.timedelta(days=14)})
            except Exception as e:
                print(f"[LOCK FAIL] {e}")
                pass
                
            # Deterministic Deduplication Gateway (Unified Account Resolution)
            lead_id_str = f"{tenant_id}_{target_domain}"
            lead_id = hashlib.sha256(lead_id_str.encode('utf-8')).hexdigest()
            doc_ref = db.collection("leads").document(lead_id)
            
            try:
                doc_ref.create({
                    "tenant_id": tenant_id,
                    "matched_campaigns": [campaign_id],
                    "url": url,
                    "status": "processing",
                    "createdAt": firestore.SERVER_TIMESTAMP
                })
            except AlreadyExists:
                print(f"[UAR] Resolved cross-campaign duplicate for {target_domain}. Updating array natively.")
                doc_ref.update({"matched_campaigns": firestore.ArrayUnion([campaign_id])})
                continue

            # Cache check
            cache_ref = db.collection("scraped_cache").document(url.replace('/','_'))
            cache_doc = cache_ref.get()
            
            if cache_doc.exists:
                c_data = cache_doc.to_dict()
                text = c_data.get("text", "")
                tech_stack = c_data.get("tech_stack", [])
                emails = c_data.get("emails", [])
                phones = c_data.get("phones", [])
            else:
                text, tech_stack, emails, phones = scrape_url(url)
                if text:
                    # Save to cache explicitly enforcing truncation rule before write
                    cache_ref.set({"url": url, "text": safe_truncate(text), "tech_stack": tech_stack, "emails": emails, "phones": phones})
            
            if text:
                db.collection("usage_metrics").document(tenant_id).set({"gemini_calls": firestore.Increment(1)}, merge=True)
                db.collection("users").document(tenant_id).set({"wallet": {"consumed_credits": firestore.Increment(1)}}, merge=True)
                
                # --- MULTI-VECTOR SERPER DORKING ---
                context_payload, native_hiring_intent = deep_context_serper_dork(target_domain, tenant_id)
                
                # RLHF Python Interceptor Check
                fit_score = 0
                if native_hiring_intent:
                    fit_score += preferences_weights.get("hiring_intent", 0)
                
                for tech in tech_stack:
                    fit_score += preferences_weights.get(f"tech_{tech}", 0)
                    
                if fit_score <= -3:
                    print(f"[RLHF] Target {target_domain} logically dropped (Fit Score: {fit_score}). Saves 1 Vertex Token Sequence.")
                    doc_ref.delete()
                    continue
                
                try:
                    evaluation = final_score_and_dm(text, bio, context_payload, tech_stack)
                except Exception as e:
                    db.collection("leads").document(lead_id).update({"status": "failed", "error": str(e)})
                    continue
                    
                if evaluation.get("score", 0) >= 7:
                    # Update the atomic stub securely saving pipeline extraction logic
                    doc_ref.update({
                        "score": evaluation.get("score"),
                        "pain_point": evaluation.get("pain_point"),
                        "dm": evaluation.get("dm"),
                        "hiring_intent_found": evaluation.get("hiring_intent_found", ""),
                        "tech_stack_found": evaluation.get("tech_stack_found", []),
                        "icebreaker_angle": evaluation.get("icebreaker_angle", ""),
                        "email": emails[0] if emails else evaluation.get("email", ""),
                        "phone": phones[0] if phones else evaluation.get("phone", ""),
                        "linkedin": evaluation.get("linkedin", ""),
                        "status": "new"
                    })
                    
                    # Meta WhatsApp Business API Trigger (V6)
                    if evaluation.get("score", 0) >= 8:
                        tenant_doc = db.collection("users").document(tenant_id).get().to_dict() or {}
                        wa_token_encrypted = tenant_doc.get("wa_token")
                        wa_phone_id = tenant_doc.get("wa_phone_id")
                        admin_phone = tenant_doc.get("admin_phone")
                        
                        wa_token = None
                        if wa_token_encrypted:
                            try:
                                wa_token = cipher_suite.decrypt(wa_token_encrypted.encode()).decode()
                            except:
                                wa_token = wa_token_encrypted # Fallback if not encrypted legacy
                                
                        if wa_token and wa_phone_id and admin_phone:
                            wa_payload = {
                                "messaging_product": "whatsapp",
                                "to": admin_phone,
                                "type": "interactive",
                                "interactive": {
                                    "type": "button",
                                    "body": {
                                        "text": f"🔥 Hot Lead Found!\nCompany: {url}\nScore: {evaluation.get('score')}/10\nWhy: {evaluation.get('pain_point')}\nTech Stack: {', '.join(evaluation.get('tech_stack_found', []))}\nHiring: {evaluation.get('hiring_intent_found', '')}\n\nDrafted DM: {evaluation.get('dm')}"
                                    },
                                    "action": {
                                        "buttons": [
                                            {"type": "reply", "reply": {"id": f"approve_{lead_id}", "title": "✅ Approve & Send"}},
                                            {"type": "reply", "reply": {"id": f"ignore_{lead_id}", "title": "🚫 Ignore"}}
                                        ]
                                    }
                                }
                            }
                            try:
                                wa_headers = {"Authorization": f"Bearer {wa_token}", "Content-Type": "application/json"}
                                httpx.post(f"https://graph.facebook.com/v18.0/{wa_phone_id}/messages", json=wa_payload, headers=wa_headers, timeout=5)
                            except Exception as wa_e:
                                print(f"WhatsApp Meta POST failed: {wa_e}")
                    
                    # Store purely for JSON endpoint tracking response formatting locally
                    lead_doc = {
                        "tenant_id": tenant_id,
                        "campaign_id": campaign_id,
                        "url": url,
                        "score": evaluation.get("score"),
                        "pain_point": evaluation.get("pain_point"),
                        "dm": evaluation.get("dm"),
                        "hiring_intent_found": evaluation.get("hiring_intent_found", ""),
                        "tech_stack_found": evaluation.get("tech_stack_found", []),
                        "icebreaker_angle": evaluation.get("icebreaker_angle", ""),
                        "email": evaluation.get("email", ""),
                        "phone": evaluation.get("phone", ""),
                        "linkedin": evaluation.get("linkedin", ""),
                        "status": "new"
                    }
                    all_results.append(lead_doc)
                else:
                    # Delete the atomic document so we don't accidentally ingest phantom rows
                    doc_ref.delete()
                    
    return jsonify({"processed_leads": len(all_results)}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
