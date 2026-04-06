import os
import random
import json
import httpx
import hashlib
import datetime
import concurrent.futures
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from flask import Flask, request, jsonify
from google.cloud import firestore
import google.auth
from google.cloud import secretmanager
from google.api_core.exceptions import AlreadyExists, ResourceExhausted
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig, Schema, Type

app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "version": "12.99.1", "location": "us-central1"}), 200

db = firestore.Client()
project_id = os.environ.get("PROJECT_ID", "sideio-leads-v16")
PROJECT_ID = project_id
LOCATION = os.environ.get("LOCATION", "asia-south1")
QUEUE = os.environ.get("QUEUE", "lead-pipeline-queue")
sm_client = secretmanager.SecretManagerServiceClient()

SCRAPER_HEAVY_URL = os.environ.get("SCRAPER_HEAVY_URL", "https://scraper-heavy-abc.a.run.app/scrape")
SERPER_API_KEY_NAME = f"projects/{project_id}/secrets/serper_api_key/versions/latest"
FERNET_KEY = os.environ.get("ENCRYPTION_KEY", "uNqG8Jc-44SjK22N8B5-2GksnE5F_88_V5wQZ02j1A0=")
cipher_suite = Fernet(FERNET_KEY.encode())

# Global initialization explicitly routed to the central US cluster
vertexai.init(location="us-central1")

def call_gemini_2_5(prompt: str, expect_json: bool = True, response_schema=None, system_instruction=None):
    model = GenerativeModel("gemini-2.5-flash", system_instruction=system_instruction)
    if expect_json:
        config = GenerationConfig(response_mime_type="application/json", response_schema=response_schema)
    else:
        config = None
    
    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(5), retry=retry_if_exception_type(ResourceExhausted))
    def _invoke_model():
        return model.generate_content(prompt, generation_config=config)
        
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_invoke_model)
            response = future.result(timeout=45.0)
    except concurrent.futures.TimeoutError:
        print("Vertex AI execution timed out / quota hang.")
        raise TimeoutError("Vertex AI timeout")
    
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

def safe_truncate(text: str, max_bytes: int = 100000) -> str:
    """Enforce strict byte-level truncation to prevent Firestore 1MB document crashes."""
    encoded = text.encode('utf-8', errors='ignore')
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode('utf-8', errors='ignore')
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
        symptom_prompt = f"The user solves this business problem: '{bio}'. Generate 3 highly specific Google Search operators to find targets PUBLICLY EXPERIENCING this problem. \nRule 1: You MUST include at least one query targeting social/professional networks using 'site:linkedin.com', 'site:facebook.com', or 'site:reddit.com'. \nRule 2: You MUST append negative keywords to exclude retail/informational sites (e.g., '-shop -cart -amazon -wiki'). \nReturn ONLY a JSON list of 3 strings."
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
    enterprise_domains = ["ibm.com", "amazon.com", "microsoft.com", "g2.com", "capterra.com", "zoominfo.com"]
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
    
    prompt = f"CRITICAL INTENT CHECK: Read the user's bio: '{bio}'. Is the website EXPERIENCING the problem the user solves, or are they SELLING a solution to it? You MUST reject any URL that is an SEO blog, a competitor, or a direct-to-consumer (D2C) retail catalog. You MUST reject any URL that is a business directory, aggregator, yellow pages, or marketplace (e.g., JustDial, Alibaba, Yelp, IndiaMart, ExportersIndia). These are not end-buyers. Only approve the direct websites of individual businesses. Only approve targets that match the user's intended value chain.\n\nCOMPETITOR & MANUFACTURER BAN: You MUST reject manufacturers, wholesalers, and suppliers who already produce or sell products in the user's industry. If the user sells car care products, you MUST reject a company that manufactures car shampoo. Only approve END-USERS of the product/service.\n\nSOCIAL PLATFORM RULE: If the URL is from a social network or forum (e.g., linkedin.com, facebook.com, reddit.com, quora.com), DO NOT evaluate the host platform. You must evaluate the INTENT of the specific post or user snippet. If the snippet shows a human or local business asking for help or discussing the symptom, APPROVE the URL. Do not reject Reddit just because Reddit itself is not your target B2B buyer.\n\nCRITICAL: Evaluate the business location. If the target location is '{location_target}', and this website explicitly serves a different geographic region (e.g., a Dubai business for a Kochi search), you MUST reject the URL immediately. Return a failed state.\n\nYOUR OUTPUT MUST BE STRICTLY A LINE-BY-LINE LIST OF ONLY URLs matching high-value leads. Do NOT output markdown. Do NOT output bullet points. Every line must start precisely with 'http'.\n\nSnippets: {json.dumps(snippets)}"
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
        raise ValueError("DEFERRED")

