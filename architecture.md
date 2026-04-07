# Lead Sniper / Sideio Smart Growth — V13.22
**Full Technical Specification Document (TSD)**
*Last Updated: 2026-04-07 | Version: V13.22 Real-Time WebSocket Architecture*

---

## 1. SYSTEM OVERVIEW

Sideio Lead Sniper is a fully automated, multi-tenant B2B lead generation SaaS platform. It discovers, scores, and delivers hyper-personalized outreach messages for paying tenants — autonomously, 24/7, without manual input.

**Core loop:**
1. A Cloud Scheduler cron hits the Orchestrator every 5 minutes
2. The Orchestrator queues pipeline tasks per active campaign
3. Pipeline-Main runs AI-driven search → scrape → score → write to Firestore
4. The React frontend listens via `onSnapshot` and renders leads in real-time

---

## 2. REPOSITORY DIRECTORY TREE

```
/sideio_leads
├── /public                          # Firebase Static Hosting (PWA)
│   ├── index.html                   # DOM scaffolding, Firebase SDK init, Auth UI
│   ├── app.js                       # All frontend logic (1100+ lines)
│   ├── styles.css                   # CSS design system
│   ├── sw.js                        # Service Worker (cache v10-3, Firebase bypass)
│   └── manifest.json                # PWA manifest
├── /services
│   ├── /orchestrator                # Cloud Run: REST API Gateway + Cron Dispatcher
│   │   ├── Dockerfile
│   │   ├── main.py                  # 624 lines — all API routes + cron sweep
│   │   └── requirements.txt
│   ├── /pipeline-main               # Cloud Run: AI Extraction Engine
│   │   ├── Dockerfile
│   │   ├── main.py                  # 806 lines — search, scrape, score, write
│   │   └── requirements.txt
│   └── /scraper-heavy               # Cloud Run: Playwright Headless Browser
│       ├── Dockerfile
│       ├── main.py                  # 171 lines — async Chromium + proxy tiers
│       └── requirements.txt
├── /terraform                       # GCP infrastructure as code
├── .firebaserc                      # Firebase project binding (lead-sniper-prod)
├── firebase.json                    # Hosting config + Firestore rules pointer
├── firestore.rules                  # V13.22 multi-tenant security rules
├── firestore.indexes.json           # Composite index: tenant_id ASC + timestamp DESC
├── cloudbuild.yaml                  # CI/CD: builds & deploys all 5 microservices
└── architecture.md                  # This document
```

---

## 3. GCP INFRASTRUCTURE & SERVICE TOPOLOGY

| Service | Cloud Run Name | Auth | Memory | Region |
|---|---|---|---|---|
| Orchestrator | `orchestrator` | `--allow-unauthenticated` | 256 Mi | asia-south1 |
| Pipeline Main | `lead-pipeline-main` | `--no-allow-unauthenticated` | 512 Mi | asia-south1 |
| Scraper Heavy | `scraper-heavy` | `--no-allow-unauthenticated` | 2 Gi | asia-south1 |
| WhatsApp Webhook | `whatsapp-webhook` | `--allow-unauthenticated` | 128 Mi | asia-south1 |
| Email Summary | `email-summary` | `--no-allow-unauthenticated` | 128 Mi | asia-south1 |
| Frontend | Firebase Hosting | Public CDN | — | Global |

**GCP Project ID:** `sideio-leads-v16`  
**Firebase Project:** `lead-sniper-prod`  
**Cloud Tasks Queue:** `lead-pipeline-queue` (region: asia-south1)  
**Vertex AI Cluster:** `us-central1` (gemini-2.5-flash)

### Environment Variables (per service)
```
# Orchestrator & Pipeline-Main (shared)
PROJECT_ID=sideio-leads-v16
LOCATION=asia-south1
QUEUE=lead-pipeline-queue
PIPELINE_URL=https://lead-pipeline-main-222247989819.asia-south1.run.app/dispatch
ENCRYPTION_KEY=<fernet_key>          # Fallback symmetric cipher

# Pipeline-Main extras
SCRAPER_HEAVY_URL=https://scraper-heavy-<hash>.a.run.app/scrape
PIPELINE_BASE_URL=https://lead-pipeline-main-<hash>.a.run.app

# Scraper-Heavy extras
PIPELINE_BASE_URL=https://lead-pipeline-main-<hash>.a.run.app
```

### Secret Manager Secrets (GCP)
| Secret Name | Used By | Purpose |
|---|---|---|
| `serper_api_key` | pipeline-main | Serper.dev search API key |
| `FIREBASE_SA_KEY` | Cloud Build | Firebase deploy service account JSON |
| `kms_wa_key_path` | orchestrator, pipeline-main | KMS key ring path for WhatsApp token |
| `DECODO_STANDARD_PROXY` | scraper-heavy | Standard rotating proxy URL |
| `DECODO_PREMIUM_PROXY` | scraper-heavy | Premium WAF-bypass proxy URL |

---

## 4. FIRESTORE DATABASE SCHEMA

### 4.1 `users` Collection
Primary tenant anchor. Document ID = Firebase Auth UID.

