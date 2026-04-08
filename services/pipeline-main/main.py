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
from vertexai.generative_models import GenerativeModel, GenerationConfig
from pydantic import ValidationError
from models import LeadPayload

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


def validate_and_update_lead(payload_dict: dict, doc_ref) -> bool:
    """
    Universal Data Contract gatekeeper.

    Validates payload_dict against LeadPayload (Pydantic v2).
    On success  → writes validated, clean dict to Firestore. Returns True.
    On failure  → Dead Letter pattern: writes raw payload with
                  status='schema_violation' for debugging. Returns False.

    Both dispatch() and finalize() must pass their final dicts through here
    instead of calling doc_ref.update() directly.
    """
    try:
        validated = LeadPayload(**payload_dict)
        doc_ref.update(validated.to_firestore_dict())
        print(f"[CONTRACT] ✓ Validated lead {payload_dict.get('id', '?')} "
              f"(engine={validated.origin_engine}, score={validated.score})")
        return True
    except ValidationError as ve:
        print(f"[CONTRACT] ✗ Schema violation for {payload_dict.get('id', '?')}: {ve}")
        # Dead Letter: preserve raw payload but quarantine it from the main feed
        dead_letter = dict(payload_dict)
        dead_letter["status"]            = "schema_violation"
        dead_letter["schema_error"]      = str(ve)
        dead_letter["schema_error_time"] = firestore.SERVER_TIMESTAMP
        try:
            doc_ref.update(dead_letter)
        except Exception as dl_e:
            print(f"[CONTRACT] Dead-letter write also failed: {dl_e}")
        return False

# ---------------------------------------------------------------------------
# V14: SYNAPTIC ROUTER — Vector-to-Platform Dork Map
# Maps a sourcing vector string to platform-specific Google Search operators.
# Injected into generate_smart_query() to dynamically tailor search topology.
# ---------------------------------------------------------------------------
VECTOR_PLATFORM_MAP = {
    "Social/Forum Listening": [
        "site:reddit.com",
        "site:quora.com",
        "site:facebook.com/groups"
    ],
    "Review Hijacking": [
        "site:tripadvisor.com",
        "site:trustpilot.com"
    ],
    "Maps/GMB Targeting": [
        "site:google.com/maps",
        '"near me"'
    ],
    "Classic B2B": [
        "site:linkedin.com/company"
    ]
}

def generate_smart_query(user_keywords, tenant_id, bio, sourcing_vector=None):
    """
    V14.1: Intent Expansion Engine.
    Translates raw keywords into platform-native conversational queries
    via Gemini before constructing Google Dork strings.
    Literal keyword passthrough is deprecated.
    """
    historical_phrases = []
    try:
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

    # --- Symptom Discovery Funnel (bio-driven dorks) ---
    symptom_dorks = []
    if bio:
        symptom_prompt = f"The user solves this business problem: '{bio}'. Generate 3 highly specific Google Search operators to find targets PUBLICLY EXPERIENCING this problem. \nRule 1: You MUST include at least one query targeting social/professional networks using 'site:linkedin.com', 'site:facebook.com', or 'site:reddit.com'. \nRule 2: You MUST append negative keywords to exclude retail/informational sites (e.g., '-shop -cart -amazon -wiki'). \nReturn ONLY a JSON list of 3 strings."
        try:
            symptom_dorks = call_gemini_2_5(symptom_prompt, expect_json=True)
        except Exception as e:
            print(f"Symptom Extraction Exception: {e}")

    blacklist = "-wiki -jobs -careers -investors -support -\"login\" -www.zoominfo.com -www.ibm.com -www.amazon.com"
    historical_str = ""
    if historical_phrases:
        phrases_escaped = [f'"{p}"' for p in historical_phrases[:3]]
        historical_str = " AND (" + " OR ".join(phrases_escaped) + ")"

    smart_queries = []

    # -----------------------------------------------------------------------
    # V14.1: INTENT EXPANSION ENGINE
    # Deprecates literal keyword passthrough.
    # Gemini translates raw audience keywords into 3 platform-native
    # conversational queries that real humans use on the chosen vector.
    # -----------------------------------------------------------------------
    keyword_str = ", ".join(user_keywords) if user_keywords else ""
    if keyword_str:
        vector_label = sourcing_vector or "Classic B2B"
        intent_prompt = f"""You are an Intent Expansion Engine. The user is targeting this audience: '{keyword_str}'. The current digital platform vector is: '{vector_label}'.

Translate the user's target audience into exactly 3 natural-language, conversational queries that real humans actually type on this specific platform.

Platform-specific rules:
- If the vector is 'Social/Forum Listening': write queries as raw, first-person or question-style forum posts (e.g. 'how to waive IELTS requirement', 'universities that accept without English test').
- If the vector is 'Review Hijacking': write queries as review search terms or complaint phrases (e.g. 'problems with', 'disappointed by', 'looking for alternative to').
- If the vector is 'Maps/GMB Targeting': write geo-intent phrases (e.g. 'best [service] near me', '[service] in [city]').
- If the vector is 'Classic B2B': use professional industry terminology (e.g. 'enterprise [solution] provider', '[industry] workflow optimization').

Output ONLY a JSON array of exactly 3 strings. No explanation, no markdown."""
        try:
            translated_queries = call_gemini_2_5(intent_prompt, expect_json=True)
            if isinstance(translated_queries, list):
                for tq in translated_queries[:3]:
                    if isinstance(tq, str) and tq.strip():
                        smart_queries.append(f'"{tq.strip()}"{historical_str} {blacklist}')
                print(f"[INTENT ENGINE] Translated '{keyword_str}' → {len(smart_queries)} platform-native queries for '{vector_label}'")
        except Exception as e:
            print(f"[INTENT ENGINE] Translation failed: {e}. Falling back to literal keywords.")
            # Graceful fallback: use raw keywords if Gemini fails
            for kw in user_keywords:
                smart_queries.append(f'("{kw}"){historical_str} {blacklist}')

    for sd in symptom_dorks:
        smart_queries.append(f'{sd} {blacklist}')

    # V14: Inject vector-specific platform dorks AFTER translated queries
    if sourcing_vector and sourcing_vector in VECTOR_PLATFORM_MAP:
        for platform_dork in VECTOR_PLATFORM_MAP[sourcing_vector]:
            dork_q = f'{platform_dork}{historical_str} {blacklist}'
            smart_queries.append(dork_q)
        print(f"[SYNAPTIC ROUTER] Appended {len(VECTOR_PLATFORM_MAP[sourcing_vector])} platform dorks for vector: '{sourcing_vector}'")

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
    """
    V14: Returns a tiered dict {"High": [...urls], "Medium": [...urls]}.
    Low-confidence URLs are silently dropped.
    Uses strict JSON schema enforcement — no flat URL list hallucinations.
    """
    if not snippets:
        return {"High": [], "Medium": []}

    tiering_schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "url":             {"type": "STRING"},
                "confidence_tier": {"type": "STRING", "enum": ["High", "Medium", "Low"]},
                "reason":          {"type": "STRING"}
            },
            "required": ["url", "confidence_tier", "reason"]
        }
    }

    prompt = f"""CONFIDENCE TIERING GATE: Evaluate each URL snippet against the user's business context.

USER BIO: '{bio}'
TARGET LOCATION: '{location_target}'

# STEP 1 — PERSONA CLASSIFICATION (execute this first, before evaluating any URL)
Read the USER BIO and classify the user as:
- B2B Vendor: sells tools, services, or software TO businesses or professionals.
- B2C Service Provider: sells help, coaching, advice, or services DIRECTLY to individual consumers or students.

# STEP 2 — PERSONA-LOCKED TIERING RULES
Apply the correct ruleset based on the persona you classified:

IF B2B Vendor:
- High: The URL belongs to a business or professional entity that is EXPLICITLY experiencing the pain point the user solves, correct intent, correct geo.
- Medium: Ambiguous intent or geo, but clearly a relevant industry vertical.
- Low: Competitor, manufacturer, directory, aggregator (JustDial, Alibaba, Yelp, IndiaMart), SEO blog, D2C retail.

IF B2C Service Provider:
- High: The URL or snippet belongs to an INDIVIDUAL (not a company) who is EXPLICITLY expressing the pain point, frustration, or need in their own words.
- Medium: Ambiguous individual, or individual whose need is implied but not explicit.
- Low: Agency, university admin page, corporate entity, competitor, directory, or any organisational URL. Route ALL institutional/agency results to Low — B2C providers target individuals, not organisations.

# STEP 3 — UNIVERSAL RULES (always apply)
SOCIAL PLATFORM RULE: For Reddit, Quora, Facebook, LinkedIn — evaluate the SPECIFIC POST or COMMENT INTENT, not the platform. An individual asking for help = High/Medium. Platform homepage = Low.
GEO RULE: If a target explicitly serves a different region than '{location_target}', mark as Low.

Snippets to evaluate:
{json.dumps(snippets)}"""

    try:
        tiered_results = call_gemini_2_5(prompt, expect_json=True, response_schema=tiering_schema)
        if not isinstance(tiered_results, list):
            raise ValueError("Expected list from tiering gate")
    except Exception as e:
        print(f"[TIER GATE] Gemini tiering failed: {e}. Falling back to empty result.")
        return {"High": [], "Medium": []}

    output = {"High": [], "Medium": []}
    for item in tiered_results:
        tier = item.get("confidence_tier", "Low")
        url  = item.get("url", "").strip()
        if not url.startswith("http"):
            continue
        if tier == "High":
            output["High"].append(url)
        elif tier == "Medium":
            output["Medium"].append(url)
        # Low: silently drop

    print(f"[TIER GATE] High={len(output['High'])}, Medium={len(output['Medium'])}, Low dropped.")
    return output