def final_score_and_dm(text, bio, context_payload, tech_stack, historical_dms=None):
    prompt = f"""You are an Elite B2B Profiler. Score this lead 1-10 based on campaign goals and product bio: '{bio}'. 

You MUST extract contact information. You MUST identify a specific human decision-maker (Name). If the extracted text is just a generic corporate homepage, an advertisement, or lacks a specific human contact, you MUST score it 0. Do not recommend generic info@ or sales@ emails without a named target. 

For the "hiring_intent_found" field: Return ONLY the string 'Yes' or 'No'. Do not include any explanation, context, or reasoning. If unknown, return 'No'.

CONTEXTUAL DORKING DATA:
{context_payload}

DETECTED TECH STACK:
{', '.join(tech_stack) if tech_stack else 'None extracted'}
"""

    if historical_dms:
        prompt += f"\nHere are examples of past successful messages that converted: {historical_dms}. Match this tone and length strictly.\n"

    prompt += f"""
Using all of this context, specifically weigh the Hiring Intent and Tech Stack to write a hyper-personalized "Trojan Horse" Icebreaker. Format JSON strictly with exact keys.

Text DOM: {text}
"""
    sys_inst = "You are an Elite B2B Profiler. Your mandate is to extract factual enterprise data and draft concise, highly-converting outreach messages. Do not use fluff, robotic greetings, or marketing jargon. Be ruthless, analytical, and highly specific to the provided text."

    schema = Schema(
        type=Type.OBJECT,
        properties={
            "score": Schema(type=Type.INTEGER),
            "dm": Schema(type=Type.STRING, description="If the scraped text does not represent a valid B2B prospect or lacks sufficient information to draft a message, you MUST output the exact string 'N/A' for this field. Do not leave it blank or null."),
            "pain_point": Schema(type=Type.STRING, description="If the scraped text does not represent a valid B2B prospect or lacks sufficient information to draft a message, you MUST output the exact string 'N/A' for this field. Do not leave it blank or null."),
            "icebreaker_angle": Schema(type=Type.STRING, description="If the scraped text does not represent a valid B2B prospect or lacks sufficient information to draft a message, you MUST output the exact string 'N/A' for this field. Do not leave it blank or null."),
            "hiring_intent_found": Schema(
                type=Type.STRING,
                enum=["Yes", "No"]
            ),
            "tech_stack_found": Schema(
                type=Type.ARRAY,
                items=Schema(type=Type.STRING),
                description="Only include real, verified software technologies (e.g., 'wordpress', 'shopify', 'stripe'). Do NOT include internal system notes."
            ),
            "whatsapp_draft": Schema(type=Type.STRING),
            "email": Schema(type=Type.STRING),
            "phone": Schema(type=Type.STRING),
            "linkedin": Schema(type=Type.STRING),
            "decision_maker_name": Schema(type=Type.STRING, description="Specific human name found, else 'Unknown'"),
            "decision_maker_title": Schema(type=Type.STRING, description="Title of the decision maker, else 'Unknown'"),
            "company_size_tier": Schema(
                type=Type.STRING, 
                description="Must be strictly one of: 'Startup', 'Mid-Market', 'Enterprise', or 'Unknown'"
            ),
            "primary_objection_hypothesis": Schema(type=Type.STRING, description="A 1-sentence prediction of why they might reject our bio/pitch based on their site context.")
        },
        required=["score", "dm", "pain_point", "icebreaker_angle", "hiring_intent_found", "tech_stack_found", "decision_maker_name", "decision_maker_title", "company_size_tier", "primary_objection_hypothesis"]
    )
    
    try:
        data = call_gemini_2_5(prompt, expect_json=True, response_schema=schema, system_instruction=sys_inst)
        if not isinstance(data, dict):
            raise ValueError("Parsed JSON is not a dictionary.")
            
        return {
            "score": data.get("score", 0),
            "pain_point": data.get("pain_point", "Unknown"),
            "hiring_intent_found": data.get("hiring_intent_found", "None"),
            "tech_stack_found": data.get("tech_stack_found", []),
            "icebreaker_angle": data.get("icebreaker_angle", ""),
            "dm": data.get("dm", data.get("whatsapp_draft", "Failed to generate DM")),
            "email": data.get("email", ""),
            "phone": data.get("phone", ""),
            "linkedin": data.get("linkedin", ""),
            "decision_maker_name": data.get("decision_maker_name", "Unknown"),
            "decision_maker_title": data.get("decision_maker_title", "Unknown"),
            "company_size_tier": data.get("company_size_tier", "Unknown"),
            "primary_objection_hypothesis": data.get("primary_objection_hypothesis", "Unknown")
        }
    except Exception as e:
        raise ValueError("LLM Parsing Failure")

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
    target_urls = campaign.get("target_urls", [])
    user_urls = target_urls[:10] if isinstance(target_urls, list) else []
    
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
        approved_serper_urls = pre_filter_gemini(unique_results, bio, location)
        
        # Merge & Deduplicate
        seen_urls = set()
        combined_urls = []
        for u in user_urls + approved_serper_urls:
            if u not in seen_urls:
                seen_urls.add(u)
                combined_urls.append(u)
                
        # The Cap
        final_execution_urls = combined_urls[:20]
        
        # Step 3, 4, 5
        for url in final_execution_urls:
            target_domain = extract_root_domain(url)
            if not target_domain: continue
            
            SOCIAL_DOMAINS = ["linkedin.com", "facebook.com", "reddit.com", "instagram.com", "x.com", "twitter.com", "team-bhp.com", "quora.com", "youtube.com"]
            
            if any(target_domain.endswith(social) for social in SOCIAL_DOMAINS):
                parsed_url = urlparse(url)
                exact_path = f"{parsed_url.netloc}{parsed_url.path}".lower().replace('www.', '')
                lock_entity = hashlib.sha256(exact_path.encode('utf-8')).hexdigest()
                dedupe_target = exact_path
            else:
                lock_entity = target_domain
                dedupe_target = target_domain
            
            # --- GLOBAL EXCLUSIVITY LOCK ---
            lock_ref = db.collection("global_lead_locks").document(lock_entity)
            try:
                lock_doc = lock_ref.get()
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if lock_doc.exists:
                    locked_until = lock_doc.to_dict().get("locked_until")
                    if locked_until and locked_until > now_utc:
                        print(f"[EXCLUSIVITY] Silently dropping {url}. Entity {lock_entity} locked.")
                        continue
                
                lock_ref.set({"locked_until": now_utc + datetime.timedelta(days=14)})
            except Exception as e:
                print(f"[LOCK FAIL] {e}")
                pass
                
            # Deterministic Deduplication Gateway (Unified Account Resolution)
            lead_id_str = f"{tenant_id}_{dedupe_target}"
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

            try:
                # Cache check
                cache_ref = db.collection("scraped_cache").document(url.replace('/','_'))
                cache_doc = cache_ref.get()
            
                if cache_doc.exists:
                    c_data = cache_doc.to_dict()
                    text = c_data.get("text", "")
                    tech_stack = c_data.get("tech_stack", [])
                    emails = c_data.get("emails", [])
                    phones = c_data.get("phones", [])
                elif any(target_domain.endswith(social) for social in SOCIAL_DOMAINS):
                    snippet_text = ""
                    for ur in unique_results:
                        if ur.get("link") == url:
                            snippet_text = ur.get("snippet", "")
                            break
                    text = snippet_text if snippet_text else "Social profile snippet empty."
                    tech_stack, emails, phones = ["Social Platform Bypass"], [], []
                    print(f"[SOCIAL SHORT-CIRCUIT] Extracted snippet for {url}")
                else:
                    try:
                        text, tech_stack, emails, phones = scrape_url(url)
                    except ValueError as e:
                        if str(e) == "DEFERRED":
                            print(f"[DEFERRED] Queueing async task to scraper-heavy for {url}")
                            # Dispatch Cloud Task
                            from google.cloud import tasks_v2
                            tasks_client = tasks_v2.CloudTasksClient()
                            parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
                            task = {
                                "http_request": {
                                    "http_method": tasks_v2.HttpMethod.POST,
                                    "url": SCRAPER_HEAVY_URL,
                                    "headers": {"Content-Type": "application/json"},
                                    "body": json.dumps({
                                        "url": url, "lead_id": lead_id, "tenant_id": tenant_id, 
                                        "campaign_id": campaign_id, "bio": bio, "target_domain": target_domain,
                                        "preferences_weights": preferences_weights
                                    }).encode()
                                }
                            }
                            tasks_client.create_task(parent=parent, task=task)
                            continue
                        text, tech_stack, emails, phones = "", [], [], []
                        
                    if text:
                        # Save to cache explicitly enforcing truncation rule before write
                        cache_ref.set({"url": url, "text": safe_truncate(text), "tech_stack": tech_stack, "emails": emails, "phones": phones})
            
                if text:
                    bot_keywords = ["Cloudflare Ray ID", "Please verify you are human", "Enable JavaScript and cookies to continue", "Checking if the site connection is secure", "Access Denied", "403 Forbidden"]
                    if any(keyword.lower() in text.lower() for keyword in bot_keywords):
                        doc_ref.update({"status": "failed", "error": "Blocked by Cloudflare/WAF"})
                        continue
                        
                    shard_id = random.randint(0, 9)
                    db.collection("usage_metrics").document(tenant_id).collection("shards").document(str(shard_id)).set({"gemini_calls": firestore.Increment(1)}, merge=True)
                    db.collection("users").document(tenant_id).collection("wallet_shards").document(str(shard_id)).set({"consumed_credits": firestore.Increment(1)}, merge=True)
                
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
                    except TimeoutError:
                        db.collection("leads").document(lead_id).update({"status": "failed", "error": "Vertex AI timeout"})
                        continue
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
            except Exception as loop_e:
                print(f'Pipeline execution crashed: {loop_e}')
                db.collection('leads').document(lead_id).update({'status': 'failed', 'error': 'Pipeline execution crashed'})
                continue
                    
    return jsonify({"processed_leads": len(all_results)}), 200