```json
{
  "_id": "uid_from_firebase_auth",
  "email": "user@example.com",
  "role": "admin",
  "tenant_id": "uid_from_firebase_auth",
  "is_active": true,
  "approval_status": "pending",
  "beta_expiry": "2026-10-01T00:00:00Z",
  "agreed_to_terms": "<SERVER_TIMESTAMP>",
  "crm_webhook_url": "https://hooks.zapier.com/hooks/catch/...",
  "wa_token": "gAAAAAB...",
  "wa_phone_id": "123456789",
  "admin_phone": "13125550199",
  "wallet": {
    "allocated_credits": 20000,
    "consumed_credits": 314
  },
  "preferences_weights": {
    "hiring_intent": 2,
    "tech_wordpress": -5,
    "tech_react": 1
  },
  "dynamic_blocklist": ["checkout", "add to cart"],
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

**Notes:**
- `role`: `"admin"` (default) or `"super_admin"` (grants L0 dashboard + quota bypass)
- `approval_status`: `"pending"` blocks all pipeline execution; must be set to `"approved"` by L0
- `wallet.consumed_credits`: Base value only. True consumed total = `consumed_credits + SUM(wallet_shards/0-9)`
- `wa_token`: Encrypted via Google Cloud KMS (primary) or Fernet (fallback legacy)

### 4.2 `users/{tenant_id}/wallet_shards/{0-9}` Sub-Collection
Distributed credit counters to bypass Firestore write-contention limits.

```json
{ "consumed_credits": 42 }
```
Each pipeline execution picks a random shard (0–9) and increments by 1.

### 4.3 `users/{tenant_id}/usage_metrics/shards/{0-9}` Sub-Collection
Gemini call tracking shards.
```json
{ "gemini_calls": 17 }
```

### 4.4 `campaigns` Collection

```json
{
  "_id": "auto_generated_firestore_id",
  "tenant_id": "uid_from_firebase_auth",
  "name": "Q3 Commercial Cleaning Push",
  "bio": "We offer B2B janitorial services for offices.",
  "status": "active",
  "keywords": "facility management, office cleaning",
  "location": "Austin, TX",
  "gl": "us",
  "target_urls": ["https://specific-target.com"],
  "leads_generated": 105,
  "next_drip_due": "<TIMESTAMP>",
  "drip_interval_minutes": 60,
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

**Notes:**
- `keywords`: Stored as comma-separated string, parsed to array in pipeline
- `target_urls`: Up to 10 manually specified URLs, bypasses Serper search
- `next_drip_due`: Set by cron sweep after each dispatch. Controls per-campaign drip rate
- `drip_interval_minutes`: Defaults to 60. Controls time between pipeline runs for this campaign

### 4.5 `leads` Collection
Core atomic lead document. Document ID is a deterministic SHA-256 hash.

```json
{
  "_id": "sha256(tenant_id + '_' + root_domain)",
  "tenant_id": "uid_from_firebase_auth",
  "matched_campaigns": ["camp_uuid_789", "camp_uuid_101"],
  "url": "https://techcorp.com",
  "status": "new",
  "score": 8,
  "pain_point": "Complaining about high turnover on LinkedIn.",
  "icebreaker_angle": "Focus on facility hygiene boosting employee retention.",
  "dm": "Hey [Name], noticed...",
  "hiring_intent_found": "Yes",
  "tech_stack_found": ["react", "hubspot"],
  "decision_maker_name": "John Doe",
  "decision_maker_title": "VP of Operations",
  "company_size_tier": "Mid-Market",
  "primary_objection_hypothesis": "They might lack budget for external enterprise tooling.",
  "email": "hr@techcorp.com",
  "phone": "3125550199",
  "linkedin": "https://linkedin.com/in/...",
  "error": null,
  "interactions": [
    { "action": "status_ignored", "date": "<SERVER_TIMESTAMP>" }
  ],
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

**Status Enum:** `processing` → `new` → `contacted` → `converted` | `ignored` | `failed`  
**Score threshold:** Only leads scoring `>= 7` are written as `"new"`. Scores below 7 delete the document.  
**Deduplication key:** `sha256(tenant_id + '_' + root_domain)` — same domain can never appear twice for the same tenant.

### 4.6 `global_lead_locks` Collection
Cross-tenant exclusivity lock. Prevents two tenants from being assigned the same lead.

```json
{
  "_id": "sha256(exact_path_or_root_domain)",
  "locked_until": "<TIMESTAMP +14 days>"
}
```

### 4.7 `scraped_cache` Collection
Caches Playwright scrape results for 30 days. Prevents re-scraping same URLs.

```json
{
  "_id": "url_with_slashes_replaced_by_underscores",
  "url": "https://techcorp.com",
  "text": "<truncated DOM text, max 100KB>",
  "tech_stack": ["wordpress"],
  "emails": ["contact@techcorp.com"],
  "phones": ["+13125550199"],
  "expireAt": "<TIMESTAMP +30 days>"
}
```

### 4.8 `usage_metrics` Collection
Per-tenant API cost telemetry (Serper + Gemini call counts).

```json
{
  "_id": "tenant_id",
  "serper_searches": 142
}
```

---

## 5. FIRESTORE SECURITY RULES (V13.22)

```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {

    match /leads/{document} {
      allow read:          if request.auth != null && resource.data.tenant_id == request.auth.uid;
      allow create:        if request.auth != null && request.resource.data.tenant_id == request.auth.uid;
      allow update, delete: if request.auth != null && resource.data.tenant_id == request.auth.uid;
    }

    match /campaigns/{document} {
      allow read:          if request.auth != null && resource.data.tenant_id == request.auth.uid;
      allow create:        if request.auth != null && request.resource.data.tenant_id == request.auth.uid;
      allow update, delete: if request.auth != null && resource.data.tenant_id == request.auth.uid;
    }

    match /{document=**} {
      allow read, write: if false; // Deny all other collections to frontend
    }
  }
}
```

**Critical Design Note:** All other Firestore collections (`users`, `global_lead_locks`, `scraped_cache`, etc.) are **only accessible via the Firebase Admin SDK** inside backend services, which bypasses security rules entirely.

### Firestore Composite Index
```json
{
  "collectionGroup": "leads",
  "fields": [
    { "fieldPath": "tenant_id", "order": "ASCENDING" },
    { "fieldPath": "timestamp",  "order": "DESCENDING" }
  ]
}
```

---

## 6. THE 10-STEP PIPELINE EXECUTION FLOW

### Step 1: Cloud Scheduler Cron Trigger
- **Schedule:** Every 5 minutes
- **Target:** `POST /api/internal/cron/sweep` on the Orchestrator Cloud Run service
- **Auth:** OIDC token verified via `google.oauth2.id_token.verify_oauth2_token()`
- The Orchestrator queries all `campaigns` where `status == "active"`, limit 500

### Step 2: Per-Campaign Drip Rate Check
**Location:** `orchestrator/main.py::trigger_daily_sweep`

```python
next_drip_due = campaign_data.get("next_drip_due")
if next_drip_due and next_drip_due > now_utc:
    continue  # Campaign not due yet. Skip.