def extract_root_domain(url):
    try:
        netloc = urlparse(url).netloc.lower()
        if not netloc:
             netloc = urlparse('http://' + url).netloc.lower()
        netloc = netloc.replace('www.', '')
        return netloc
    except:
        return ""

def deep_context_serper_dork(domain, tenant_id, sourcing_vector="Classic B2B"):
    """
    V14.4 HOTFIX: Enrichment Gatekeeper.
    Skips ALL Serper calls for:
      1. Social/UGC domains (reddit, facebook, instagram, youtube, etc.)
      2. B2C sourcing vectors (no company LinkedIn / Naukri job listings exist)
    """
    if not domain: return "", False

    # ── GATEKEEPER: Social Domain Blacklist ───────────────────────────────────
    ENRICHMENT_SOCIAL_BLACKLIST = [
        "reddit.com", "facebook.com", "instagram.com", "youtube.com",
        "linkedin.com", "quora.com", "twitter.com", "x.com", "medium.com"
    ]
    cleaned_domain = domain.lower().replace("www.", "")
    for blocked in ENRICHMENT_SOCIAL_BLACKLIST:
        if blocked in cleaned_domain:
            print(f"[ENRICHMENT] Bypassing company enrichment for B2C/Social domain: {domain}")
            return "", False

    # ── GATEKEEPER: B2C Persona Bypass ───────────────────────────────────────
    # B2C vectors do not have company LinkedIn pages or Naukri job listings.
    # Running enrichment searches against them burns credits with zero signal.
    B2C_VECTORS = [
        "Reddit B2C", "Quora B2C", "Google Maps B2C",
        "TripAdvisor B2C", "YouTube B2C", "Facebook Groups B2C"
    ]
    if sourcing_vector in B2C_VECTORS:
        print(f"[ENRICHMENT] Bypassing company enrichment for B2C/Social domain: {domain} (vector={sourcing_vector})")
        return "", False

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