def extract_dense_payload(text, bio):
    import re
    paragraphs = [p.strip() for p in text.split('\n') if len(p.strip()) > 30]
    bio_words = set(re.findall(r'\b\w{4,}\b', bio.lower()))
    about_us_terms = {"we are", "founded", "team", "mission", "services", "our goal", "about us"}
    
    scored_paragraphs = []
    for p in paragraphs:
        raw_p = p.lower()
        score = 0
        for term in about_us_terms:
            if term in raw_p:
                score += 3
        words = set(re.findall(r'\b\w{4,}\b', raw_p))
        score += len(words.intersection(bio_words))
        scored_paragraphs.append((score, p))
        
    scored_paragraphs.sort(key=lambda x: x[0], reverse=True)
    return "\n\n".join([p for s, p in scored_paragraphs[:10]])

@app.route("/finalize", methods=["POST"])
def finalize():
    # Receive decoupled webhook from scraper-heavy
    data = request.json
    text = data.get("text", "")
    emails = data.get("emails", [])
    phones = data.get("phones", [])
    lead_id = data.get("lead_id")
    tenant_id = data.get("tenant_id")
    campaign_id = data.get("campaign_id")
    bio = data.get("bio", "")
    url = data.get("url", "")
    target_domain = data.get("target_domain", "")
    preferences_weights = data.get("preferences_weights", {})
    tech_stack = ["Fallback Scraper Used"]
    
    if not lead_id or not tenant_id:
        return jsonify({"error": "Missing crucial context"}), 400
        
    doc_ref = db.collection("leads").document(lead_id)
    if not text:
        doc_ref.delete()
        return jsonify({"status": "dropped empty text"}), 200
        
    bot_keywords = ["Cloudflare Ray ID", "Please verify you are human", "Enable JavaScript and cookies to continue", "Checking if the site connection is secure", "Access Denied", "403 Forbidden"]
    if any(keyword.lower() in text.lower() for keyword in bot_keywords):
        doc_ref.update({"status": "failed", "error": "Blocked by Cloudflare/WAF"})
        return jsonify({"status": "blocked by waf"}), 200
        
    try:
        # Python Fast-Fail Gate
        tenant_doc = db.collection("users").document(tenant_id).get().to_dict() or {}
        global_b2b_blocklist = ['add to cart', 'shopping bag', 'checkout', 'shipping policy', 'return policy', 'in stock']
        dynamic_blocklist = tenant_doc.get("dynamic_blocklist", [])
        b2b_blacklist = global_b2b_blocklist + [str(x).lower() for x in dynamic_blocklist]
        
        fail_score = sum(text.lower().count(term) for term in b2b_blacklist)
        if fail_score > 3:
            doc_ref.update({"status": "failed", "error": "Dropped by Python Heuristics (Cost Saved)"})
            return jsonify({"status": "heuristic_drop"}), 200

        # Token Reduction via Density Extraction
        dense_text = extract_dense_payload(text, bio)

        # Re-enter processing flow
        import datetime
        expire_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
        cache_ref = db.collection("scraped_cache").document(url.replace('/','_'))
        
        cache_ref.set({
            "url": url, 
            "text": safe_truncate(dense_text), 
            "tech_stack": tech_stack, 
            "emails": emails, 
            "phones": phones,
            "expireAt": expire_at
        }, merge=True)
        
        shard_id = random.randint(0, 9)
        db.collection("usage_metrics").document(tenant_id).collection("shards").document(str(shard_id)).set({"gemini_calls": firestore.Increment(1)}, merge=True)
        db.collection("users").document(tenant_id).collection("wallet_shards").document(str(shard_id)).set({"consumed_credits": firestore.Increment(1)}, merge=True)
        
        context_payload, native_hiring_intent = deep_context_serper_dork(target_domain, tenant_id)
        
        try:
            docs = db.collection("leads").where("tenant_id", "==", tenant_id).where("status", "==", "converted").order_by("updatedAt", direction=firestore.Query.DESCENDING).limit(3).stream()
            historical_dms = [doc.to_dict().get("dm") for doc in docs if doc.to_dict().get("dm")]
            
            evaluation = final_score_and_dm(dense_text, bio, context_payload, tech_stack, historical_dms)
        except TimeoutError:
            doc_ref.update({"status": "failed", "error": "Vertex AI timeout"})
            return jsonify({"status": "timeout"}), 200
        except Exception as e:
            doc_ref.update({"status": "failed", "error": str(e)})
            return jsonify({"status": "failed"}), 200
        
        if evaluation.get("score", 0) >= 7:
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
                "decision_maker_name": evaluation.get("decision_maker_name", "Unknown"),
                "decision_maker_title": evaluation.get("decision_maker_title", "Unknown"),
                "company_size_tier": evaluation.get("company_size_tier", "Unknown"),
                "primary_objection_hypothesis": evaluation.get("primary_objection_hypothesis", "Unknown"),
                "status": "new"
            })
            
            # Simplified WhatsApp Meta Call (V13)
            if evaluation.get("score", 0) >= 8:
                tenant_doc = db.collection("users").document(tenant_id).get().to_dict() or {}
                wa_token_encrypted = tenant_doc.get("wa_token")
                wa_phone_id = tenant_doc.get("wa_phone_id")
                admin_phone = tenant_doc.get("admin_phone")
                
                wa_token = None
                if wa_token_encrypted:
                    try:
                        from google.cloud import kms
                        import base64
                        kms_client = kms.KeyManagementServiceClient()
                        key_name = get_secret("kms_wa_key_path").strip()
                        ciphertext = base64.b64decode(wa_token_encrypted)
                        response = kms_client.decrypt(
                            request={'name': key_name, 'ciphertext': ciphertext}
                        )
                        wa_token = response.plaintext.decode('utf-8')
                    except Exception as e:
                        print(f"KMS Decryption failed: {e}. Attempting Fernet fallback.")
                        try:
                            wa_token = cipher_suite.decrypt(wa_token_encrypted.encode()).decode()
                        except:
                            wa_token = wa_token_encrypted
                        
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
                                    {"type": "reply", "reply": {"id": f"approve_{lead_id}", "title": "✅ Approve"}},
                                    {"type": "reply", "reply": {"id": f"ignore_{lead_id}", "title": "🚫 Ignore"}}
                                ]
                            }
                        }
                    }
                    try:
                        httpx.post(f"https://graph.facebook.com/v18.0/{wa_phone_id}/messages", json=wa_payload, headers={"Authorization": f"Bearer {wa_token}", "Content-Type": "application/json"}, timeout=5)
                    except:
                        pass
        else:
            doc_ref.delete()
            
    except Exception as hook_err:
        doc_ref.update({"status": "failed", "error": "Finalize webhook crash"})
        
    return jsonify({"status": "finalized"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