```
After queuing, `next_drip_due` is updated to `now + drip_interval_minutes` (default: 60 min).

### Step 3: Quota & Wallet Validation
**Location:** `orchestrator/main.py::check_quota`

For each campaign's tenant:
1. Skip if `role == "super_admin"` (unlimited)
2. Check `approval_status == "approved"`, else return 403
3. Calculate true balance: `allocated_credits - consumed_credits - SUM(wallet_shards)`
4. If balance <= 0: skip campaign, append to audit trail

### Step 4: Cloud Task Dispatch with Jitter
**Location:** `orchestrator/main.py::trigger_daily_sweep`

```python
jitter_seconds = random.randint(1, 290)  # Stagger over 5-minute window
task = {
    "http_request": {
        "http_method": POST,
        "url": PIPELINE_URL,  # /dispatch endpoint
        "body": json.dumps({"tenant_id": ..., "campaign_id": ...}).encode(),
        "oidc_token": {"service_account_email": sa_email, "audience": base_url}
    },
    "schedule_time": now + jitter_seconds
}
tasks_client.create_task(request={"parent": queue_path, "task": task})
```

OIDC token is dynamically fetched from GCP metadata server with exponential backoff (3 attempts).

### Step 5: Smart Query Generation (RLHF-Enhanced)
**Location:** `pipeline-main/main.py::generate_smart_query`

Two parallel generation strategies are merged:

**A. Historical Mining (RLHF):**
```python
query = db.collection("leads")
  .where("tenant_id", "==", tenant_id)
  .where("status", "in", ["contacted", "converted"])
  .limit(20)
# Falls back to global leads if no tenant history
pain_points = [d.get("pain_point") for d in docs]
prompt = "Analyze these successful lead extractions. Extract exactly 3 short B2B phrases identifying high-value trends..."
historical_phrases = call_gemini_2_5(prompt, expect_json=False).split(',')
```

**B. Symptom Dorking (Bio-Driven):**
```python
symptom_prompt = f"""The user solves this business problem: '{bio}'.
Generate 3 highly specific Google Search operators to find targets PUBLICLY EXPERIENCING this problem.
Rule 1: MUST include at least one query targeting site:linkedin.com, site:facebook.com, or site:reddit.com.
Rule 2: MUST append negative keywords (e.g., '-shop -cart -amazon -wiki').
Return ONLY a JSON list of 3 strings."""
symptom_dorks = call_gemini_2_5(symptom_prompt, expect_json=True)
```

Final queries = keyword queries + symptom dorks + global blacklist suffixed to each:
```python
blacklist = "-wiki -jobs -careers -investors -support -\"login\" -www.zoominfo.com -www.ibm.com -www.amazon.com"
```

### Step 6: Serper Search Execution
**Location:** `pipeline-main/main.py::search_serper`

```python
payload = {"q": f"{query} AND {location}", "num": 20, "location": location, "gl": country_code}
response = httpx.post("https://google.serper.dev/search", headers={"X-API-KEY": key}, data=payload)
results = response.json().get("organic", [])
```

Post-flight noise filter (`filter_serper_noise`) removes:
- Known enterprise/aggregator domains: `ibm.com`, `amazon.com`, `g2.com`, `capterra.com`, `zoominfo.com`
- Noise URL paths: `/legal`, `/pricing`, `/docs`, `/author/`, `/login`
- Noise snippets: `"sign in"`, `"access denied"`, `"please enable cookies"`

### Step 7: Gemini B2B Intent & Geo Gate
**Location:** `pipeline-main/main.py::pre_filter_gemini`

All deduplicated Serper snippets pass through a Gemini LLM gate before any scraping begins:

```
CRITICAL INTENT CHECK: Is the website EXPERIENCING the problem the user solves, or SELLING a solution?
- Reject: SEO blogs, competitors, D2C retail, business directories (JustDial, Alibaba, Yelp, IndiaMart)
- Reject: Manufacturers/wholesalers who sell what the user sells
- Social Platform Rule: Evaluate the SPECIFIC POST intent, not the platform itself
- Geo Rule: If target is '{location}' and site explicitly serves a different region → REJECT
Output: Line-by-line list of approved URLs only, each starting with 'http'
```

### Step 8: Global Exclusivity Lock + Deduplication
**Location:** `pipeline-main/main.py` — inner URL processing loop

**Exclusivity Gate:**
```python
lock_ref = db.collection("global_lead_locks").document(lock_entity)
lock_doc = lock_ref.get()
if lock_doc.exists and lock_doc.to_dict().get("locked_until") > now_utc:
    continue  # Domain locked by another tenant for 14 days