def final_score_and_dm(text, bio, context_payload, tech_stack, historical_dms=None, source_url=None):
    """
    V14.1: Persona Value-Chain Matrix + Polymorphic URI hardening.
    - Infers B2B/B2C persona from bio and aligns DM tone accordingly.
    - Forces social profile link extraction from forum DOMs.
    - Strict contact_endpoints schema with enum-locked platform field.
    """
    # Detect social/forum origin for the URI hardening rule
    social_domains = ["reddit.com", "quora.com", "facebook.com", "linkedin.com", "instagram.com"]
    is_social_source = source_url and any(d in source_url.lower() for d in social_domains)
    social_platform = "other"
    if source_url:
        if "reddit.com"    in source_url: social_platform = "reddit"
        elif "quora.com"   in source_url: social_platform = "other"
        elif "facebook.com" in source_url: social_platform = "facebook"
        elif "linkedin.com" in source_url: social_platform = "linkedin"
        elif "instagram.com" in source_url: social_platform = "instagram"

    social_uri_rule = ""
    if is_social_source:
        social_uri_rule = f"""

SOCIAL PROFILE URI RULE (MANDATORY — this source is from a social/forum platform):
The source URL '{source_url}' originates from a social network or forum.
You MUST extract the URL of the original poster's user profile from the DOM text.
Map this profile link to the contact_endpoints array using the correct platform enum ('{social_platform}').
Do NOT return an empty contact_endpoints array if a user profile link is present in the text.
Look for patterns like '/u/', '/user/', '/profile/', '@username', or any author attribution link."""

    prompt = f"""You are an Elite Extraction Engine with dynamic persona intelligence.

# STEP 1 — PERSONA CLASSIFICATION
Before scoring, classify the user based on their bio: '{bio}'.
- B2B Vendor: sells tools, software, or services TO other businesses.
- B2C Service Provider: sells coaching, advice, or services DIRECTLY to individual consumers or students.

# STEP 2 — PERSONA-LOCKED SCORING & EXTRACTION
Score this lead 1-10 based on how well it matches the bio above.

IF B2B Vendor:
- The ideal target is a business decision-maker experiencing the user's stated pain point.
- A generic homepage, ad, or content without a human contact MUST score 0.
- Outreach (dm field) MUST use professional B2B tone: direct, outcome-driven, no fluff.

IF B2C Service Provider:
- The ideal target is an INDIVIDUAL (not a company) who has explicitly expressed the need in their own words.
- Corporate pages, university sites, agencies, or competitor pages MUST score 0.
- Outreach (dm field) MUST be warm, personal, direct-to-consumer. DO NOT write enterprise software pitches or B2B jargon.
- Example: A career counselor should send a personal offer of help, NOT an ROI pitch.

# STEP 3 — EXTRACTION RULES
For hiring_intent_found: Return ONLY 'Yes' or 'No'. No explanation.

For contact_endpoints: Extract ALL reachable contact surfaces explicitly present in the text.
Each endpoint must have a 'platform' from the strict enum and a 'uri'.

URI PROTOCOL RULE (MANDATORY): Every URI MUST include its full protocol prefix. Never return a naked domain:
- Web profile URLs: must start with https:// (e.g. https://reddit.com/user/johndoe, https://linkedin.com/in/jane)
- Email addresses: return ONLY the email string (e.g. hello@company.com), the frontend will prefix mailto:
- Phone numbers: return ONLY the digits/number string, the frontend will prefix tel:
- If a URI would be a naked domain (e.g. 'reddit.com' with no path), DO NOT include it.

REDDIT TARGETING RULE (MANDATORY — only applies when source is Reddit):
You MUST extract the href of the original poster's author attribution link (e.g. https://reddit.com/user/[username]).
If the poster's profile URL cannot be found in the DOM, do NOT invent one. Instead, omit this endpoint entirely.
A generic 'https://reddit.com' URI with no username path is useless and MUST NOT be returned.

PHONE DEDUPLICATION RULE: If the DOM contains more than 3 telephone numbers, return ONLY the first 2 in contact_endpoints. Excess numbers create UI clutter.

Do NOT invent contacts. Only extract what is explicitly present in the DOM.
{social_uri_rule}

For intent_signal: Write one precise sentence explaining the specific signal in this content that proves they need the user's solution.

CONTEXTUAL DORKING DATA:
{context_payload}

DETECTED TECH STACK:
{', '.join(tech_stack) if tech_stack else 'None extracted'}
"""

    if historical_dms:
        prompt += f"\nPast successful converted messages (match tone and length strictly): {historical_dms}\n"

    prompt += f"\nUsing all context, write a hyper-personalized outreach message aligned to the persona above, targeting the identified pain point.\n\nText DOM:\n{text}"

    sys_inst = "You are an Elite Extraction Engine. Extract factual data with precision and draft concise, high-converting outreach. Adapt your tone strictly to the user's persona (B2B/B2C). Never hallucinate contacts. Never use fluff or generic greetings."

    # V14: Strict polymorphic contact schema — enum-locked platform field
    schema = {
        "type": "OBJECT",
        "properties": {
            "score": {"type": "INTEGER"},
            "dm": {
                "type": "STRING",
                "description": "Drafted outreach message. Output exact string 'N/A' if insufficient data."
            },
            "pain_point": {
                "type": "STRING",
                "description": "Specific pain point extracted. Output 'N/A' if insufficient data."
            },
            "icebreaker_angle": {
                "type": "STRING",
                "description": "The tactical angle for the icebreaker. Output 'N/A' if insufficient data."
            },
            "intent_signal": {
                "type": "STRING",
                "description": "One precise sentence: the specific signal in the content proving they need the user's solution."
            },
            "hiring_intent_found": {
                "type": "STRING",
                "enum": ["Yes", "No"]
            },
            "tech_stack_found": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Only verified software technologies found in the HTML. No internal notes."
            },
            "contact_endpoints": {
                "type": "ARRAY",
                "description": "ALL reachable contact surfaces found. Only extract explicitly present contacts.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "platform": {
                            "type": "STRING",
                            "enum": ["instagram", "reddit", "whatsapp", "gmb", "email", "linkedin", "facebook", "other"]
                        },
                        "uri": {
                            "type": "STRING",
                            "description": "The email address, profile URL, phone number, map link, or handle."
                        }
                    },
                    "required": ["platform", "uri"]
                }
            },
            "decision_maker_name": {
                "type": "STRING",
                "description": "Specific human name found in the text. Use 'Unknown' if not found."
            },
            "decision_maker_title": {
                "type": "STRING",
                "description": "Title of the decision maker. Use 'Unknown' if not found."
            },
            "company_size_tier": {
                "type": "STRING",
                "description": "Strictly one of: 'Startup', 'Mid-Market', 'Enterprise', 'Unknown'."
            },
            "primary_objection_hypothesis": {
                "type": "STRING",
                "description": "One sentence: why they might reject the pitch based on their site context."
            },
            "company_name": {
                "type": "STRING",
                "description": "The legal or trading name of the company/brand. Output 'Unknown' if not determinable from the text."
            }
        },
        "required": [
            "score", "dm", "pain_point", "icebreaker_angle", "intent_signal",
            "hiring_intent_found", "tech_stack_found", "contact_endpoints",
            "decision_maker_name", "decision_maker_title",
            "company_size_tier", "primary_objection_hypothesis"
            # company_name intentionally NOT in required — optional extraction
        ]
    }

    try:
        data = call_gemini_2_5(prompt, expect_json=True, response_schema=schema, system_instruction=sys_inst)
        if not isinstance(data, dict):
            raise ValueError("Parsed JSON is not a dictionary.")

        return {
            "score":                        data.get("score", 0),
            "pain_point":                   data.get("pain_point", "Unknown"),
            "hiring_intent_found":          data.get("hiring_intent_found", "No"),
            "tech_stack_found":             data.get("tech_stack_found", []),
            "icebreaker_angle":             data.get("icebreaker_angle", ""),
            "intent_signal":                data.get("intent_signal", ""),
            "dm":                           data.get("dm", "Failed to generate DM"),
            "contact_endpoints":            data.get("contact_endpoints", []),
            "decision_maker_name":          data.get("decision_maker_name", "Unknown"),
            "decision_maker_title":         data.get("decision_maker_title", "Unknown"),
            "company_size_tier":            data.get("company_size_tier", "Unknown"),
            "primary_objection_hypothesis": data.get("primary_objection_hypothesis", "Unknown"),
            # Optional — None if Gemini couldn't determine it
            "company_name":                 data.get("company_name") or None,
        }
    except Exception as e:
        raise ValueError(f"LLM Parsing Failure: {e}")

