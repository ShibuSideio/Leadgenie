import os
import json
import httpx
import hashlib
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from google.cloud import firestore
import google.auth
from google.cloud import secretmanager
from google.api_core.exceptions import AlreadyExists
import vertexai
from vertexai.generative_models import GenerativeModel

app = Flask(__name__)
db = firestore.Client()
project_id = os.environ.get("PROJECT_ID", "sideio-leads-v16")
sm_client = secretmanager.SecretManagerServiceClient()

SCRAPER_HEAVY_URL = os.environ.get("SCRAPER_HEAVY_URL", "https://scraper-heavy-abc.a.run.app/scrape")
SERPER_API_KEY_NAME = f"projects/{project_id}/secrets/serper_api_key/versions/latest"

vertexai.init(project=project_id, location="asia-south1")
model = GenerativeModel("gemini-2.5-flash")

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

def generate_smart_query(user_keywords, tenant_id):
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
            resp = model.generate_content(prompt)
            historical_phrases = [p.strip() for p in resp.text.split(',') if p.strip()]
    except Exception as e:
        print(f"Historical Composite Mining Exception: {e}")
        historical_phrases = []

    smart_queries = []
    blacklist = "-wiki -jobs -careers -investors -support -\"login\" -www.zoominfo.com -www.ibm.com -www.amazon.com"
    historical_str = ""
    if historical_phrases:
        phrases_escaped = [f'"{p}"' for p in historical_phrases[:3]]
        historical_str = " AND (" + " OR ".join(phrases_escaped) + ")"
        
    for kw in user_keywords:
        q = f'("{kw}"){historical_str} {blacklist}'
        smart_queries.append(q)
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

def pre_filter_gemini(snippets, bio):
    if not snippets:
        return []
    
    prompt = f"Review these {len(snippets)} search snippets. Based on the user's product bio: '{bio}', discard low-signal companies. YOUR OUTPUT MUST BE STRICTLY A LINE-BY-LINE LIST OF ONLY URLs matching high-value leads. Do NOT output markdown. Do NOT output bullet points. Every line must start precisely with 'http'.\n\nSnippets: {json.dumps(snippets)}"
    response = model.generate_content(prompt)
    
    # Aggressively parse only raw HTTP links from Gemini inference
    urls = []
    for line in response.text.split('\n'):
        clean_url = line.strip().replace('- ', '').replace('* ', '').replace('`', '').replace('"', '')
        if clean_url.startswith('http'):
             urls.append(clean_url)
    print(f"Gemini approved {len(urls)} URLs matching the B2B criteria.")
    return urls

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
                
        text = soup.get_text(separator=' ', strip=True)
        if len(text) < 500: # Potential JS Heavy page
            raise ValueError("Too little content, likely JS framework")
        return safe_truncate(text) # Strict truncation
    except Exception as e:
        print(f"Fallback to heavy scraper for {url} due to {str(e)}")
        # Call heavy scraper
        heavy_resp = httpx.post(SCRAPER_HEAVY_URL, json={"url": url}, timeout=45)
        if heavy_resp.status_code == 200:
            return safe_truncate(heavy_resp.json().get("text", ""))
        return ""

def final_score_and_dm(text, bio):
    prompt = f"Score this 1-10 based on campaign goals and product bio: '{bio}'. You must extract contact information. You must identify a specific human decision-maker (Name). If the extracted text is just a generic corporate homepage, an advertisement, or lacks a specific human contact, you MUST score it 0. Do not recommend generic info@ or sales@ emails without a named target. Extract core pain point and write a highly conversational, brief, varied, non-salesy 2-sentence WhatsApp/LinkedIn DM. Format JSON strictly with exact keys: score, pain_point, dm, email, phone, linkedin.\n\nText: {text}"
    result = model.generate_content(prompt)
    try:
        data = json.loads(result.text.replace('```json', '').replace('```', ''))
        return {
            "score": data.get("score", 0),
            "pain_point": data.get("pain_point", "Unknown"),
            "dm": data.get("dm", "Failed to generate DM"),
            "email": data.get("email", ""),
            "phone": data.get("phone", ""),
            "linkedin": data.get("linkedin", "")
        }
    except:
        return {"score": 0, "pain_point": "Unknown", "dm": "Failed to parse generation", "email": "", "phone": "", "linkedin": ""}

@app.route("/dispatch", methods=["POST"])
def dispatch():
    data = request.json
    tenant_id = data.get("tenant_id")
    campaign_id = data.get("campaign_id")
    
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
    smart_keywords = generate_smart_query(keywords, tenant_id)
    
    for kw in smart_keywords:
        # Step 1: Augmented Sweep
        raw_results = search_serper(kw, location=location if location else None, gl=gl if gl else None)
        
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
        filtered_urls = pre_filter_gemini(unique_results, bio)
        
        # Step 3, 4, 5
        for url in filtered_urls[:30]:
            # Deterministic Deduplication Gateway (Atomic locking)
            lead_id_str = f"{tenant_id}_{campaign_id}_{url}"
            lead_id = hashlib.sha256(lead_id_str.encode('utf-8')).hexdigest()
            doc_ref = db.collection("leads").document(lead_id)
            
            try:
                # Atomically ensure one task processes this URL cleanly preventing token burn
                doc_ref.create({
                    "tenant_id": tenant_id,
                    "campaign_id": campaign_id,
                    "url": url,
                    "status": "processing",
                    "createdAt": firestore.SERVER_TIMESTAMP
                })
            except AlreadyExists:
                # Silently catch duplicate invocations safely returning execution cycles
                continue

            # Cache check
            cache_ref = db.collection("scraped_cache").document(url.replace('/','_'))
            cache_doc = cache_ref.get()
            
            if cache_doc.exists:
                text = cache_doc.to_dict().get("text", "")
            else:
                text = scrape_url(url)
                if text:
                    # Save to cache explicitly enforcing truncation rule before write
                    cache_ref.set({"url": url, "text": safe_truncate(text)})
            
            if text:
                evaluation = final_score_and_dm(text, bio)
                if evaluation.get("score", 0) >= 7:
                    # Upgrade the atomic stub securely saving pipeline extraction logic
                    doc_ref.update({
                        "score": evaluation.get("score"),
                        "pain_point": evaluation.get("pain_point"),
                        "dm": evaluation.get("dm"),
                        "email": evaluation.get("email", ""),
                        "phone": evaluation.get("phone", ""),
                        "linkedin": evaluation.get("linkedin", ""),
                        "status": "new"
                    })
                    
                    # Store purely for JSON endpoint tracking response formatting locally
                    lead_doc = {
                        "tenant_id": tenant_id,
                        "campaign_id": campaign_id,
                        "url": url,
                        "score": evaluation.get("score"),
                        "pain_point": evaluation.get("pain_point"),
                        "dm": evaluation.get("dm"),
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