lock_ref.set({"locked_until": now_utc + timedelta(days=14)})
```

**Tenant Deduplication:**
```python
lead_id = hashlib.sha256(f"{tenant_id}_{root_domain}".encode()).hexdigest()
doc_ref.create({"status": "processing", ...})  # Raises AlreadyExists if duplicate
# On AlreadyExists: just append campaign to matched_campaigns array, continue
```

### Step 9: Scraping — Three-Tier Strategy
**Location:** `pipeline-main/main.py::scrape_url` + `scraper-heavy/main.py`

**Tier 1 — Social Short-Circuit (Free):**
If `target_domain` ends with any of: `linkedin.com, facebook.com, reddit.com, instagram.com, x.com, twitter.com, quora.com, youtube.com, team-bhp.com` → skip scraping entirely, use the Serper snippet directly as the text blob.

**Tier 2 — Lightweight httpx Scraper:**
`pipeline-main::scrape_url` does a synchronous `httpx.get(url, timeout=10)`.
- Parses with BeautifulSoup
- Detects WAF blocks via title/body fingerprints (`"just a moment..."`, `"cloudflare"`, etc.)
- Runs Tech Stack X-Ray by scanning raw HTML for signature strings
- Extracts `mailto:` and `tel:` links from DOM
- If content < 500 chars → raises `ValueError("DEFERRED")` → escalates to Tier 3

**Tier 3 — Playwright Heavy Scraper (DEFERRED):**
Pipeline-main queues a Cloud Task to `scraper-heavy/scrape`. The scraper-heavy container:
1. Loads `DECODO_STANDARD_PROXY` from Secret Manager
2. Launches headless Chromium with `--disable-dev-shm-usage --single-process --no-sandbox --no-zygote`
3. Aborts all `image, media, font, stylesheet` resource types to prevent OOM
4. Sets a hard 20-second `asyncio.wait_for()` kill switch
5. Detects WAF (403/429/503 HTTP or CloudFlare DOM keywords)
6. If WAF detected → re-launches with `DECODO_PREMIUM_PROXY` (high-cost bypass)
7. Strips `script, style, noscript, nav, footer, iframe` from DOM
8. Harvests `mailto:` and `tel:` links via JavaScript evaluate
9. Queues a Cloud Task back to `pipeline-main/finalize` with full payload

### Step 10: RLHF Pre-Screen + Vertex AI Scoring
**Location:** `pipeline-main/main.py::finalize` and `final_score_and_dm`

**A. Python Fast-Fail Gate (Cost Guard):**
```python
global_b2b_blocklist = ['add to cart', 'shopping bag', 'checkout', 'shipping policy', ...]
dynamic_blocklist = tenant_doc.get("dynamic_blocklist", [])  # User's learned blocklist
fail_score = sum(text.lower().count(term) for term in (global_b2b_blocklist + dynamic_blocklist))
if fail_score > 3:
    doc_ref.update({"status": "failed", "error": "Dropped by Python Heuristics (Cost Saved)"})
```

**B. Token Reduction — Density Extraction:**
```python
def extract_dense_payload(text, bio):
    # Scores paragraphs by keyword overlap with bio
    # Returns top 10 most relevant paragraphs only
    # Reduces Vertex AI token consumption by ~80%
```

**C. Multi-Vector Serper Dorking (Context Enrichment):**
```python
def deep_context_serper_dork(domain, tenant_id):
    # Vector A: Google My Business (rating, reviews, address)
    # Vector B: LinkedIn/Facebook company social presence
    # Vector C: Hiring intent (Naukri, Instahyre, LinkedIn Jobs, Indeed)
    # Returns: context_payload string + native_hiring_intent boolean
```

**D. RLHF Python Interceptor:**
```python
fit_score = 0
if native_hiring_intent:
    fit_score += preferences_weights.get("hiring_intent", 0)
for tech in tech_stack:
    fit_score += preferences_weights.get(f"tech_{tech}", 0)
if fit_score <= -3:
    doc_ref.delete()  # Drop before calling Vertex AI — saves 1 token sequence
    continue
```

**E. Few-Shot Conversion Context Injection:**
```python
docs = db.collection("leads")
  .where("tenant_id", "==", tenant_id)
  .where("status", "==", "converted")
  .order_by("updatedAt", DESCENDING)
  .limit(3)
historical_dms = [doc.get("dm") for doc in docs]
# Injected into final_score_and_dm prompt: "Match this tone strictly"
```

**F. Vertex AI Final Scoring (gemini-2.5-flash):**

System instruction:
```
You are an Elite B2B Profiler. Extract factual enterprise data and draft concise, highly-converting outreach messages. Be ruthless, analytical, and highly specific.
```

The response is locked to a strict JSON schema via `GenerationConfig(response_mime_type="application/json", response_schema=schema)`:

```python
schema = {
  "type": "OBJECT",
  "properties": {
    "score":                       {"type": "INTEGER"},
    "dm":                          {"type": "STRING"},   # "N/A" if no valid prospect
    "pain_point":                  {"type": "STRING"},   # "N/A" if insufficient data
    "icebreaker_angle":            {"type": "STRING"},   # "N/A" if insufficient data
    "hiring_intent_found":         {"type": "STRING", "enum": ["Yes", "No"]},
    "tech_stack_found":            {"type": "ARRAY",  "items": {"type": "STRING"}},
    "whatsapp_draft":              {"type": "STRING"},
    "email":                       {"type": "STRING"},
    "phone":                       {"type": "STRING"},
    "linkedin":                    {"type": "STRING"},
    "decision_maker_name":         {"type": "STRING"},
    "decision_maker_title":        {"type": "STRING"},
    "company_size_tier":           {"type": "STRING"},   # Enum: Startup|Mid-Market|Enterprise|Unknown
    "primary_objection_hypothesis":{"type": "STRING"}
  },
  "required": ["score", "dm", "pain_point", "icebreaker_angle", "hiring_intent_found",
               "tech_stack_found", "decision_maker_name", "decision_maker_title",
               "company_size_tier", "primary_objection_hypothesis"]
}
```

Vertex AI is called via `call_gemini_2_5()` which wraps invocation in:
- `tenacity` retry: `wait_exponential(min=2, max=10)`, `stop_after_attempt(5)`, triggered on `ResourceExhausted`
- `concurrent.futures.ThreadPoolExecutor` with a hard 45-second `future.result(timeout=45.0)` kill switch

**Score gate:** Only leads scoring `>= 7` are written with `status: "new"`. Leads scoring below 7 are **deleted** from Firestore immediately.

---

## 7. RLHF SELF-LEARNING SYSTEM

The platform is self-optimizing using zero-cost database reads.

### 7.1 UI Action → Backpropagation (Orchestrator)
When the user clicks `Ignore` or `Converted` on a lead card:

```python
# orchestrator/main.py — PUT /api/leads/{id}
delta = 1 if status == "converted" else -1

# Hiring intent weight
pref_updates["preferences_weights.hiring_intent"] = firestore.Increment(delta)

# Per-tech weight
for tech in tech_stack:
    pref_updates[f"preferences_weights.tech_{tech}"] = firestore.Increment(delta)

# Ignored leads → populate dynamic blocklist
if status == "ignored":
    words = re.findall(r'\b\w{4,}\b', pain_point.lower())[:3]
    pref_updates["dynamic_blocklist"] = firestore.ArrayUnion(words + tech_stack[:2])