@app.route("/produce", methods=["POST"])
def produce():
    """
    V14.4: THE PRODUCER — 24-Hour Serper Fetch Job.
    Runs Intent Translation (Step 3) and Serper Execution (Step 4).
    Deduplicates against global leads collection.
    Writes unprocessed URLs to campaigns/{id}.unprocessed_queue.
    Does NOT call the Gemini Gate. Halts here.
    """
    lead_data = request.json
    tenant_id   = lead_data.get("tenant_id")
    campaign_id = lead_data.get("campaign_id")
    if not tenant_id or not campaign_id:
        print(f"[PRODUCER] CRITICAL: Missing tenant_id or campaign_id. Aborting.")
        return jsonify({"error": "Missing campaign_id or tenant_id"}), 400

    campaign_ref  = db.collection("campaigns").document(campaign_id)
    campaign      = campaign_ref.get().to_dict() or {}
    bio           = campaign.get("bio", "")
    sourcing_vector = campaign.get("sourcing_vector", "Classic B2B")
    location      = campaign.get("location", "").strip()
    gl            = campaign.get("gl", "").strip()

    raw_keywords  = campaign.get("keywords", "")
    keywords      = [k.strip() for k in raw_keywords.split(',') if k.strip()] \
                    if isinstance(raw_keywords, str) else raw_keywords

    if not keywords:
        print(f"[PRODUCER] Campaign {campaign_id}: empty keywords. Aborting.")
        return jsonify({"error": "Empty keywords matrix"}), 400

    print(f"[SYNAPTIC ROUTER] Campaign {campaign_id} → sourcing vector: '{sourcing_vector}'")

    # ── Step 3: Intent Translation + Smart Query Generation ─────────────────
    smart_keywords = generate_smart_query(keywords, tenant_id, bio, sourcing_vector)

    # Telemetry billing
    db.collection("usage_metrics").document(tenant_id).set(
        {"serper_searches": firestore.Increment(len(smart_keywords))}, merge=True
    )

    # ── Step 4: Serper Execution ─────────────────────────────────────────────
    # P2: Track snippet text alongside each URL for walled-garden hand-off
    SOCIAL_DOMAINS_PRODUCER = ["reddit.com", "linkedin.com", "facebook.com",
                                "instagram.com", "x.com", "twitter.com",
                                "quora.com", "youtube.com", "team-bhp.com"]

    raw_urls    = []       # ordered list of raw URLs
    snippet_db  = {}       # url → {"title": ..., "snippet": ...} for hand-off

    for kw in smart_keywords:
        # ── Query Builder Guard: clean location string, no orphaned AND operators ──
        # Strips whitespace, ignores 'all', prevents contradictory geo injection.
        clean_location = location.strip() if location else ""
        if clean_location and clean_location.lower() != "all":
            search_query = f"{kw} AND {clean_location}"
        else:
            search_query = kw
        raw_results  = search_serper(search_query, location=clean_location or None, gl=gl or None)
        filtered     = filter_serper_noise(raw_results)
        for r in filtered:
            link = r.get("link")
            if link and link not in raw_urls:
                raw_urls.append(link)
                # Capture snippet for later scraped_cache persistence
                snippet_db[link] = {
                    "title":   r.get("title",   ""),
                    "snippet": r.get("snippet", "")
                }

    # Also include target_urls from campaign doc as first-class candidates
    target_urls = campaign.get("target_urls", [])
    for u in (target_urls[:10] if isinstance(target_urls, list) else []):
        if u not in raw_urls:
            raw_urls.insert(0, u)
            if u not in snippet_db:
                snippet_db[u] = {"title": "", "snippet": ""}

    fetched_count = len(raw_urls)
    print(f"[FUNNEL] Campaign: {campaign_id} | Producer: Fetched {fetched_count} URLs")

    # ── P2: Persist Serper snippets to scraped_cache for social/walled-garden URLs ──
    # This is the hand-off point. The Consumer will read these from Firestore
    # instead of receiving an empty snippet_map.
    for surl, meta in snippet_db.items():
        s_domain = extract_root_domain(surl)
        is_social_url = any(s_domain.endswith(d) for d in SOCIAL_DOMAINS_PRODUCER)
        combined_text = f"{meta['title']}\n{meta['snippet']}".strip()
        if is_social_url and combined_text:
            try:
                cache_key = surl.replace('/', '_')
                db.collection("scraped_cache").document(cache_key).set({
                    "url":      surl,
                    "text":     combined_text,
                    "source":   "serper_snippet",
                    "tech_stack": [],
                    "emails":   [],
                    "phones":   []
                }, merge=True)
            except Exception as se:
                print(f"[PRODUCER] Snippet persist failed for {surl}: {se}")

    # ── P0: Native Global Deduplication — path-aware for social domains ───────
    # Previously: all reddit threads hashed to SHA256(tenant_reddit.com)
    # Fix: social URLs hash by full URL; B2B URLs still hash by domain.
    existing_ids = set()
    try:
        known_docs = db.collection("leads").where(
            "tenant_id", "==", tenant_id
        ).select(["url"]).stream()
        for doc in known_docs:
            d = doc.to_dict()
            u = d.get("url", "")
            if u:
                d_domain   = extract_root_domain(u)
                is_social  = any(d_domain.endswith(s) for s in SOCIAL_DOMAINS_PRODUCER)
                dedup_key  = u if is_social else d_domain   # ← FIX: full URL for social
                lead_id_ex = hashlib.sha256(f"{tenant_id}_{dedup_key}".encode()).hexdigest()
                existing_ids.add(lead_id_ex)
                existing_ids.add(u)  # also track raw URL as secondary guard
    except Exception as dedup_e:
        print(f"[PRODUCER] Dedup query failed: {dedup_e}. Continuing without dedup.")

    fresh_urls = []
    for url in raw_urls:
        f_domain  = extract_root_domain(url)
        is_social = any(f_domain.endswith(s) for s in SOCIAL_DOMAINS_PRODUCER)
        dedup_key = url if is_social else f_domain          # ← FIX: full URL for social
        lead_id_h = hashlib.sha256(f"{tenant_id}_{dedup_key}".encode()).hexdigest()
        if lead_id_h not in existing_ids and url not in existing_ids:
            fresh_urls.append(url)

    duped_count  = fetched_count - len(fresh_urls)
    queued_count = len(fresh_urls)
    print(f"[FUNNEL] Campaign: {campaign_id} | Producer: Fetched {fetched_count} URLs | Deduplicated: {duped_count} | Queued: {queued_count}")

    # ── Write to unprocessed_queue (additive merge, cap at 200) ─────────────
    current_queue = campaign.get("unprocessed_queue", [])
    combined = list(dict.fromkeys(current_queue + fresh_urls))  # preserve order, dedupe
    combined = combined[:200]  # hard cap to prevent runaway growth

    campaign_ref.update({
        "unprocessed_queue":  combined,
        "last_produced_at":   firestore.SERVER_TIMESTAMP,
    })

    print(f"[PRODUCER] Campaign {campaign_id}: queue now has {len(combined)} URLs.")
    return jsonify({
        "status":        "produced",
        "fetched":       fetched_count,
        "deduplicated":  duped_count,
        "queued":        queued_count,
        "queue_depth":   len(combined)
    }), 200


