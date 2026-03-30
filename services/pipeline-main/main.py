import os
import json
import httpx
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from google.cloud import firestore
import google.auth
from google.cloud import secretmanager
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

def search_serper(query):
    api_key = get_secret(SERPER_API_KEY_NAME)
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": query, "num": 20})
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    response = httpx.post(url, headers=headers, data=payload)
    if response.status_code == 200:
        return response.json().get("organic", [])
    return []

def safe_truncate(text: str) -> str:
    """Enforce strict 100KB text truncation to prevent Firestore 1MB document crashes."""
    return text[:100000]

def pre_filter_gemini(snippets, bio):
    prompt = f"Review these 80 search snippets. Based on the user's product bio: '{bio}', discard low-signal content. Return only the URLs of the top 30 potential leads.\n\nSnippets: {json.dumps(snippets)}"
    response = model.generate_content(prompt)
    
    # Very rudimentary extraction of URLs from Gemini response
    urls = []
    for line in response.text.split('\n'):
        if line.startswith('http'):
             urls.append(line.strip())
    return urls

def scrape_url(url):
    # Lightweight scrape
    try:
        resp = httpx.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
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
    prompt = f"Score this 1-10 based on campaign goals and product bio: '{bio}'. Extract core pain point and write a highly conversational, brief, varied, non-salesy 2-sentence WhatsApp/LinkedIn DM. Format JSON with keys: score, pain_point, dm.\n\nText: {text}"
    result = model.generate_content(prompt)
    try:
        return json.loads(result.text.replace('```json', '').replace('```', ''))
    except:
        return {"score": 0, "pain_point": "Unknown", "dm": "Failed to generate DM"}

@app.route("/dispatch", methods=["POST"])
def dispatch():
    data = request.json
    tenant_id = data.get("tenant_id")
    campaign_id = data.get("campaign_id")
    
    campaign_ref = db.collection("campaigns").document(campaign_id)
    campaign = campaign_ref.get().to_dict()
    bio = campaign.get("product_bio", "")
    keywords = campaign.get("keywords", [])
    
    all_results = []
    
    for kw in keywords:
        # Step 1: Sweep
        results = search_serper(kw)
        
        # URL Deduplication
        unique_results = []
        seen = set()
        for r in results:
            if r.get("link") not in seen:
                seen.add(r.get("link"))
                unique_results.append(r)
                
        # Step 2: Pre-Filter
        filtered_urls = pre_filter_gemini(unique_results, bio)
        
        # Step 3, 4, 5
        for url in filtered_urls[:30]:
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
                    lead_doc = {
                        "tenant_id": tenant_id,
                        "campaign_id": campaign_id,
                        "url": url,
                        "score": evaluation.get("score"),
                        "pain_point": evaluation.get("pain_point"),
                        "dm": evaluation.get("dm"),
                        "status": "new"
                    }
                    db.collection("tenants").document(tenant_id).collection("leads").add(lead_doc)
                    all_results.append(lead_doc)
                    
    return jsonify({"processed_leads": len(all_results)}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