user_ref.set(pref_updates, merge=True)
```

### 7.2 Function Map: RLHF Interceptor (Pipeline)
Before hitting Vertex AI, the pipeline pre-screens each target using the tenant's learned weights:
```python
# If fit_score <= -3: delete lead doc, skip Vertex call entirely
```
This prevents Vertex credit spend on leads the tenant historically dislikes.

### 7.3 Function Map: Historical Query Mining
`generate_smart_query()` analyzes the tenant's last 20 successful leads to extract B2B trend phrases and injects them directly into the Serper search queries.

### 7.4 Function Map: Few-Shot DM Injection
`finalize()` fetches the last 3 `"converted"` leads' DMs and injects them into the Vertex prompt to enforce proven phrasing style.

---

## 8. ORCHESTRATOR REST API REFERENCE

**Base URL:** `https://orchestrator-222247989819.asia-south1.run.app`  
**Auth:** All user-facing endpoints require `Authorization: Bearer <Firebase ID Token>`  
**CORS:** Strict allowlist — only `lead-sniper-prod.web.app` and `lead-sniper-prod.firebaseapp.com`

### 8.1 Authentication Flow (`authenticate_request`)
Every protected route calls this function first:
1. Extracts Bearer token from `Authorization` header
2. Calls `firebase_admin.auth.verify_id_token(token)` — validates cryptographic signature
3. Looks up `users/{uid}` in Firestore
4. If user doesn't exist → auto-creates with `approval_status: "pending"`, `wallet: {0, 0}`
5. If `is_active == false` and not `super_admin` → raises `ValueError` → 401 returned
6. Returns `(uid, tenant_id, user_role)` tuple

### 8.2 Endpoint Reference

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/me` | User | Returns user profile + wallet balance (shard-aggregated) |
| PUT | `/api/me` | User | Update `agreed_to_terms` or `crm_webhook_url` |
| GET | `/api/campaigns` | User | List all tenant campaigns (limit 100) |
| POST | `/api/campaigns` | User | Create new campaign (runs quota check first) |
| PUT | `/api/campaigns/{id}` | User | Update campaign (tenant ownership enforced) |
| GET | `/api/leads` | User | List all tenant leads (limit 100) — legacy polling fallback |
| PUT | `/api/leads/{id}` | User | Update lead status + trigger RLHF backprop |
| POST | `/api/settings` | User | Save WhatsApp credentials (KMS encrypted) |
| GET | `/api/l0/telemetry` | super_admin | Global macro lead counts + all tenant summaries |
| GET | `/api/l0/trends` | super_admin | Active campaigns ranked by leads generated |
| GET | `/api/l0/users` | super_admin | All user profiles with usage metrics |
| POST | `/api/l0/users/suspend` | super_admin | Toggle `is_active` for any tenant |
| POST | `/api/l0/users/{id}/mint` | super_admin | Add credits to tenant wallet |
| POST | `/api/l0/users/{id}/approve` | super_admin | Set `approval_status: approved` + mint credits + set expiry |
| POST | `/api/internal/cron/sweep` | OIDC only | Master cron: dispatch pipeline tasks per active campaign |
| POST | `/purge` | Internal | DPDP compliance: erase all data for a tenant |

### 8.3 L0 Wallet: Credit Minting
```python
# POST /api/l0/users/{tenant_id}/approve
payload = {"amount": 20000, "days": 180}
# Sets approval_status: "approved"
# Adds {amount} to wallet.allocated_credits via firestore.Increment
# Sets beta_expiry to now + {days}
```

### 8.4 Lead Status Update + RLHF
```python
# PUT /api/leads/{id}
# Body: {"status": "converted"} or {"status": "ignored"}
# 1. Verifies doc.tenant_id == authenticated tenant_id
# 2. Updates status + updatedAt
# 3. Executes RLHF backpropagation (Section 7.1)
```

---

## 9. FRONTEND ARCHITECTURE (public/)

### 9.1 Technology Stack
- **Runtime:** Vanilla JavaScript (no build step, no framework)
- **Auth:** Firebase SDK v8 compat (`firebase.auth()`)
- **Database:** Firebase SDK v8 compat (`firebase.firestore()`) — direct `onSnapshot`
- **Charts:** Chart.js (Doughnut funnel)
- **PWA:** Service Worker + `manifest.json`
- **Hosting:** Firebase Hosting (CDN-distributed)

### 9.2 Firebase Config
```javascript
const firebaseConfig = {
    apiKey: "AIzaSyCxqimZJ7kspuJJ8qXF34zguLkNXi6MWd4",
    authDomain: "lead-sniper-prod.firebaseapp.com",
    projectId: "lead-sniper-prod",
    storageBucket: "lead-sniper-prod.firebasestorage.app",
    messagingSenderId: "222247989819",
    appId: "1:222247989819:web:17066a1bbf0b1f3df2221e",
    measurementId: "G-SQ6DDQ7HW0"
};
const API_BASE = "https://orchestrator-222247989819.asia-south1.run.app";
```

### 9.3 Authentication Flow
```javascript
// Login: Google OAuth popup
auth.signInWithPopup(new firebase.auth.GoogleAuthProvider())

// State observer (app entry point)
auth.onAuthStateChanged(async user => {
    if (user) {
        authContainer.classList.add('hidden');
        appContainer.classList.remove('hidden');
        loadDashboard(); // Loads me + campaigns + leads
    }
});
```

### 9.4 Real-Time Lead Feed (`onSnapshot`)
```javascript
// Called inside loadLeads(), which is guarded by onAuthStateChanged
unsubscribeLeads = firebase.firestore()
    .collection('leads')
    .where('tenant_id', '==', user.uid)
    .onSnapshot((snapshot) => {
        rawLeadsCache = [];
        snapshot.forEach(doc => {
            let data = doc.data();
            data.id = doc.id;
            rawLeadsCache.push(data);
        });
        rawLeadsCache.sort((a, b) => (b.score || 0) - (a.score || 0));
        // Update stat counters (discovered, actionable, ignored)
        // Update Chart.js doughnut
        renderLeads(); // DOM virtualization render
    }, error => {
        if (error.code === 'permission-denied') console.warn("Firestore rules block.");
    });