@app.route("/dispatch", methods=["POST"])
def dispatch():
    """
    V14.4: THE CONSUMER — 4-Hour Drip Processor.
    Pops exactly 10 URLs from campaigns/{id}.unprocessed_queue (destructive read).
    Runs Step 5 (Gemini Confidence Gate) and Step 6 (Playwright Scraper).
    Does NOT call Serper. If queue is empty, exits gracefully.
    """
    lead_data = request.json
    tenant_id = lead_data.get("tenant_id")

    target_campaign_id = lead_data.get("campaign_id") or (
        lead_data.get("matched_campaigns")[0] if lead_data.get("matched_campaigns") else None
    )
    if not target_campaign_id:
        print("[CONSUMER] CRITICAL: No identifiable campaign context. Dropping task.")
        return jsonify({"error": "Missing campaign_id context"}), 400

    campaign_id  = target_campaign_id
    campaign_ref = db.collection("campaigns").document(campaign_id)
    campaign     = campaign_ref.get().to_dict() or {}
    bio          = campaign.get("bio", "")
    sourcing_vector = campaign.get("sourcing_vector", "Classic B2B")
    location     = campaign.get("location", "").strip()

    # ── Destructive Queue Pop (Batch of 10) — Race Condition Safe ────────────
    # We immediately write the queue MINUS the batch before processing begins.
    # This prevents double-processing if two tasks fire concurrently.
    current_queue = campaign.get("unprocessed_queue", [])

    if not current_queue:
        print(f"[CONSUMER] Campaign {campaign_id}: unprocessed_queue is empty. Exiting gracefully.")
        return jsonify({"status": "queue_empty", "processed": 0}), 200

    BATCH_SIZE    = 10
    batch_urls    = current_queue[:BATCH_SIZE]
    remaining     = current_queue[BATCH_SIZE:]

    # Atomic destructive read — remove batch from queue immediately
    campaign_ref.update({"unprocessed_queue": remaining})

    print(f"[FUNNEL] Campaign: {campaign_id} | Consumer: Processing Batch of {len(batch_urls)} URLs")

    user_doc             = db.collection("users").document(tenant_id).get()
    preferences_weights  = user_doc.to_dict().get("preferences_weights", {}) if user_doc.exists else {}

    # ── P2: Hydrate snippet_map from scraped_cache (Producer hand-off) ────────
    # The Producer wrote Serper snippets to scraped_cache with source=serper_snippet.
    # Load them now so the social short-circuit path has real context.
    snippet_map = {}
    for batch_url in batch_urls:
        b_domain   = extract_root_domain(batch_url)
        is_social  = any(b_domain.endswith(s) for s in ["reddit.com", "linkedin.com", "facebook.com",
                                                          "instagram.com", "x.com", "twitter.com",
                                                          "quora.com", "youtube.com", "team-bhp.com"])
        if is_social:
            try:
                cache_key = batch_url.replace('/', '_')
                cdoc = db.collection("scraped_cache").document(cache_key).get()
                if cdoc.exists:
                    cached_text = cdoc.to_dict().get("text", "")
                    if cached_text:
                        snippet_map[batch_url] = cached_text
            except Exception as sm_e:
                print(f"[CONSUMER] snippet_map hydration failed for {batch_url}: {sm_e}")

    # ── Step 5: Confidence Tiering Gate ─────────────────────────────────────
    # Feed batch URLs into pre_filter_gemini. Include snippet text where available
    # so the tiering LLM has real context (not just bare URLs) for social leads.
    synthetic_snippets = [
        {"link": u, "snippet": snippet_map.get(u, ""), "title": ""}
        for u in batch_urls
    ]
    tiered = pre_filter_gemini(synthetic_snippets, bio, location)
    high_urls   = tiered.get("High", [])
    medium_urls = tiered.get("Medium", [])

    velocity_threshold = int(os.environ.get("VELOCITY_THRESHOLD", "10"))
    try:
        cutoff_24h   = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        recent_count = (
            db.collection("leads")
            .where("tenant_id", "==", tenant_id)
            .where("status",    "==", "new")
            .where("createdAt", ">=", cutoff_24h)
            .count().get()[0][0].value
        )
    except Exception as vel_e:
        print(f"[VELOCITY] Count query failed: {vel_e}. Defaulting to allow Medium.")
        recent_count = 0

    allow_medium      = recent_count < velocity_threshold
    approved_urls     = high_urls + (medium_urls if allow_medium else [])
    gate_rejected     = len(batch_urls) - len(approved_urls)
    print(f"[FUNNEL] Campaign: {campaign_id} | Gate (Step 5) Rejected: {gate_rejected} | Passed to Scraper: {len(approved_urls)}")

    url_to_tier = {u: "High" for u in high_urls}
    url_to_tier.update({u: "Medium" for u in medium_urls})

    # ── Step 6: Playwright Scraper + Gemini Extraction ──────────────────────
    SOCIAL_DOMAINS = ["linkedin.com", "facebook.com", "reddit.com", "instagram.com",
                      "x.com", "twitter.com", "team-bhp.com", "quora.com", "youtube.com"]

    all_results    = []
    scrape_success = 0
    scrape_failed  = 0

    for url in approved_urls:
        target_domain = extract_root_domain(url)
        if not target_domain:
            continue

        # Social path detection
        is_social = any(target_domain.endswith(s) for s in SOCIAL_DOMAINS)
        if is_social:
            parsed_url  = urlparse(url)
            exact_path  = f"{parsed_url.netloc}{parsed_url.path}".lower().replace('www.', '')
            lock_entity = hashlib.sha256(exact_path.encode()).hexdigest()
            dedupe_target = exact_path
        else:
            lock_entity   = target_domain
            dedupe_target = target_domain

        # Global Exclusivity Lock
        lock_ref  = db.collection("global_lead_locks").document(lock_entity)
        try:
            lock_doc  = lock_ref.get()
            now_utc   = datetime.datetime.now(datetime.timezone.utc)
            if lock_doc.exists:
                locked_until = lock_doc.to_dict().get("locked_until")
                if locked_until and locked_until > now_utc:
                    print(f"[EXCLUSIVITY] Dropping {url}. Entity {lock_entity} locked.")
                    continue
            lock_ref.set({"locked_until": now_utc + datetime.timedelta(days=14)})
        except Exception as le:
            print(f"[LOCK FAIL] {le}")

        # Deterministic Dedup Gateway
        lead_id_str = f"{tenant_id}_{dedupe_target}"
        lead_id     = hashlib.sha256(lead_id_str.encode()).hexdigest()
        doc_ref     = db.collection("leads").document(lead_id)

        try:
            doc_ref.create({
                "tenant_id":        tenant_id,
                "matched_campaigns": [campaign_id],
                "url":              url,
                "confidence_tier":  url_to_tier.get(url, "High"),
                "sourcing_vector":  sourcing_vector,
                "status":           "processing",
                "createdAt":        firestore.SERVER_TIMESTAMP
            })
        except AlreadyExists:
            print(f"[UAR] Cross-campaign duplicate for {target_domain}. Updating matched_campaigns.")
            doc_ref.update({"matched_campaigns": firestore.ArrayUnion([campaign_id])})
            continue

        try:
            # Cache check
            cache_ref  = db.collection("scraped_cache").document(url.replace('/', '_'))
            cache_doc  = cache_ref.get()
            text, tech_stack, emails, phones = "", [], [], []

            if cache_doc.exists:
                c_data     = cache_doc.to_dict()
                text       = c_data.get("text", "")
                tech_stack = c_data.get("tech_stack", [])
                emails     = c_data.get("emails", [])
                phones     = c_data.get("phones", [])
            elif is_social:
                # ── P2: Social short-circuit — read from hydrated snippet_map ──
                # snippet_map was loaded from scraped_cache (Producer hand-off) at
                # the top of dispatch(). It contains real Serper title+snippet text.
                raw_snippet = snippet_map.get(url, "")
                if raw_snippet:
                    text       = raw_snippet
                    tech_stack = ["Social Platform Bypass"]
                    print(f"[SOCIAL SHORT-CIRCUIT] Real snippet ({len(text)} chars) for {url}")
                else:
                    # ── P3: No snippet available — ghost state prevention ──────
                    # Do NOT leave doc in 'processing'. Mark dropped and skip.
                    print(f"[SOCIAL SHORT-CIRCUIT] No snippet available for {url} — marking dropped.")
                    doc_ref.update({"status": "dropped_no_context", "error": "No Serper snippet in cache"})
                    scrape_failed += 1
                    continue
            else:
                try:
                    text, tech_stack, emails, phones = scrape_url(url)
                    scrape_success += 1
                except ValueError as e:
                    if str(e) == "DEFERRED":
                        print(f"[DEFERRED] Queueing async task to scraper-heavy for {url}")
                        from google.cloud import tasks_v2 as _tv2
                        _tc = _tv2.CloudTasksClient()
                        _parent = _tc.queue_path(PROJECT_ID, LOCATION, QUEUE)
                        _task = {
                            "http_request": {
                                "http_method": _tv2.HttpMethod.POST,
                                "url": SCRAPER_HEAVY_URL,
                                "headers": {"Content-Type": "application/json"},
                                "body": json.dumps({
                                    "url": url, "lead_id": lead_id, "tenant_id": tenant_id,
                                    "campaign_id": campaign_id, "bio": bio,
                                    "target_domain": target_domain,
                                    "preferences_weights": preferences_weights
                                }).encode()
                            }
                        }
                        _tc.create_task(parent=_parent, task=_task)
                        continue
                    # ── P3: Non-DEFERRED scrape failure — mark failed, do not ghost ──
                    print(f"[SCRAPE FAIL] {url}: {e}")
                    doc_ref.update({"status": "failed_scrape", "error": str(e)})
                    scrape_failed += 1
                    continue  # ← was: text="", now we skip entirely

                if text:
                    cache_ref.set({"url": url, "text": safe_truncate(text),
                                   "tech_stack": tech_stack, "emails": emails, "phones": phones})

            if text:
                bot_keywords = ["Cloudflare Ray ID", "Please verify you are human",
                                "Enable JavaScript and cookies to continue",
                                "Checking if the site connection is secure",
                                "Access Denied", "403 Forbidden"]
                if any(kw.lower() in text.lower() for kw in bot_keywords):
                    doc_ref.update({"status": "failed", "error": "Blocked by Cloudflare/WAF"})
                    scrape_failed += 1
                    continue

                shard_id = random.randint(0, 9)
                db.collection("usage_metrics").document(tenant_id).collection("shards") \
                    .document(str(shard_id)).set({"gemini_calls": firestore.Increment(1)}, merge=True)
                db.collection("users").document(tenant_id).collection("wallet_shards") \
                    .document(str(shard_id)).set({"consumed_credits": firestore.Increment(1)}, merge=True)

                context_payload, native_hiring_intent = deep_context_serper_dork(target_domain, tenant_id, sourcing_vector)

                # RLHF Fit Score
                fit_score = 0
                if native_hiring_intent:
                    fit_score += preferences_weights.get("hiring_intent", 0)
                for tech in tech_stack:
                    fit_score += preferences_weights.get(f"tech_{tech}", 0)
                if fit_score <= -3:
                    print(f"[RLHF] Dropping {target_domain} (fit_score={fit_score}).")
                    doc_ref.delete()
                    continue

                try:
                    evaluation = final_score_and_dm(text, bio, context_payload, tech_stack, source_url=url)
                except TimeoutError:
                    db.collection("leads").document(lead_id).update({"status": "failed", "error": "Vertex AI timeout"})
                    scrape_failed += 1
                    continue
                except Exception as e:
                    db.collection("leads").document(lead_id).update({"status": "failed", "error": str(e)})
                    scrape_failed += 1
                    continue

                # ── P4: Dynamic acceptance threshold ─────────────────────────
                # Snippet-sourced leads (< 500 chars) lack DOM depth.
                # Gemini cannot confidently score them >= 7 even with clear intent.
                # Lower to >= 6 for thin payloads so snippet leads are not mass-dropped.
                is_thin_payload = len(text.strip()) < 500
                accept_threshold = 6 if is_thin_payload else 7
                print(f"[THRESHOLD] Payload: {len(text)} chars → threshold: {accept_threshold} (thin={is_thin_payload})")

                if evaluation.get("score", 0) >= accept_threshold:
                    contact_endpoints = list(evaluation.get("contact_endpoints", []))
                    existing_uris     = {e["uri"] for e in contact_endpoints}
                    for em in (emails or [])[:3]:
                        if em and em not in existing_uris:
                            contact_endpoints.append({"platform": "email", "uri": em})
                            existing_uris.add(em)
                    for ph in (phones or [])[:2]:
                        if ph and ph not in existing_uris:
                            contact_endpoints.append({"platform": "other", "uri": ph})
                            existing_uris.add(ph)

                    # ── Universal Data Contract write (dispatch path) ─────────
                    lead_payload = {
                        "id":                           lead_id,
                        "source_url":                   url,
                        "tenant_id":                    tenant_id,
                        "origin_engine":                "cartographer",
                        "score":                        evaluation.get("score", 0),
                        "pain_point":                   evaluation.get("pain_point", ""),
                        "dm":                           evaluation.get("dm", ""),
                        "intent_signal":                evaluation.get("intent_signal", ""),
                        "hiring_intent_found":          evaluation.get("hiring_intent_found", "No"),
                        "tech_stack_found":             evaluation.get("tech_stack_found", []),
                        "icebreaker_angle":             evaluation.get("icebreaker_angle", ""),
                        "contact_endpoints":            contact_endpoints,
                        "decision_maker_name":          evaluation.get("decision_maker_name", "Unknown"),
                        "decision_maker_title":         evaluation.get("decision_maker_title", "Unknown"),
                        "company_size_tier":            evaluation.get("company_size_tier", "Unknown"),
                        "primary_objection_hypothesis": evaluation.get("primary_objection_hypothesis", "Unknown"),
                        "company_name":                 evaluation.get("company_name"),   # Optional
                        "dossier_text":                 None,                              # V16 placeholder
                        "sourcing_vector":               sourcing_vector,
                        "confidence_tier":               url_to_tier.get(url, "High"),
                        "status":                        "new",
                    }
                    validate_and_update_lead(lead_payload, doc_ref)

                    scrape_success += 1

                    # Meta WhatsApp Business API Trigger
                    if evaluation.get("score", 0) >= 8:
                        tenant_doc       = db.collection("users").document(tenant_id).get().to_dict() or {}
                        wa_token_encrypted = tenant_doc.get("wa_token")
                        wa_phone_id      = tenant_doc.get("wa_phone_id")
                        admin_phone      = tenant_doc.get("admin_phone")
                        wa_token         = None
                        if wa_token_encrypted:
                            try:
                                wa_token = cipher_suite.decrypt(wa_token_encrypted.encode()).decode()
                            except:
                                wa_token = wa_token_encrypted
                        if wa_token and wa_phone_id and admin_phone:
                            wa_payload = {
                                "messaging_product": "whatsapp",
                                "to": admin_phone, "type": "interactive",
                                "interactive": {
                                    "type": "button",
                                    "body": {"text": f"🔥 Hot Lead!\n{url}\nScore: {evaluation.get('score')}/10\n{evaluation.get('pain_point')}\n\nDM: {evaluation.get('dm')}"},
                                    "action": {"buttons": [
                                        {"type": "reply", "reply": {"id": f"approve_{lead_id}", "title": "✅ Approve"}},
                                        {"type": "reply", "reply": {"id": f"ignore_{lead_id}",  "title": "🚫 Ignore"}}
                                    ]}
                                }
                            }
                            try:
                                httpx.post(
                                    f"https://graph.facebook.com/v18.0/{wa_phone_id}/messages",
                                    json=wa_payload,
                                    headers={"Authorization": f"Bearer {wa_token}", "Content-Type": "application/json"},
                                    timeout=5
                                )
                            except Exception as wa_e:
                                print(f"[WA] Meta POST failed: {wa_e}")

                    all_results.append({"url": url, "score": evaluation.get("score")})
                else:
                    # Score below threshold — clean delete (not a ghost)
                    print(f"[SCORE GATE] {url} scored {evaluation.get('score', 0)} < {accept_threshold}. Deleting stub.")
                    doc_ref.delete()
            else:
                # ── P3: text is empty after all code paths — ghost state prevention ──
                print(f"[DEAD PAYLOAD] No text for {url} after all paths. Marking failed_scrape.")
                doc_ref.update({"status": "failed_scrape", "error": "Empty DOM — no cache, no snippet, no scrape"})
                scrape_failed += 1

        except Exception as loop_e:
            print(f"[CONSUMER] Pipeline loop crashed for {url}: {loop_e}")
            try:
                db.collection('leads').document(lead_id).update(
                    {'status': 'failed', 'error': 'Consumer pipeline crash'}
                )
            except:
                pass
            scrape_failed += 1
            continue

    print(f"[FUNNEL] Campaign: {campaign_id} | Scraper Success: {scrape_success} | Failed/Timeout: {scrape_failed}")
    return jsonify({"processed_leads": len(all_results), "scrape_success": scrape_success, "scrape_failed": scrape_failed}), 200
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
        doc_ref.update({"status": "failed_scrape", "error": "scraper-heavy returned empty text"})
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
        
        # Fetch sourcing_vector from the lead doc (scraper-heavy payload doesn't include it)
        lead_doc_data   = doc_ref.get().to_dict() or {}
        sourcing_vector = lead_doc_data.get("sourcing_vector", "Classic B2B")

        context_payload, native_hiring_intent = deep_context_serper_dork(target_domain, tenant_id, sourcing_vector)
        
        try:
            docs = db.collection("leads").where("tenant_id", "==", tenant_id).where("status", "==", "converted").order_by("updatedAt", direction=firestore.Query.DESCENDING).limit(3).stream()
            historical_dms = [doc.to_dict().get("dm") for doc in docs if doc.to_dict().get("dm")]
            
            evaluation = final_score_and_dm(dense_text, bio, context_payload, tech_stack, historical_dms, source_url=url)
        except TimeoutError:
            doc_ref.update({"status": "failed", "error": "Vertex AI timeout"})
            return jsonify({"status": "timeout"}), 200
        except Exception as e:
            doc_ref.update({"status": "failed", "error": str(e)})
            return jsonify({"status": "failed"}), 200
        
        # P4: Dynamic threshold — scraper-heavy always provides full DOM text, so
        # dense_text >= 500 chars is expected; standard >= 7 applies.
        # Guard for edge cases where Playwright returned a thin extract.
        is_thin_payload  = len(dense_text.strip()) < 500
        accept_threshold = 6 if is_thin_payload else 7
        print(f"[FINALIZE THRESHOLD] Payload: {len(dense_text)} chars → threshold: {accept_threshold}")

        if evaluation.get("score", 0) >= accept_threshold:
            # V14: Polymorphic contact merge — LLM endpoints + Playwright-scraped contacts
            contact_endpoints = list(evaluation.get("contact_endpoints", []))
            existing_uris = {e["uri"] for e in contact_endpoints}
            for em in (emails or [])[:3]:
                if em and em not in existing_uris:
                    contact_endpoints.append({"platform": "email", "uri": em})
                    existing_uris.add(em)
            for ph in (phones or [])[:2]:
                if ph and ph not in existing_uris:
                    contact_endpoints.append({"platform": "other", "uri": ph})
                    existing_uris.add(ph)

            # ── Universal Data Contract write (finalize/scraper-heavy path) ──
            lead_payload = {
                "id":                           lead_id,
                "source_url":                   url,
                "tenant_id":                    tenant_id,
                "origin_engine":                "cartographer",
                "score":                        evaluation.get("score", 0),
                "pain_point":                   evaluation.get("pain_point", ""),
                "dm":                           evaluation.get("dm", ""),
                "intent_signal":                evaluation.get("intent_signal", ""),
                "hiring_intent_found":          evaluation.get("hiring_intent_found", "No"),
                "tech_stack_found":             evaluation.get("tech_stack_found", []),
                "icebreaker_angle":             evaluation.get("icebreaker_angle", ""),
                "contact_endpoints":            contact_endpoints,
                "decision_maker_name":          evaluation.get("decision_maker_name", "Unknown"),
                "decision_maker_title":         evaluation.get("decision_maker_title", "Unknown"),
                "company_size_tier":            evaluation.get("company_size_tier", "Unknown"),
                "primary_objection_hypothesis": evaluation.get("primary_objection_hypothesis", "Unknown"),
                "company_name":                 evaluation.get("company_name"),   # Optional
                "dossier_text":                 None,                              # V16 placeholder
                "sourcing_vector":               sourcing_vector,
                "status":                        "new",
            }
            validate_and_update_lead(lead_payload, doc_ref)
            
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