```
Previous listener is unsubscribed before re-attaching to prevent duplicate listeners.

### 9.5 DOM Virtualization (Virtual Observer)
Prevents FPS drops on large lead arrays. Only viewport-visible leads are hydrated.

```javascript
let virtualObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting && !entry.target.hasAttribute('data-rendered')) {
            const leadId = entry.target.getAttribute('data-lead-id');
            const lead = rawLeadsCache.find(l => (l.id || l.doc_id) === leadId);
            if (lead) {
                entry.target.innerHTML = generateLeadInnerHtml(leadId, lead); // renders HTML string
                entry.target.setAttribute('data-rendered', 'true');
                entry.target.style.height = 'auto';
            }
        } else if (!entry.isIntersecting && entry.target.hasAttribute('data-rendered')) {
            // Preserve height to maintain scroll position
            entry.target.style.height = `${Math.max(150, rect.height)}px`;
            entry.target.innerHTML = '';
            entry.target.removeAttribute('data-rendered');
        }
    });
}, { rootMargin: "800px" }); // Pre-load 800px before viewport
```

### 9.6 `generateLeadInnerHtml(docId, lead)` — Enterprise Dossier Renderer
Defined above the `virtualObserver`. Returns an HTML **string**. Called by the observer on every scroll event for newly visible lead wrappers.

**Phase 5 Backward-Compatible Field Parsing:**
```javascript
// Intercepts both undefined AND literal "N/A" strings
const targetName = (!lead.decision_maker_name || lead.decision_maker_name === 'N/A')
    ? 'Data unavailable on scanned domain'
    : lead.decision_maker_name;

const companySize = (!lead.company_size_tier || lead.company_size_tier === 'N/A')
    ? 'Requires secondary analysis'
    : lead.company_size_tier;

const primaryObjection = (!lead.primary_objection_hypothesis || lead.primary_objection_hypothesis === 'N/A')
    ? 'Insufficient data to generate confident hypothesis'
    : lead.primary_objection_hypothesis;
```

**Rendered Elements (in order):**
1. `lead-header` — URL link + source + status badge + score
2. `pain-point` — AI-extracted pain point quote
3. `premium-badges` — Exclusive Lead + Competitor Intercept + 🟢 Hiring + 👤 Decision Maker + 🏢 Company Size + ⚡ Tech Stack
4. `icebreaker-row` — Purple left-border callout (conditional on `icebreaker_angle`)
5. `objection-row` — Amber left-border warning (conditional on `primary_objection_hypothesis`)
6. `dm-draft` — AI-drafted message
7. `contact-info` — mailto + tel links
8. `action-row` — 📋 Copy Message | ☁️ Push to CRM | 🚫 Ignore | 🎯 Converted | 🕒 Timeline

### 9.7 Tech Stack Badge Dictionary
Displayed on each lead card when found in `tech_stack_found`:
```javascript
const techDict = {
    'stripe':           'Takes Online Payments',
    'wordpress':        'Active Content/Blog',
    'shopify':          'E-Commerce Store',
    'salesforce':       'Enterprise CRM',
    'hubspot':          'Marketing Automation',
    'google analytics': 'Tracks Analytics',
    'segment':          'Customer Data Platform',
    'intercom':         'Live Chat Support',
    'react':            'Modern Web App'
};
```

### 9.8 API-Backed Mutations (No Direct Firestore Writes from Frontend)
All data mutations from the frontend go through the Orchestrator REST API:
```javascript
async function performApiMutation(url, method, payload) {
    const token = await firebase.auth().currentUser.getIdToken();
    const response = await fetch(`${API_BASE}${url}`, {
        method,
        headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    if (response.status === 401 || response.status === 403) return handleAuthRejection();
    if (!response.ok) throw new Error("API Execution Failed");
    return true;
}
```

### 9.9 Optimistic UI — Copy Message Action
When `📋 Copy Message` is clicked:
1. Instantly removes lead card from `rawLeadsCache` and DOM (optimistic)
2. Unobserves from IntersectionObserver
3. Fires background `PUT /api/leads/{id}` with `status: "contacted"`
4. Backend RLHF backprop executes asynchronously

### 9.10 CRM Webhook Push
```javascript
await fetch(crm_webhook_url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    mode: 'no-cors',  // Bypass CORS for Zapier/Webhook.site
    body: JSON.stringify({ event: 'lead_pushed', lead: lead })
});
```

### 9.11 Approval Gate (Wait Room)
If `loadMe()` returns `data.approval_status === 'pending'`:
- Hides `.dashboard-grid` and `.glass-nav`
- Shows `#waitroom-overlay` fullscreen

### 9.12 Wallet Alert System
- `credits <= 0` → Red banner + disables "Find New Clients" button
- `credits < 50` → Warning banner visible
- `credits >= 50` → Banner hidden

### 9.13 L0 Super Admin Dashboard
Only rendered when `data.role === 'super_admin'`:
- Tab `#tab-l0-admin` becomes visible
- Calls `GET /api/l0/telemetry` — returns macro totals + per-tenant summaries
- 30-second memory debounce on Refresh button: `if (now - lastL0FetchTime < 30000) return`
- In-memory sort: `window.sortL0Table('email' | 'wallet' | 'leads')` — sorts JSON array in client RAM, no DB calls
- `GET /api/l0/trends` — active campaigns ranked by leads generated
- Actions: Approve, Mint Credits, Suspend tenant

---

## 10. SERVICE WORKER (public/sw.js)
**Cache Version:** `sideio-v10-3`

### Strategy
```javascript
// Install: cache static assets (/, index.html, app.js, styles.css, manifest.json)
self.addEventListener('install', event => {
    self.skipWaiting(); // Force immediate activation
    event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS_TO_CACHE)));
});

// Activate: delete all old cache versions
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(names =>
            Promise.all(names.map(n => n !== CACHE_NAME && caches.delete(n)))
        ).then(() => self.clients.claim())
    );
});
```

### Critical Firebase Bypass (V10-3 Fix)
```javascript
self.addEventListener('fetch', event => {
    // MUST be at the very top of the fetch handler
    const url = new URL(event.request.url);
    if (
        url.hostname.includes('googleapis.com') ||
        url.hostname.includes('google.com')     ||
        url.hostname.includes('firestore')
    ) {
        event.respondWith(fetch(event.request));
        return;  // Never cache Firestore WebChannel streams
    }
    // All other requests: network-first, cache fallback
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
```

**Why this is critical:** Firestore `onSnapshot` uses long-poll HTTP requests (WebChannel) that return opaque, non-cloneable response bodies. If the SW intercepts them, it throws `"Failed to convert value to 'Response'"` and causes 30-second disconnect loops. The bypass was added in v10-3.

**To force SW update on all clients:** Bump `CACHE_NAME` version (e.g., `sideio-v10-4`). On next page load, the browser detects the changed SW, installs it, and `skipWaiting()` activates it immediately.

---

## 11. CI/CD PIPELINE (cloudbuild.yaml)

Triggered automatically on every push to `main` branch of `ShibuSideio/Leadsniper`.

### Build Steps (in order)
```
1. docker build gcr.io/$PROJECT_ID/lead-orchestrator ./services/orchestrator
2. docker push gcr.io/$PROJECT_ID/lead-orchestrator
3. gcloud run deploy orchestrator (256Mi, --allow-unauthenticated, asia-south1)

4. docker build gcr.io/$PROJECT_ID/lead-pipeline-main ./services/pipeline-main
5. docker push gcr.io/$PROJECT_ID/lead-pipeline-main
6. gcloud run deploy lead-pipeline-main (512Mi, --no-allow-unauthenticated, SA: lead-pipeline-sa)

7. docker build gcr.io/$PROJECT_ID/scraper-heavy ./services/scraper-heavy
8. docker push gcr.io/$PROJECT_ID/scraper-heavy
9. gcloud run deploy scraper-heavy (2Gi, --min-instances 0, SA: scraper-heavy-sa)

10. docker build gcr.io/$PROJECT_ID/whatsapp-webhook ./services/whatsapp-webhook
11. docker push gcr.io/$PROJECT_ID/whatsapp-webhook
12. gcloud run deploy whatsapp-webhook (128Mi, --allow-unauthenticated, SA: whatsapp-webhook-sa)

13. docker build gcr.io/$PROJECT_ID/email-summary ./services/email-summary
14. docker push gcr.io/$PROJECT_ID/email-summary
15. gcloud run deploy email-summary (128Mi, --no-allow-unauthenticated, SA: email-summary-sa)

16. node:20 container:
    npm install -g firebase-tools
    firebase deploy --project lead-sniper-prod --only hosting,firestore --non-interactive --force
```

### Secrets Used by Cloud Build
```
projects/lead-sniper-prod/secrets/FIREBASE_SA_KEY/versions/latest
→ Exported as env var FIREBASE_SA_KEY
→ Written to /workspace/secure-key.json
→ Used as GOOGLE_APPLICATION_CREDENTIALS for firebase deploy
```

### Logging
`options: logging: CLOUD_LOGGING_ONLY` — all build logs go to Google Cloud Logging.

---

## 12. ERROR HANDLING & STATE RECOVERY

### 12.1 Playwright Timeout Kill Switch
```python
text, contacts = loop.run_until_complete(
    asyncio.wait_for(fetch_page_content(url), timeout=20.0)
)
# On timeout: returns ("", {}) — lead continues to finalize with empty text → deleted
```

### 12.2 Vertex AI Timeout Kill Switch
```python
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(_invoke_model)
    response = future.result(timeout=45.0)
# On TimeoutError: raises → caught upstream → lead marked "failed"
```

### 12.3 Vertex AI Rate Limit Retry (tenacity)
```python
@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type(ResourceExhausted)
)
def _invoke_model():
    return model.generate_content(prompt, generation_config=config)
```

### 12.4 Universal Loop Crash Handler
The entire per-URL processing block in `dispatch()` is wrapped in `try/except`:
```python
try:
    # cache check, scraping, RLHF, Vertex scoring, doc write
except Exception as loop_e:
    print(f"Pipeline execution crashed: {loop_e}")
    db.collection("leads").document(lead_id).update({
        "status": "failed",
        "error": "Pipeline execution crashed"
    })
    continue  # Never hangs in "processing" state
```

### 12.5 WAF Block Detection (Two Levels)
- **HTTP level:** `response.status in [403, 429, 503]` in Playwright
- **DOM level:** String match against: `"just a moment..."`, `"attention required"`, `"cf-browser-verification"`, `"ray id"`, `"cloudflare ray id"`, `"please verify you are human"`, `"access denied"`, `"403 forbidden"`

### 12.6 DPDP Data Erasure (`/purge`)
```python
# Deletes all campaigns, leads, scraped_cache entries, and tenant doc
# Called internally for compliance data erasure requests
```

---

## 13. KMS ENVELOPE ENCRYPTION (WhatsApp Tokens)

### Write Path (Orchestrator `/api/settings`)
```python
# Primary: Google Cloud KMS
kms_key_path = sm_client.access_secret_version("projects/.../secrets/kms_wa_key_path/versions/latest")
ciphertext = kms_client.encrypt({"name": kms_key_path, "plaintext": wa_token.encode()})
encrypted = base64.b64encode(ciphertext).decode()

# Fallback: Fernet symmetric (if KMS unavailable)
encrypted = Fernet(ENCRYPTION_KEY).encrypt(wa_token.encode()).decode()

db.collection("users").document(uid).update({"wa_token": encrypted})
```

### Read Path (pipeline-main `finalize()`)
```python
# Primary: KMS decrypt
ciphertext = base64.b64decode(wa_token_encrypted)
wa_token = kms_client.decrypt({"name": key_name, "ciphertext": ciphertext}).plaintext.decode()

# Fallback 1: Fernet
wa_token = cipher_suite.decrypt(wa_token_encrypted.encode()).decode()

# Fallback 2: Legacy plain text (no encryption)
wa_token = wa_token_encrypted
```

---

## 14. WHATSAPP META BUSINESS API (Hot Lead Alerts)

Triggered automatically when a lead scores `>= 8` (pipeline-main `dispatch()` and `finalize()`).

```python
wa_payload = {
    "messaging_product": "whatsapp",
    "to": admin_phone,       # From users/{tenant_id}.admin_phone
    "type": "interactive",
    "interactive": {
        "type": "button",
        "body": {
            "text": f"🔥 Hot Lead!\nCompany: {url}\nScore: {score}/10\nWhy: {pain_point}\nTech: {tech_stack}\nHiring: {hiring_intent}\n\nDrafted DM: {dm}"
        },
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": f"approve_{lead_id}", "title": "✅ Approve & Send"}},
                {"type": "reply", "reply": {"id": f"ignore_{lead_id}",  "title": "🚫 Ignore"}}
            ]
        }
    }
}
httpx.post(f"https://graph.facebook.com/v18.0/{wa_phone_id}/messages",
           json=wa_payload,
           headers={"Authorization": f"Bearer {wa_token}"},
           timeout=5)
```

Button replies are handled by the `whatsapp-webhook` service (separate Cloud Run container).

---

## 15. DEPENDENCY MANIFEST

### orchestrator/requirements.txt
```
google-cloud-firestore==2.14.0
google-cloud-tasks==2.15.0
Flask==3.0.0
gunicorn==21.2.0
firebase-admin>=6.5.0
cryptography==41.0.7
google-cloud-kms==2.21.3
google-cloud-secret-manager==2.16.2
google-auth>=2.22.0
```

### pipeline-main/requirements.txt
```
google-cloud-firestore>=2.14.0
google-cloud-secret-manager>=2.16.2
grpcio==1.60.0
google-cloud-aiplatform>=1.45.0
firebase-admin>=6.5.0
google-api-core>=2.17.1
Flask==3.0.0
gunicorn==21.2.0
httpx==0.26.0
beautifulsoup4==4.12.3
cryptography==41.0.7
tenacity==8.2.3
google-cloud-tasks>=2.14.2
```

### scraper-heavy/requirements.txt
```
playwright==1.42.0
Flask==3.0.0
gunicorn==21.2.0
google-cloud-tasks==2.14.2
google-cloud-secret-manager>=2.16.2
```
> After installing playwright, run: `playwright install chromium`

---

## 16. INTERN REBUILD CHECKLIST

To rebuild this system from scratch, follow this exact sequence:

### Phase 1: GCP Project Setup
- [ ] Create GCP project `sideio-leads-v16`
- [ ] Enable APIs: Cloud Run, Cloud Tasks, Firestore, Secret Manager, KMS, Cloud Build, Vertex AI
- [ ] Create service accounts: `lead-pipeline-sa`, `scraper-heavy-sa`, `whatsapp-webhook-sa`, `email-summary-sa`
- [ ] Create Cloud Tasks queue: `lead-pipeline-queue` in `asia-south1`
- [ ] Create Firestore database in Native mode
- [ ] Store secrets in Secret Manager: `serper_api_key`, `FIREBASE_SA_KEY`, `DECODO_STANDARD_PROXY`, `DECODO_PREMIUM_PROXY`, `kms_wa_key_path`

### Phase 2: Firebase Setup
- [ ] Create Firebase project `lead-sniper-prod` linked to GCP project
- [ ] Enable Google Sign-In in Firebase Auth
- [ ] Set Firebase Hosting public dir to `public/`
- [ ] Add `lead-sniper-prod.web.app` and `lead-sniper-prod.firebaseapp.com` to CORS allowlist in orchestrator
- [ ] Deploy `firestore.rules` and `firestore.indexes.json` via `firebase deploy --only firestore`

### Phase 3: Deploy Microservices (via Cloud Build)
- [ ] Connect `ShibuSideio/Leadsniper` GitHub repo to Cloud Build trigger on `main` branch
- [ ] Cloud Build auto-deploys all 5 services + Firebase hosting on every push
- [ ] Verify each Cloud Run service URL and update `PIPELINE_URL` env var in orchestrator

### Phase 4: Configure Cloud Scheduler
- [ ] Create job: `POST /api/internal/cron/sweep` every 5 minutes
- [ ] Use OIDC auth targeting the Orchestrator Cloud Run service URL
- [ ] Set service account to one with `roles/run.invoker` on the Orchestrator

### Phase 5: Onboard First Tenant
- [ ] User signs in via Google OAuth → user doc auto-created with `approval_status: "pending"`
- [ ] L0 admin calls `POST /api/l0/users/{uid}/approve` with `{"amount": 20000, "days": 180}`
- [ ] User creates a campaign with keywords + bio + location
- [ ] Wait for next cron sweep (max 5 minutes) — pipeline runs automatically

### Phase 6: Verify Data Flow
- [ ] Check Cloud Logging for `✅ QUEUED Campaign` messages in Orchestrator
- [ ] Check pipeline-main logs for `Gemini approved X URLs` messages
- [ ] Check Firestore `leads` collection for documents with `status: "new"`
- [ ] Verify frontend `onSnapshot` receives docs in real-time (no permission-denied errors)
- [ ] Check `global_lead_locks` collection for 14-day locks being written

### Key Design Invariants (Never Break These)
1. **Firestore rules**: `leads` and `campaigns` are the only collections the frontend can read/write directly
2. **Tenant isolation**: Every Firestore document that a tenant touches must have `tenant_id == user.uid`
3. **Lead dedup ID**: Always `sha256(tenant_id + '_' + root_domain)` — deterministic, not auto-generated
4. **Score gate**: Only leads `>= 7` are written as `"new"`. Everything below is deleted.
5. **SW Firebase bypass**: Never let the service worker intercept `googleapis.com` or `google.com` traffic
6. **Wallet shards**: True balance = `allocated_credits - consumed_credits - SUM(wallet_shards/0-9)`
7. **OIDC for cron**: `/api/internal/cron/sweep` validates `id_token` via `google.oauth2.id_token` — never use Firebase ID tokens here

