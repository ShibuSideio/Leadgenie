# Lead Sniper / Sideio Smart Growth — V22
**Full Technical Specification Document (TSD)**
*Last Updated: 2026-04-17 | Version: V22 PROD-FREEZE — Proprietary Intent Engine + Negative Knowledge Graph + L1 ROI Analytics Matrix*

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
│   ├── index.html                   # DOM scaffolding, Firebase SDK init, Auth UI (V18 redesigned)
│   ├── app.js                       # All frontend logic (3,594 lines — V20 Persona Vault + DT Engine)
│   ├── styles.css                   # CSS design system (V18+ additions)
│   ├── sw.js                        # Service Worker (cache v10-3, Firebase bypass)
│   └── manifest.json                # PWA manifest
├── /services
│   ├── /orchestrator                # Cloud Run: REST API Gateway + Cron Dispatcher + Persona Vault
│   │   ├── Dockerfile
│   │   ├── main.py                  # 3,095 lines — all API routes + cron sweep + Persona CRUD + migration hook
│   │   └── requirements.txt
│   ├── /pipeline-main               # Cloud Run: AI Extraction Engine (Cartographer / P5 Profiler)
│   │   ├── Dockerfile
│   │   ├── main.py                  # Search, scrape, score, write + V20 response_schema enforcement
│   │   └── requirements.txt
│   ├── /scraper-heavy               # Cloud Run: Playwright Headless Browser
│   │   ├── Dockerfile
│   │   ├── main.py                  # async Chromium + Decodo proxy tiers (standard + premium)
│   │   └── requirements.txt
│   ├── /digital-twin-engine         # Cloud Run: V20 Website Analyser + RLHF Market Trend Cache
│   │   ├── Dockerfile
│   │   ├── main.py                  # /analyze endpoint, unified Gemini schema, predictive_cache write
│   │   └── requirements.txt
│   ├── /shadow-learner-aggregator   # Cloud Run: RLHF Swarm Weight Aggregator
│   │   ├── Dockerfile
│   │   ├── main.py                  # Reads campaign RLHF deltas, writes global swarm_weights to Firestore
│   │   └── requirements.txt
│   ├── /autonomous-engine           # Cloud Run Job: Nightly Digital Exhaust Scraper
│   │   ├── Dockerfile
│   │   ├── engine.py                # Pre-scores leads into predictive_cache root collection (72h TTL)
│   │   └── requirements.txt
│   ├── /whatsapp-webhook            # Cloud Run: WhatsApp Business API Receiver
│   └── /email-summary               # Cloud Run: Email digest sender
├── /terraform                       # GCP infrastructure as code
├── .firebaserc                      # Firebase project binding (lead-sniper-prod)
├── firebase.json                    # Hosting config + Firestore rules pointer
├── firestore.rules                  # V13.22 multi-tenant security rules (BOM-stripped)
├── firestore.indexes.json           # Composite index: tenant_id + timestamp + is_in_crm (BOM-stripped)
├── cloudbuild.yaml                  # CI/CD: 20-step parallelized enterprise pipeline
└── architecture.md                  # This document (V20)
```

---

## 3. GCP INFRASTRUCTURE & SERVICE TOPOLOGY

| Service | Cloud Run Name | Auth | Memory | Region |
|---|---|---|---|---|
| Orchestrator | `orchestrator` | `--allow-unauthenticated` | 512 Mi | asia-south1 |
| Pipeline Main | `lead-pipeline-main` | `--no-allow-unauthenticated` | 1 Gi | asia-south1 |
| Scraper Heavy | `scraper-heavy` | `--no-allow-unauthenticated` | 2 Gi | asia-south1 |
| Digital Twin Engine | `digital-twin-engine` | `--no-allow-unauthenticated` | 512 Mi | asia-south1 |
| Shadow Learner Aggregator | `shadow-learner-aggregator` | `--no-allow-unauthenticated` | 256 Mi | asia-south1 |
| WhatsApp Webhook | `whatsapp-webhook` | `--allow-unauthenticated` | 128 Mi | asia-south1 |
| Email Summary | `email-summary` | `--no-allow-unauthenticated` | 128 Mi | asia-south1 |
| **Autonomous Engine** | **`autonomous-engine`** | **Cloud Run Job (no HTTP)** | **512 Mi** | **asia-south1** |
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
  "next_produce_due": "<TIMESTAMP>",
  "drip_interval_minutes": 60,
  "unprocessed_queue": [],
  "sourcing_vector": "Classic B2B",
  "persona_id": "<firestore_persona_doc_id>",
  "persona_bio": "Denormalised bio from linked Persona Vault entry.",
  "persona_keywords": "keyword1, keyword2",
  "persona_name": "Enterprise SaaS Decision Makers",
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

**Notes:**
- `keywords`: Stored as comma-separated string, parsed to array in pipeline
- ~~`target_urls`~~: **AMPUTATED in V22.** The "Suggest up to 10 websites" UI field and the `site:domain1 OR site:domain2` Serper injection loop have been permanently removed. The `target_urls` field may still be present in legacy Firestore documents but is **never read by the pipeline**. The Serper query builder now routes exclusively through the Autonomous Engine (Hybrid Starter Motor + BQ Exclusion Matrix).
- `next_drip_due`: Set by cron sweep after each dispatch. Controls per-campaign consumer drip rate
- `next_produce_due`: Set to `now + 24h` at creation by zero-wait enqueue. Controls producer re-run cadence
- `unprocessed_queue`: Array of Serper result objects awaiting Gemini profiling. Populated by producer, drained by consumer
- `sourcing_vector`: One of `Classic B2B`, `WalledGarden Social`, `B2B2C`. Set by Synaptic Router at creation
- `persona_id`: Firestore ID of the linked `tenant_profiles/{tenant_id}/personas/{id}` document
- `persona_bio/keywords/name`: Denormalised from Persona Vault at creation time for fast pipeline reads

### 4.4a `tenant_profiles/{tenant_id}/personas/{persona_id}` Sub-Collection
Persona Vault: named AI agent configurations scoped to a tenant. Each persona carries a structured ICP directive used by the pipeline to generate targeted outreach.

```json
{
  "_id": "auto_generated_firestore_id",
  "tenant_id": "uid_from_firebase_auth",
  "name": "Enterprise SaaS Decision Makers",
  "bio": "[Who we help]: ...\n[The problem we solve]: ...\n[Our unfair advantage]: ...",
  "keywords": "cto, vp engineering, saas, b2b",
  "is_legacy": false,
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

**Notes:**
- `is_legacy: true` marks personas auto-created by the silent migration hook from legacy `tenant_profiles.bio` fields
- Deleting a persona is blocked (HTTP 409) if any `active` campaigns still reference its `persona_id`
- On persona `PUT`, the orchestrator performs surgical cache invalidation: wipes all `users/{uid}/predictive_cache` documents linked to affected campaigns and denormalises updated `bio`/`keywords` back onto those campaign documents

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

### Step 5: Smart Query Generation — Hybrid Starter Motor
**Location:** `pipeline-main/main.py::generate_smart_query`

> **V22 Architecture:** The query generation layer is now a **Hybrid Confidence Router** that dynamically switches between a statistical BigQuery engine and a Gemini LLM fallback. The old `site:domain1 OR site:domain2` manual URL injection has been permanently amputated.

#### 5a. Shadow Tracker — Continuous Buyer Syntax Accumulator
Triggered asynchronously every time a lead is approved (`PUT /api/leads/{id}` with `status: approved`). Runs in a daemon thread and **never delays the 200 OK response**:

```python
# orchestrator/main.py::_do_shadow_track (daemon thread)
def _do_shadow_track(lead_text, persona_category, tenant_id):
    ngrams = extract_ngrams(lead_text, n=[2, 3])   # local Python NLP, zero API cost
    for gram in ngrams:
        bq.query("""
            INSERT OR UPDATE swarm_analytics.Intent_Keywords
            (persona_category, n_gram, occurrence_count, yield_weight)
            VALUES (@cat, @gram, 1, 1.0)
            ON DUPLICATE KEY UPDATE
              occurrence_count = occurrence_count + 1,
              yield_weight     = yield_weight     + 0.1
        """)
```

**BigQuery Table: `swarm_analytics.Intent_Keywords`**
| Column | Type | Description |
|---|---|---|
| `persona_category` | STRING | Persona or campaign name — scopes n-grams by target ICP |
| `n_gram` | STRING | Extracted buyer-syntax phrase (e.g., "struggling with") |
| `occurrence_count` | INTEGER | Raw frequency counter |
| `yield_weight` | FLOAT | Cumulative confidence mass. Router threshold key. |

#### 5b. Confidence Threshold Router
```python
# pipeline-main/main.py::generate_smart_query — Step 1
conf_query = """
    SELECT SUM(yield_weight) AS total_confidence
    FROM `{project}.swarm_analytics.Intent_Keywords`
    WHERE persona_category = @persona_category
"""
result = bq.query(conf_query).result(timeout=3.0)  # hard 3s timeout, never blocks pipeline
total_confidence = next(result).total_confidence or 0

THRESHOLD = system_config.get("confidence_threshold", 1000)  # configurable in Firestore
```

**Routing Decision:**

| Condition | Route | Query Source |
|---|---|---|
| `SUM(yield_weight) >= 1000` | **STATISTICAL BUILD** | Top 3 n-grams + Top 2 domains from BigQuery — zero Gemini cost |
| `SUM(yield_weight) < 1000` | **GEMINI_FALLBACK** | LLM starter motor generates symptom dorks (below) |

**STATISTICAL BUILD path** (high-confidence, post cold-start):
```python
top_ngrams = bq.query("""
    SELECT n_gram FROM swarm_analytics.Intent_Keywords
    WHERE persona_category = @cat
    ORDER BY yield_weight DESC LIMIT 3
""").result()
top_domains = bq.query("""
    SELECT root_domain FROM swarm_analytics.Negative_Signals  -- NOT blocked domains
    ... (top converting domains from ontology_map)
""").result()
stat_query = " OR ".join([f'"{g.n_gram}"' for g in top_ngrams])
```

**GEMINI_FALLBACK path** (cold start / low persona confidence):
```python
symptom_prompt = f"""The user solves this business problem: '{bio}'.
Generate 3 highly specific Google Search operators to find targets PUBLICLY EXPERIENCING this problem.
Rule 1: MUST include at least one query targeting site:linkedin.com, site:facebook.com, or site:reddit.com.
Rule 2: MUST append negative keywords (e.g., '-shop -cart -amazon -wiki').
Return ONLY a JSON list of 3 strings."""
symptom_dorks = call_gemini_2_5(symptom_prompt, expect_json=True)
```

Final queries (both paths) = generated queries + global blacklist:
```python
blacklist = "-wiki -jobs -careers -investors -support -\"login\" -www.zoominfo.com -www.ibm.com -www.amazon.com"
```

> **Note:** The Negative Knowledge Graph shield (Section V22.2) is applied to `blacklist` **after** this step, dynamically appending `-site:competitor.com` operators.

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

The platform is self-optimizing using zero-cost database reads and a BigQuery-backed mathematical confidence graph.

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

### 7.3 Function Map: Shadow Tracker — Buyer Syntax N-gram Accumulator (V22)
Every approved lead fires `_async_shadow_track()` — a fire-and-forget daemon thread in the Orchestrator:
1. Local Python NLP extracts 2-gram and 3-gram buyer-syntax phrases from the lead's `pain_point` and `dm` text
2. Performs a BigQuery `INSERT OR UPDATE` into `swarm_analytics.Intent_Keywords`
3. The Confidence Router in `generate_smart_query()` reads `SUM(yield_weight)` to decide between STATISTICAL BUILD and GEMINI_FALLBACK modes (see Section 5)

**Hardening:** The BQ insert is wrapped in `try/except Exception` inside the daemon thread. A BQ timeout or schema error **never propagates to the 200 OK response on the approve action**.

### 7.4 Function Map: Historical Query Mining
`generate_smart_query()` analyzes the tenant's last 20 successful leads to extract B2B trend phrases and injects them directly into the Serper search queries.

### 7.5 Function Map: Few-Shot DM Injection
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
| **POST** | **`/api/campaigns/{id}/run`** | **User** | **Epsilon-Greedy Router: splits quota between V16 cache + V14 Cartographer** |
| GET | `/api/leads` | User | List all tenant leads (limit 100) — legacy polling fallback |
| PUT | `/api/leads/{id}` | User | Update lead status + trigger RLHF backprop + fire Shadow Tracker daemon |
| POST | `/api/settings` | User | Save WhatsApp credentials (KMS encrypted) |
| **GET** | **`/api/analytics/roi`** | **User** | **L1 ROI Matrix: computes Ad Savings, Labor Savings, Pipeline Value over `?date_range=N` days** |
| **PUT** | **`/api/analytics/unit-economics`** | **User** | **Persist custom unit economics (CPL, SDR rate, deal size, conversion rate) to `users/{id}.unit_economics`** |
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


---

## 17. V16 AUTONOMOUS ENGINE — NIGHTLY DIGITAL EXHAUST SCRAPER

*Added: 2026-04-08 | Commit range: defb52a → e87b265*

### 17.1 Overview

The V16 Autonomous Engine is a **Cloud Run Job** (non-HTTP, not a Cloud Run Service) that runs nightly as a background job. It performs proactive "digital exhaust" scraping across the open web — social signals, hiring intent markers, and public sentiment data — and populates a `predictive_cache` collection with pre-scored leads. These cached leads are served with zero Serper API cost during the next campaign run via the Epsilon-Greedy Router.

**Key difference from V14/V15 pipeline:** The autonomous engine runs *before* campaign triggers. It pre-populates the cache; the Router then decides whether to serve from cache or call Serper.

### 17.2 New Infrastructure Components

| Component | Type | Name | Region |
|---|---|---|---|
| Autonomous Engine | Cloud Run Job | `autonomous-engine` | asia-south1 |
| Nightly Scheduler | Cloud Scheduler | `lead-sniper-nightly` | asia-south1 |

**Cloud Run Job spec:**
```yaml
# Created idempotently by cloudbuild.yaml step gcloud-job-provision-autonomous-engine
name: autonomous-engine
image: gcr.io/$PROJECT_ID/autonomous-engine
region: asia-south1
service-account: lead-pipeline-sa
task-timeout: 3600s   # 1-hour hard limit per execution
max-retries: 1
```

**Cloud Scheduler trigger:**
```yaml
# Created idempotently by cloudbuild.yaml step gcloud-job-scheduler-autonomous-engine
name: lead-sniper-nightly
schedule: "0 2 * * *"    # 2 AM IST daily
target: Cloud Run Job (not HTTP) — uses jobs.run API
auth: OAuth2 service account token (roles/run.invoker)
```

### 17.3 New Firestore Collections

#### `predictive_cache` Collection
Pre-scored leads from the nightly autonomous scrape. TTL: 72 hours.

```json
{
  "_id": "sha256(tenant_id + '_' + root_domain)",
  "tenant_id": "uid_from_firebase_auth",
  "url": "https://example.com",
  "score": 8,
  "pain_point": "AI-extracted pain signal from public posts",
  "dm": "Pre-drafted outreach message",
  "icebreaker_angle": "Opening hook specific to their public pain",
  "tech_stack_found": ["hubspot", "stripe"],
  "hiring_intent_found": "Yes",
  "origin_engine": "autonomous",
  "promotedAt": "<SERVER_TIMESTAMP>",
  "expire_at": "<TIMESTAMP +72 hours>"
}
```

**TTL:** Configured via Firestore TTL policy on field `expire_at`. Must be manually enabled in GCP Console: Firestore → Indexes → TTL → Collection `predictive_cache` → Field `expire_at`.

#### Updated `leads` collection — new field
```json
{
  "origin_engine": "autonomous"  // or "cartographer" (V14 Serper-driven)
}
```
This field drives the `⚡ Predictive Match` badge on lead cards.

### 17.4 Engine Logic (services/autonomous-engine/engine.py)

```python
# Entry point: runs as a Cloud Run Job (single invocation, exits when done)
def main():
    tenants = get_all_active_tenants()          # Reads users/ collection via Admin SDK
    for tenant in tenants:
        signals = harvest_digital_exhaust(tenant)  # Social scraping loop
        for signal in signals:
            if _can_use_gemini():                # Token kill-switch check
                lead = score_and_cache(signal)   # Gemini scoring + cache write
                store_in_predictive_cache(lead)

def _can_use_gemini():
    # Tracks daily Gemini call count via usage_metrics shards
    # Resets at midnight IST
    # Returns False when daily budget is exceeded → prevents runaway costs
    total_calls = sum_usage_shards()
    return total_calls < DAILY_GEMINI_BUDGET
```

**Token Kill-Switch (Critical Safety Mechanism):**
- Reads `usage_metrics/{tenant_id}/shards/{0-9}` to sum total Gemini calls
- Compares against `DAILY_GEMINI_BUDGET` environment variable
- Resets at midnight IST daily
- If budget exceeded: skips all Gemini calls, writes raw signals with `score: 0` (filtered out by score gate)
- Prevents runaway API costs from engine loops

### 17.5 Environment Variables (autonomous-engine)

```bash
PROJECT_ID=sideio-leads-v16
DAILY_GEMINI_BUDGET=1000         # Max Gemini calls per nightly run
DISCOVERY_ALLOCATION=0.15        # 15% of batch reserved for Serper discovery (Router config)
MOCK_MODE=false                  # Set true for dry-run without actual API calls
```

---

## 18. V16 EPSILON-GREEDY ROUTER — HYBRID LEAD SOURCING

*Added: 2026-04-08 | Location: services/orchestrator/main.py — POST /api/campaigns/{id}/run*

### 18.1 Overview

The Epsilon-Greedy Router is integrated into the `POST /api/campaigns/{id}/run` endpoint in the Orchestrator. Every time a campaign run is initiated (either by the user clicking "Find My Clients" or by the background cron sweep), the Router dynamically splits the lead quota between:

- **Exploit path (V16):** Serve pre-cached leads from `predictive_cache` (zero Serper cost)
- **Explore path (V14):** Fire the Cartographer/Serper pipeline for fresh discovery

### 18.2 Router Math (batch_size = 10, exploit_ratio = 0.10)

```python
batch_size    = 10
exploit_ratio = 0.10   # Configurable via DISCOVERY_ALLOCATION env var

# Step 1: Calculate split
autonomous_target   = int(batch_size * exploit_ratio)  # = 1
cartographer_target = batch_size - autonomous_target   # = 9

# Step 2: Pop from predictive_cache
cached_leads = _pop_from_predictive_cache(tenant_id, autonomous_target)
autonomous_promoted = len(cached_leads)                # Actual served (may be < target)

# Step 3: Deficit reallocation — CRITICAL SAFETY GUARANTEE
deficit = autonomous_target - autonomous_promoted      # Leads not served from cache
cartographer_actual = cartographer_target + deficit    # All deficit reallocated to Serper

# Step 4: Dispatch to Cartographer (Serper)
if cartographer_actual > 0:
    _dispatch_cartographer(campaign_id, tenant_id, cartographer_actual)

# Step 5: Promote cached leads to live leads collection
for lead in cached_leads:
    _promote_cached_lead(lead, campaign_id)
```

**Vulnerability A — Empty cache safety:** If `predictive_cache` has 0 leads, `_pop_from_predictive_cache` returns `[]`, deficit = `autonomous_target`, and full reallocation goes to Serper. No crash, no infinite loop.

**Vulnerability B — Serper payload cap:** The Cartographer receives exactly `cartographer_actual` (never the full `batch_size`). The Serper `/produce` payload is capped at `cartographer_target`. This is audited and confirmed safe.

### 18.3 Response Payload (POST /api/campaigns/{id}/run)

```json
{
  "status": "dispatched",
  "autonomous_promoted": 1,
  "cartographer_queued": 9,
  "total": 10
}
```

This response is surfaced on the frontend as a toast:
> "Engine dispatched: ⚡ 1 Predictive + 🔍 9 Cartographer leads"

### 18.4 `_pop_from_predictive_cache` — Safe Empty-Cache Handler

```python
def _pop_from_predictive_cache(tenant_id: str, count: int) -> list:
    """
    Fetches up to `count` leads from predictive_cache for this tenant.
    Returns empty list (never raises) if cache is empty.
    Deletes fetched docs atomically to prevent double-serving.
    """
    ref = db.collection("predictive_cache") \
            .where("tenant_id", "==", tenant_id) \
            .limit(count)
    docs = ref.get()
    if not docs:
        return []   # Safe: caller handles deficit via reallocation
    
    batch = db.batch()
    leads = []
    for doc in docs:
        leads.append(doc.to_dict())
        batch.delete(doc.reference)   # Atomic pop
    batch.commit()
    return leads
```

### 18.5 OIDC-Protected Internal Endpoints

All cron-triggered endpoints are protected by Google OIDC token verification. Frontend Firebase ID tokens are explicitly rejected:

```python
# Cron endpoints: /api/internal/cron/*
# Verified via: google.oauth2.id_token.verify_oauth2_token()
# Audience: Cloud Run service URL
# Rejects: Firebase ID tokens (wrong issuer)
```

---

## 19. ENTERPRISE CLOUD BUILD PIPELINE (V16)

*Updated: 2026-04-08 | Build steps: 20 total | Strategy: Fully parallelized*

### 19.1 Pipeline Architecture

The `cloudbuild.yaml` was completely rewritten from a sequential 16-step pipeline to a **20-step parallelized enterprise pipeline**. Docker builds across all 6 services run simultaneously in `waitFor: ['-']` parallel groups.

```
Step Group 1 (parallel — all fire at once):
  #0  build-orchestrator           → gcr.io/$PROJECT_ID/lead-orchestrator
  #1  build-pipeline-main          → gcr.io/$PROJECT_ID/lead-pipeline-main
  #2  build-scraper-heavy          → gcr.io/$PROJECT_ID/scraper-heavy
  #3  build-whatsapp-webhook       → gcr.io/$PROJECT_ID/whatsapp-webhook
  #4  build-email-summary          → gcr.io/$PROJECT_ID/email-summary
  #5  build-autonomous-engine      → gcr.io/$PROJECT_ID/autonomous-engine
  #19 firebase-deploy              → Firebase Hosting + Firestore rules

Step Group 2 (parallel — after group 1 pushes):
  #6  push-orchestrator
  #7  push-pipeline-main
  #8  push-scraper-heavy
  #9  push-whatsapp-webhook
  #10 push-email-summary
  #11 push-autonomous-engine

Step Group 3 (parallel — after pushes):
  #12 deploy-orchestrator          → Cloud Run Service (--allow-unauthenticated)
  #13 deploy-pipeline-main         → Cloud Run Service (--no-allow-unauthenticated)
  #14 deploy-scraper-heavy         → Cloud Run Service (--no-allow-unauthenticated)
  #15 deploy-whatsapp-webhook      → Cloud Run Service (--allow-unauthenticated)
  #16 deploy-email-summary         → Cloud Run Service (--no-allow-unauthenticated)
  #17 deploy-autonomous-engine     → Cloud Run Job (gcloud run jobs deploy)

Step #18 (sequential — after all deploys):
  gcloud-job-provision-autonomous-engine  → Creates/updates Cloud Run Job definition
  gcloud-job-scheduler-autonomous-engine  → Creates/updates Cloud Scheduler job (idempotent)
```

### 19.2 Idempotent Provisioning Pattern

Cloud Scheduler creation uses `--quiet` and falls back gracefully if the job already exists:

```bash
gcloud scheduler jobs create http lead-sniper-nightly \
  --schedule="0 2 * * *" \
  --uri="$NIGHTLY_URL" \
  --oidc-service-account-email="$SA_EMAIL" \
  --location=asia-south1 --quiet || \
gcloud scheduler jobs update http lead-sniper-nightly \
  --schedule="0 2 * * *" \
  --uri="$NIGHTLY_URL" \
  --oidc-service-account-email="$SA_EMAIL" \
  --location=asia-south1 --quiet
```

This pattern prevents Cloud Build failures on subsequent deploys when the scheduler job already exists.

### 19.3 Build Substitutions (All Required)

```yaml
substitutions:
  _PROJECT_ID: "sideio-leads-v16"
  _REGION: "asia-south1"
  _FIREBASE_PROJECT: "lead-sniper-prod"
  _PIPELINE_SA_EMAIL: "lead-pipeline-sa@sideio-leads-v16.iam.gserviceaccount.com"
  _PIPELINE_URL: "https://lead-pipeline-main-222247989819.asia-south1.run.app/dispatch"
  _SCRAPER_URL: "https://scraper-heavy-222247989819.asia-south1.run.app/scrape"
  _ORCH_URL: "https://orchestrator-222247989819.asia-south1.run.app"
```

> ⚠️ **Critical:** All bash variable substitutions in shell commands use `$$VAR` (double dollar) to prevent Cloud Build from attempting to substitute them as build substitutions.

### 19.4 Dependency Matrix Fixes (V16)

The following dependency conflicts were resolved during V16 enterprise audit:

| Service | Package | Before | After | Reason |
|---|---|---|---|---|
| orchestrator | `httpx` | `==0.26.0` (pinned) | `>=0.26.0` | google-genai requires newer httpx |
| pipeline-main | `httpx` | `==0.26.0` (pinned) | `>=0.26.0` | pip backtracking resolved |
| email-summary | `httpx` | `==0.26.0` (pinned) | `>=0.26.0` | pip backtracking resolved |
| pipeline-main | `google-protobuf` | present | **removed** | Package does not exist; google-api-core handles it |
| pipeline-main | `google-cloud-firestore` | `>=2.14.0` | `==2.14.0` (pinned) | Prevents grpcio version conflicts |

**Root cause of pip backtracking:** `google-genai` SDK depends on `httpx>=0.28.0`, which conflicted with the old pinned `==0.26.0`. Relaxing to `>=0.26.0` allows pip to resolve both constraints without backtracking.

### 19.5 Firebase CLI UTF-8 BOM Fix

`firestore.rules` and `firestore.indexes.json` were saved with UTF-8 BOM (Byte Order Mark) by Windows editors. The Firebase CLI parser does not handle BOM and threw `token recognition error at: '&#65279;'` (BOM character). Both files were re-saved without BOM using PowerShell `Set-Content -Encoding UTF8` (without BOM).

---

## 20. FORENSIC SECURITY AUDIT (V16 PRE-IGNITION)

*Conducted: 2026-04-08 | Scope: 4 critical nodes*

This audit was conducted before enabling live Serper API traffic (4,000 credits loaded). All 4 nodes passed.

### Node 1: Epsilon-Greedy Router — PASS ✅
- **Vulnerability A (Empty cache crash):** `_pop_from_predictive_cache` returns `[]` on empty cache. Deficit is reallocated 100% to Cartographer. No crash, no infinite loop.
- **Vulnerability B (Serper payload overflow):** Cartographer receives `cartographer_target + deficit`, never `batch_size`. Serper payload is capped correctly.
- **Math verification (batch=10, exploit=0.10):** autonomous_target=1, cartographer_target=9. If cache empty: deficit=1, cartographer_actual=10. Matches expected behavior.

### Node 2: Gemini Token Kill-Switch — PASS ✅
- `_can_use_gemini()` reads shard aggregates from `usage_metrics/{tenant_id}/shards/*`
- Resets at midnight IST via timestamp comparison
- If `total_calls >= DAILY_GEMINI_BUDGET`: returns `False`, all Gemini calls skipped
- Leads with `score: 0` are rejected by the score gate (>= 7 required) — no garbage written to `leads` collection

### Node 3: Deduplication Ledger — PASS ✅
- `lead_id = sha256(tenant_id + '_' + root_domain)` — deterministic
- `doc_ref.create()` raises `AlreadyExists` on duplicate → caught, campaign appended to `matched_campaigns` array, loop continues
- Cross-tenant: `global_lead_locks` collection provides 14-day exclusivity per domain
- No lead can appear twice for the same tenant; no lead can be served to two tenants simultaneously

### Node 4: Endpoint Security — PASS ✅
- All public endpoints: Firebase ID token verified via `firebase_admin.auth.verify_id_token()`
- Cron endpoints (`/api/internal/cron/*`): Google OIDC token verified via `google.oauth2.id_token.verify_oauth2_token()`
- Internal service-to-service: OIDC tokens fetched from GCP metadata server with exponential backoff
- `super_admin` routes: role checked from Firestore `users/{uid}.role` field after token verification

---

## 21. V17 FRONTEND — GOOGLE-LIKE UX REDESIGN

*Added: 2026-04-08 | Commit: e87b265 | Files: public/index.html, public/app.js, public/styles.css*

### 21.1 Design Philosophy

The V17 redesign transitions the Sideio interface from a developer-centric legacy tool to a **perception-first business growth platform**. The design principle follows the Google Search paradigm: replace complex multi-field forms with a single conversational input that hides technical complexity from the business owner.

**Before V17:** 6-field form (Name, Bio, Keywords, Country dropdown, City input, Target URLs)
**After V17:** One sentence describing who you want to reach → system auto-generates all technical parameters

### 21.2 Navigation Simplification

| Old Label | New Label | Reason |
|---|---|---|
| Dashboard | Home | Business-owner language |
| Targeting | My Searches | Describes what they did, not the tech |
| (new) | Pipeline CRM | Direct link to the CRM view |
| Custom Reports | Reports | Simplified |
| L0 Admin | Admin | Hidden unless super_admin role |
| My Team | (removed) | Unused, added noise |

### 21.3 "Find New Clients" — 2-Step Conversational Modal

#### Step 1: Single-Sentence Intent Input

The modal opens to a single large textarea styled like a Google Search bar:

```
Who are your next clients?
Describe them in plain English. We handle the rest.

[  🔍 Small e-commerce businesses in the UK that are growing fast...  ]

Quick start ↓
[🏪 Local service businesses that need more customers]
[💻 SaaS startups actively hiring sales engineers]
[📦 E-commerce stores running paid ads]
[🏥 Healthcare clinics expanding into new areas]

[  Find My Clients →  ]
```

**Template chips:** Clicking a chip fills the textarea — eliminates blank-state anxiety.
**Enter key:** Submits Step 1 (Shift+Enter inserts newline).
**Character hint:** Dynamically updates: "A bit more detail helps get better results ↓" → "Good. Add a location for sharper targeting →" → "✓ Ready — click to proceed".

#### Step 2: Smart Confirmation Card

The system parses the sentence and shows what it understood:

```
✓ Here's what I found

🎯 You want to reach
   "e-commerce businesses in the UK that are growing fast"  [Edit]

💼 What you offer them
   Tell me what you sell…                                   [Add ↓]
   ⚡ This helps us write a personalised pitch for each lead.

📍 Where should I look?
   [🌍 Worldwide] [🇮🇳 India] [🇺🇸 USA] [🇬🇧 UK] [🇨🇦 Canada] [🇦🇺 Australia]
                                                          ← auto-selected from Step 1
   [City or region (optional)]

[🚀 Find My Clients]
Leads are matched to your description. Quality over quantity, always.
```

#### Intent Parser (`fcParseIntent`)

Extracts location from the natural language sentence using regex matching:

```javascript
const locationMap = [
    { re: /\b(united\s*states|usa)\b/i,    gl: 'us', label: 'United States' },
    { re: /\b(united\s*kingdom|uk|britain|london)\b/i, gl: 'uk', label: 'United Kingdom' },
    { re: /\b(canada|toronto|vancouver)\b/i, gl: 'ca', label: 'Canada' },
    { re: /\b(australia|sydney|melbourne)\b/i, gl: 'au', label: 'Australia' },
    { re: /\b(india|mumbai|delhi|bangalore|bangalore|hyderabad|pune|chennai|kolkata)\b/i, gl: 'in', label: 'India' },
];
// Strips location phrase from "who" summary
// Auto-selects the correct country chip in Step 2
```

#### Campaign Name Auto-Generation (`fcBuildCampaignName`)

Campaign name is auto-generated — the user never types a technical name:

```javascript
function fcBuildCampaignName(who, where) {
    const base = who.length > 35 ? who.substring(0, 35).trim() + '…' : who;
    return where ? `${base} · ${where} · ${month} ${year}` : `${base} · ${month} ${year}`;
    // Example: "e-commerce businesses growing fast · UK · Apr 2026"
}
```

#### Smart Validation (Conversational, Not Error Messages)

| Missing field | User sees |
|---|---|
| Intent sentence < 5 chars | Textarea border turns red, refocuses, no error modal |
| Product bio < 15 chars | "⚡ This helps us write a personalised pitch for every lead. Please add a sentence or two." |
| No location selected | "📍 Please pick a location so I know where to focus." |

No technical error messages. All guidance is framed as the system helping the user get better results.

#### Hidden Fields (Backend Compatibility)

The conversational modal populates the same hidden `<input>` fields that `saveCampaignAction()` already reads, maintaining full backend compatibility:

```html
<input type="hidden" id="camp-gl" />        <!-- Country code: "uk" -->
<input type="hidden" id="camp-location" />   <!-- "London, United Kingdom" -->
<input type="hidden" id="camp-name" />       <!-- Auto-generated name -->
<input type="hidden" id="camp-bio" />        <!-- What the user sells -->
<input type="hidden" id="camp-keys" />       <!-- First 120 chars of who-description -->
<input type="hidden" id="camp-target-urls" /> <!-- Empty (user doesn't see this) -->
```

#### Auto Geo-Detection

When the modal opens, `ipapi.co/json/` is called to detect the user's country and city. The matching country chip is pre-selected in Step 2 — the user usually doesn't need to pick a location at all.

### 21.4 Dashboard Greeting Bar + KPI Tiles

Replaces the static "Your Latest Hot Leads" header.

```
Good morning, Sunilkumar.          [+ Find New Clients]

[🔥 1 New leads] [💬 0 Contacted] [🏆 0 Converted]
```

**Greeting logic:**
```javascript
function fcUpdateGreeting(firstName) {
    const hr = new Date().getHours();
    const g  = hr < 12 ? 'Good morning' : hr < 17 ? 'Good afternoon' : 'Good evening';
    el.textContent = firstName ? `${g}, ${firstName}.` : `${g}.`;
}
// Called from loadMe() after wallet/user data loads, using auth.currentUser.displayName
```

**KPI tile data source:**
```javascript
function fcUpdateKPIs(leadsArray) {
    const counts = { new: 0, contacted: 0, converted: 0 };
    leadsArray.forEach(l => {
        if (l.status === 'new' || l.status === 'processing') counts.new++;
        else if (l.status === 'contacted' || l.status === 'replied') counts.contacted++;
        else if (l.status === 'converted') counts.converted++;
    });
    // Updates #kpi-new-count, #kpi-contacted-count, #kpi-won-count
}
// Called from Firestore onSnapshot handler on every leads update
```

### 21.5 Lead Card V2 — Fold Architecture (`createLeadCardV2`)

New lead card design. Default state shows minimal information. Full intelligence requires a single click.

#### Default (Folded) State

```
[Company Name ↗]                                        [🔥 ⣿⣿⣿⣿⣿⣿⣿⣿░░ 8/10]
Web Signal · 2h ago

Complaining about high customer acquisition costs on public posts.

[⚡ Predictive] [🔒 Exclusive] [🟢 Hiring] [🎯 Competitor: SalesLoft]

↓ See opening message & full intelligence

[  ✉ Contact This Lead  ] [→ CRM] [···]
```

#### Expanded State (single click)

```
YOUR OPENING MESSAGE ────────────────────────────────────────────
│ Hey [Name], noticed you mentioned on LinkedIn that your CAC has
│ been climbing this quarter. We've helped 3 similar SaaS companies
│ cut acquisition cost by 34% using targeted outreach…

WHY THIS LEAD ───────────────────────────────────────────────────
Active hiring for a Head of Growth. This means they recognize the
need to scale acquisition — prime timing for an outreach tool pitch.

LIKELY OBJECTION ────────────────────────────────────────────────
⚠️ They use HubSpot internally — may feel they have enough tooling.

CONTACT INFO ───────────────────────────────────────────────────
✉ hr@techcorp.com  📞 +1-312-555-0199
```

#### Action Row Consolidation

| Before V17 | After V17 | Change |
|---|---|---|
| 📋 Copy Message | ✉ Contact This Lead | Primary action |
| ☁️ Push to CRM | → CRM | Secondary action |
| 🚫 Ignore | ··· → Skip This Lead | In overflow menu |
| 🎯 Converted | ··· → Mark Converted | In overflow menu |
| 🕒 View Timeline Logs | ··· → View Timeline | In overflow menu |

**From 5 visible buttons → 2 primary buttons + 1 overflow menu (···)**

#### Score Visualization

| Before V17 | After V17 |
|---|---|
| `Score: 8/10` (text badge) | 🔥 gradient heat bar + emoji |
| Green badge color | `linear-gradient(90deg, #f97316, #ef4444)` fill |
| — | Emoji: 🔥 (9-10), ⚡ (7-8), 👍 (5-6), 📋 (<5) |

#### Source Labels (Human-Readable)

| Internal value | Displayed as |
|---|---|
| `origin_engine: "autonomous"` | `AI Match` |
| `origin_engine: "cartographer"` | `Web Signal` |
| Timestamp | Relative: `2h ago`, `Found yesterday` |

### 21.6 Virtual Observer — V17 Update

The IntersectionObserver was updated to use `createLeadCardV2` (DOM element replacement) instead of `generateLeadInnerHtml` (innerHTML string injection):

```javascript
// Before V17 (innerHTML injection):
entry.target.innerHTML = generateLeadInnerHtml(leadId, lead);
entry.target.setAttribute('data-rendered', 'true');

// After V17 (element replacement):
const newCard = window.createLeadCardV2(leadId, lead);
entry.target.replaceWith(newCard);           // replaceWith preserves scroll position
virtualObserver.unobserve(entry.target);
virtualObserver.observe(newCard);
newCard.setAttribute('data-rendered', 'true');
```

**Why `replaceWith()` instead of `innerHTML`:** The V2 card contains event handlers bound in JavaScript (overflow menus, expand toggles). innerHTML injection loses event handler scope context. DOM element replacement preserves the full element tree including event closures.

### 21.7 CSS Design System Additions (V17)

All new styles are additive — appended to `styles.css` without removing existing classes.

| CSS Class Group | Purpose |
|---|---|
| `.fc-overlay`, `.fc-modal` | Conversational modal container + animation |
| `.fc-intent-input`, `.fc-input-wrap` | Google-like single search input |
| `.fc-chip`, `.fc-chips` | Quick-start template buttons |
| `.fc-loc-chip`, `.fc-location-chips` | Flag-based location selector |
| `.fc-confirm-block`, `.fc-block-*` | Step 2 confirmation card rows |
| `.fc-validation-bar` | Non-modal validation message bar |
| `.greeting-bar`, `.find-clients-hero-btn` | Dashboard greeting + hero CTA |
| `.kpi-tiles`, `.kpi-tile`, `.kpi-*` | 3-tile KPI row |
| `.lead-card-v2`, `.lc-*` | New folded lead card system |
| `.lc-heat-bar`, `.lc-heat-fill` | Score gradient heat bar |
| `.lc-expanded`, `.lc-expanded.open` | Fold/expand animation |
| `.lc-overflow-menu`, `.lc-overflow-item` | ··· action overflow popup |

### 21.8 New JavaScript Utilities (V17)

| Function | Purpose |
|---|---|
| `fcParseIntent(sentence)` | Extracts who/where from natural language |
| `fcBuildCampaignName(who, where)` | Auto-generates campaign name with date stamp |
| `fcTimeAgo(timestamp)` | Formats Firestore timestamps as "2h ago" |
| `fcUpdateGreeting(firstName)` | Sets time-aware greeting (Good morning/afternoon/evening) |
| `fcUpdateKPIs(leadsArray)` | Computes and renders KPI tile counts |
| `fcStep1Next()` | Validates Step 1, parses intent, transitions to Step 2 |
| `fcGoBack()` | Transitions Step 2 → Step 1 |
| `fcToggleEdit(field)` | Inline edit toggle for who/what blocks |
| `fcSelectLocation(btn)` | Location chip selection handler |
| `fcLaunch()` | Final validation → populates hidden fields → calls saveCampaignAction() |
| `fcFillTemplate(btn)` | Fills textarea from quick-start chip |
| `fcUpdateCharHint(el)` | Character count hint updater |
| `closeNewCampaignModal()` | Closes modal + resets all step state |
| `createLeadCardV2(docId, lead)` | Builds full folded lead card DOM element |
| `lcToggleExpand(docId)` | Expand/collapse intelligence section |
| `lcToggleMore(docId)` | Open/close ··· overflow menu |
| `getScoreEmoji(score)` | Returns heat emoji for score (🔥 ⚡ 👍 📋) |

### 21.9 Updated UI Element IDs

New DOM IDs introduced in V17 (required for JS bindings):

| ID | Location | Purpose |
|---|---|---|
| `greeting-message` | Dashboard heading | Greeting text target |
| `greeting-sub` | Dashboard subheading | Subtitle text target |
| `kpi-new-count` | KPI tile | New leads count |
| `kpi-contacted-count` | KPI tile | Contacted count |
| `kpi-won-count` | KPI tile | Converted count |
| `fc-step-1` | Modal | Step 1 container |
| `fc-step-2` | Modal | Step 2 container |
| `fc-intent` | Modal Step 1 | Intent textarea |
| `fc-char-hint` | Modal Step 1 | Character hint text |
| `fc-confirm-who` | Modal Step 2 | Parsed "who" display |
| `fc-confirm-what` | Modal Step 2 | Product bio display |
| `fc-edit-who` | Modal Step 2 | Editable who field |
| `fc-edit-what` | Modal Step 2 | Editable bio textarea |
| `fc-edit-where-city` | Modal Step 2 | City input |
| `fc-what-required` | Modal Step 2 | Bio validation hint |
| `fc-where-required` | Modal Step 2 | Location validation hint |
| `fc-validation-bar` | Modal Step 2 | Error message bar |
| `fc-what-btn` | Modal Step 2 | Add/Save bio button |
| `fc-block-where` | Modal Step 2 | Location block container |
| `nav-credit-pill` | Navigation | Credits display container |

---

## 22. KEY DESIGN INVARIANTS (UPDATED — V17)

These are added to the existing invariants in Section 16.

8. **Campaign name is never user-typed:** `fcBuildCampaignName()` auto-generates it from the parsed intent + location + date. Users should never see a "Campaign Name" input field.

9. **The conversational modal owns the hidden fields:** `camp-gl`, `camp-location`, `camp-name`, `camp-bio`, `camp-keys` are all `<input type="hidden">` populated by `fcLaunch()`. `saveCampaignAction()` reads them as before — no change to backend call sequence.

10. **Lead card V2 is additive:** `createLeadCard()` (V14 renderer) is retained in the codebase. `createLeadCardV2()` is the active renderer. The VirtualObserver calls `createLeadCardV2` exclusively. Do not delete `createLeadCard` — it may be referenced by legacy flows.

11. **Serper credits are guarded conversationally:** Validation in Step 2 ensures `camp-bio` (what you sell) and location are always present before `saveCampaignAction()` fires. This prevents sending empty/low-quality parameters to Serper that would waste API credits.

12. **Autonomous engine is a Cloud Run Job, not a Service:** It has no HTTP endpoint. It is triggered exclusively via Cloud Scheduler using the `gcloud run jobs execute` API flow. Never configure it as a Service with HTTP traffic.

13. **predictive_cache TTL:** TTL policy must be enabled manually in GCP Console. Cloud Build cannot enable it programmatically. The TTL field is `expire_at`. After initial deployment, an operator must enable TTL in: GCP Console → Firestore → Indexes → TTL → add for collection `predictive_cache`, field `expire_at`.

14. **DISCOVERY_ALLOCATION controls the Epsilon-Greedy split:** Default 0.15 (15% from cache, 85% from Serper). Raise this as the autonomous engine cache fills up. Lower it during early operation when cache is sparse.



---

## 23. V18/V19: PREDICTIVE CAMPAIGN ENGINE PIVOT (ENTERPRISE UPGRADE)

*Introduced: 2026-04-10*

This phase marks the transition from manual UI inputs to a "Zero-Click" proactive intelligence paradigm. The system now deduces the user's business context automatically and predicts highly relevant campaigns based on market trends.

### 23.1 Strict Data Model Separation
Prior to V18, the system relied entirely on active child `campaigns`. Now, the state architecture is bifurcated natively:

1. **Master Twin (`tenant_profiles` Collection):**
   - Single, permanent root-level document per `tenant_id`.
   - Stores the company DNA (`bio`, `target_personas`), extracted website geography, and the `knowledge_base_text` arrays.
   - Handled exclusively via `POST /api/tenant_profiles`.
   
2. **Execution Nodes (`campaigns` Collection):**
   - Pure "hunting patterns" spawning out of the Master Twin.
   - Returned via `GET /api/campaigns`. The orchestrator logic has been refactored so that pulling campaigns explicitly segregates `tenant_profiles`, ensuring creation of a Master Twin does not organically increment the tenant's active 1/N campaign limit tracking bug.

### 23.2 `digital-twin-engine` Dual-Chain Concurrent Pipeline
Because the `POST /api/analyze-website` endpoint is constrained by a hard 7-second timeout reverse-proxied via Firebase, sequential LLM processing is impossible.
1. **Task A (Data Extraction):** Extracts `company_bio` and core `target_personas` via Gemini.
2. **Task B (Trend Triangulation):** In parallel, synthesizes a dynamic array of `recommended_campaigns`, crossing the product portfolio with macro-economic triggers, pain points, and specific unfair advantages. 
3. **Execution:** Orchestrated via `concurrent.futures.ThreadPoolExecutor(max_workers=2)` inside `digital-twin-engine/main.py`.

### 23.3 RLHF Routing Layer (`market_trend_cache`)
We cannot query Vertex AI blindly for every generalized query (e.g., standard "B2B SaaS" queries). We utilize a human-in-the-loop cache structure:
- **Cache Lookup:** Task B polls `market_trend_cache` in Firestore. If the extracted target product identically maps to an existing high-confidence historical record, it retrieves the trend instantly (0ms latency, $0 compute cost).
- **The Backprop:** When a human actively modifies a generated trend on the frontend (Predictive Card UI) and launches the campaign with `human_edited: true`, the Orchestrator (`services/orchestrator/main.py`) captures this event and dispatches a background push syncing the successfully human-vetted context into the `market_trend_cache` to serve future cold-start users.

### 23.4 The ⚙️ Business Profile Hub & Knowledge Base Extraction
- **The Hub:** `public/index.html` hosts `#business-profile-modal`, offering read/edit oversight over the `tenant_profile`.
- **Knowledge Base Upload Guardrail (No Base64):** Files (PDF, TXT) are uploaded aggressively straight to Firebase Storage under `gs://{FIREBASE_STORAGE_BUCKET}/knowledge_bases/{tenant_id}/{file.name}` directly via the frontend native Firebase-SDK. 
- **The Extractor Endpoint:** `app.js` issues `POST /api/tenant_profiles/extract-kb`. In `orchestrator/main.py`, downloading happens natively into memory via `io.BytesIO`. `PyPDF2` strips text, truncates it to 10kb to dodge DB limits, and registers it to the database iteratively using `firestore.ArrayUnion([extracted_text])`.

### 23.5 Copilot Mind-Map Separation & GL Mapping Guardrails
Replacing the deprecated legacy keyword block method, `saveCampaignAction` structurally dissects variables to prevent Serper dilution downstream.
1. **Input Vectors (`c-card-edit-` or Custom Fallback):**
   - `campaign_focus`: Primary target identifier (e.g. "Enterprise SEO"). Route to Scraper engine natively.
   - `pain_point`: Route natively to the internal Vertex prompt.
   - `unfair_advantage`: Route natively to the internal Vertex prompt.
   - `location`: Explicit geolocation string (e.g. "London", "EMEA", "Worldwide").
2. **The "GL" Engine (`orchestrator/main.py`):**
   - The Orchestrator evaluates the `location` string against `gl_map` (e.g., "United Kingdom" → `uk`). If mapped successfully, `gl` code triggers to override Serper's standard US bounding box. Unmapped granular values (e.g. "Silicon Valley") fallback the `gl` map to "us" but retain the `location` text vector directly for exact SERP search modifications automatically.

### 23.6 Preflight CORS Engine Adjustment
Firebase Hosted reverse proxies mapped to absolute backend routes triggered `503` / `500` server errors due to frontend explicit `.ok` handling issues and the absence of `request.method == 'OPTIONS'` in the Python dispatcher. 
- Python endpoints now proactively serve `('', 204)` explicitly before performing Bearer token `Authorization` verification to circumvent silent pre-flight Auth failures. 
- `public/app.js` relies strictly on relative API routes parsed securely by `firebase.json`'s generalized `{"source": "/api/**"}` routing table mapped to the `orchestrator` Cloud Run service instance.


---

## 23. V18/V19: PREDICTIVE CAMPAIGN ENGINE PIVOT (ENTERPRISE UPGRADE)

*Introduced: 2026-04-10*

This phase marks the transition from manual UI inputs to a "Zero-Click" proactive intelligence paradigm. The system now deduces the user's business context automatically and predicts highly relevant campaigns based on market trends.

### 23.1 Strict Data Model Separation
Prior to V18, the system relied entirely on active child `campaigns`. Now, the state architecture is bifurcated natively:

1. **Master Twin (`tenant_profiles` Collection):**
   - Single, permanent root-level document per `tenant_id`.
   - Stores the company DNA (`bio`, `target_personas`), extracted website geography, and the `knowledge_base_text` arrays.
   - Handled exclusively via `POST /api/tenant_profiles`.
   
2. **Execution Nodes (`campaigns` Collection):**
   - Pure "hunting patterns" spawning out of the Master Twin.
   - Returned via `GET /api/campaigns`. The orchestrator logic has been refactored so that pulling campaigns explicitly segregates `tenant_profiles`, ensuring creation of a Master Twin does not organically increment the tenant's active 1/N campaign limit tracking bug.

### 23.2 `digital-twin-engine` Dual-Chain Concurrent Pipeline
Because the `POST /api/analyze-website` endpoint is constrained by a hard 7-second timeout reverse-proxied via Firebase, sequential LLM processing is impossible.
1. **Task A (Data Extraction):** Extracts `company_bio` and core `target_personas` via Gemini.
2. **Task B (Trend Triangulation):** In parallel, synthesizes a dynamic array of `recommended_campaigns`, crossing the product portfolio with macro-economic triggers, pain points, and specific unfair advantages. 
3. **Execution:** Orchestrated via `concurrent.futures.ThreadPoolExecutor(max_workers=2)` inside `digital-twin-engine/main.py`.

### 23.3 RLHF Routing Layer (`market_trend_cache`)
We cannot query Vertex AI blindly for every generalized query (e.g., standard "B2B SaaS" queries). We utilize a human-in-the-loop cache structure:
- **Cache Lookup:** Task B polls `market_trend_cache` in Firestore. If the extracted target product identically maps to an existing high-confidence historical record, it retrieves the trend instantly (0ms latency, $0 compute cost).
- **The Backprop:** When a human actively modifies a generated trend on the frontend (Predictive Card UI) and launches the campaign with `human_edited: true`, the Orchestrator (`services/orchestrator/main.py`) captures this event and dispatches a background push syncing the successfully human-vetted context into the `market_trend_cache` to serve future cold-start users.

### 23.4 The ⚙️ Business Profile Hub & Knowledge Base Extraction
- **The Hub:** `public/index.html` hosts `#business-profile-modal`, offering read/edit oversight over the `tenant_profile`.
- **Knowledge Base Upload Guardrail (No Base64):** Files (PDF, TXT) are uploaded aggressively straight to Firebase Storage under `gs://{FIREBASE_STORAGE_BUCKET}/knowledge_bases/{tenant_id}/{file.name}` directly via the frontend native Firebase-SDK. 
- **The Extractor Endpoint:** `app.js` issues `POST /api/tenant_profiles/extract-kb`. In `orchestrator/main.py`, downloading happens natively into memory via `io.BytesIO`. `PyPDF2` strips text, truncates it to 10kb to dodge DB limits, and registers it to the database iteratively using `firestore.ArrayUnion([extracted_text])`.

### 23.5 Copilot Mind-Map Separation & GL Mapping Guardrails
Replacing the deprecated legacy keyword block method, `saveCampaignAction` structurally dissects variables to prevent Serper dilution downstream.
1. **Input Vectors (`c-card-edit-` or Custom Fallback):**
   - `campaign_focus`: Primary target identifier (e.g. "Enterprise SEO"). Route to Scraper engine natively.
   - `pain_point`: Route natively to the internal Vertex prompt.
   - `unfair_advantage`: Route natively to the internal Vertex prompt.
   - `location`: Explicit geolocation string (e.g. "London", "EMEA", "Worldwide").
2. **The "GL" Engine (`orchestrator/main.py`):**
   - The Orchestrator evaluates the `location` string against `gl_map` (e.g., "United Kingdom" → `uk`). If mapped successfully, `gl` code triggers to override Serper's standard US bounding box. Unmapped granular values (e.g. "Silicon Valley") fallback the `gl` map to "us" but retain the `location` text vector directly for exact SERP search modifications automatically.

### 23.6 Preflight CORS Engine Adjustment
Firebase Hosted reverse proxies mapped to absolute backend routes triggered `503` / `500` server errors due to frontend explicit `.ok` handling issues and the absence of `request.method == 'OPTIONS'` in the Python dispatcher. 
- Python endpoints now proactively serve `('', 204)` explicitly before performing Bearer token `Authorization` verification to circumvent silent pre-flight Auth failures. 
- `public/app.js` relies strictly on relative API routes parsed securely by `firebase.json`'s generalized `{"source": "/api/**"}` routing table mapped to the `orchestrator` Cloud Run service instance.

---

## 24. `ontology_map` COLLECTION — DOMAIN AFFINITY LEDGER

*Reverse-engineered: 2026-04-14 | Status: Production-active since V16 | Previously undocumented*

### 24.1 Purpose

`ontology_map` is the **global domain intelligence repository** for the Sideio platform. It is a persistent, self-updating weight table that records the historical signal quality of every URL domain and social sub-path that has ever produced a cached lead. It is the physical substrate of the **Ontology RLHF feedback loop** — a closed-loop learning system that reinforces high-converting domains and penalises low-converting ones over time.

**Primary function:** Provide the `autonomous-engine` with a prior probability score (`baseline_weight`) before any Gemini call is made, enabling intelligent routing between **exploit** (proven domains) and **explore** (undiscovered domains) buckets.

### 24.2 Document ID — Key Derivation

Document IDs are **not UUIDs**. They are deterministically derived from the source URL by the `parse_base_path()` function, which is duplicated identically in `autonomous-engine/engine.py` (L75) and `orchestrator/main.py` (L94):

| URL Type | Example URL | Document ID |
|---|---|---|
| Standard B2B domain | `https://www.techcrunch.com/article` | `techcrunch.com` |
| Social — Reddit | `https://reddit.com/r/Entrepreneur/...` | `reddit.com/r/Entrepreneur` |
| Social — LinkedIn | `https://linkedin.com/company/openai` | `linkedin.com/company/openai` |
| Social — Facebook | `https://facebook.com/groups/saas` | `facebook.com/groups/saas` |
| Malformed / unknown | `javascript:void(0)` | `unknown` — **never written** |

**Rule:** Social domains (`reddit.com`, `facebook.com`, `linkedin.com`, `quora.com`, `kaggle.com`, `instagram.com`, `twitter.com`, `x.com`, `youtube.com`) use **domain + up to 2 path segments** to distinguish communities on the same platform. All other domains use root domain only.

### 24.3 Document Schema

```json
{
  "base_path":       "techcrunch.com",
  "baseline_weight": 1.15,
  "total_yield":     73,
  "last_seen":       "<SERVER_TIMESTAMP>",
  "last_decayed":    "<SERVER_TIMESTAMP>"
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `base_path` | `string` | (equals doc ID) | Redundant self-reference for Firestore Console readability. |
| `baseline_weight` | `float` | `1.0` | Exploit routing multiplier. `> 1.0` → exploit bucket. `< 1.0` → explore bucket. Modified by RLHF and decay cron. |
| `total_yield` | `integer` | `1` (on creation) | Running count of validated leads ever written to `predictive_cache` sourced from this domain. Burn-in guard: RLHF weight adjustments only apply when `total_yield >= 50`. |
| `last_seen` | `Timestamp` | `SERVER_TIMESTAMP` | Last time this domain produced a validated lead. Informational only — not queried. |
| `last_decayed` | `Timestamp` | absent until first decay | Written exclusively by the monthly decay cron. |

### 24.4 Service Dependency Map

#### Writers

| Service | Operation | Trigger |
|---|---|---|
| `autonomous-engine` | **Upsert** — `set()` (new) or `update(total_yield++, last_seen)` (existing) | Every successful `predictive_cache` write in `_validate_and_cache()` |
| `orchestrator` | **Partial update** — `update(baseline_weight += delta)` | CRM RLHF hook: `PUT /api/leads/{id}` with `crm_status = won / negotiating / lost`, gated on `total_yield >= 50` |
| `orchestrator` | **Partial update** — decay math on `baseline_weight`, writes `last_decayed` | Monthly decay cron: `POST /api/internal/cron/ontology-decay` |

#### Readers

| Service | Operation | Purpose |
|---|---|---|
| `autonomous-engine` | `db.collection("ontology_map").document(domain).get()` | Reads `baseline_weight` and `total_yield` to route domain into exploit or explore bucket |
| `orchestrator` | `db.collection('ontology_map').document(base_path_key).get()` | Reads `total_yield` inside RLHF hook to check burn-in threshold before applying weight delta |
| `orchestrator` | `db.collection('ontology_map').stream()` | Full-collection scan in monthly decay cron |

> `pipeline-main`, `scraper-heavy`, `digital-twin-engine`, `shadow-learner-aggregator`, `whatsapp-webhook`, and `email-summary` have **no interaction** with this collection.

### 24.5 RLHF Feedback Loop

#### Write 1 — Autonomous Engine Upsert (Primary)

Fires after every successful `LeadPayload` schema validation and `predictive_cache` write:

```python
# autonomous-engine/engine.py — _validate_and_cache(), lines 351-365
if ont_snap.exists:
    ont_ref.update({"total_yield": firestore.Increment(1), "last_seen": SERVER_TIMESTAMP})
else:
    ont_ref.set({"base_path": base_path, "total_yield": 1,
                 "baseline_weight": 1.0, "last_seen": SERVER_TIMESTAMP})
```

#### Write 2 — RLHF Reward / Penalty (orchestrator)

Fires when a CRM outcome is recorded on a lead that has passed the burn-in guard:

```python
# orchestrator/main.py — PUT /api/leads/{id}, lines 1646-1680
if crm_status in ["won", "negotiating"]: delta_weight = +0.15
elif crm_status == "lost":               delta_weight = -0.05

# Burn-in guard: sparse data domain — skip math, log only
if total_yield >= 50:
    ontology_ref.update({"baseline_weight": firestore.Increment(delta_weight)})
```

**Asymmetry:** Rewards (`+0.15`) are deliberately 3× the magnitude of penalties (`-0.05`). This biases the system toward continued exploration of weakly-performing domains rather than premature exclusion.

#### Write 3 — Monthly Decay Cron (orchestrator)

Applies regression-to-mean on `baseline_weight` across every document:

```
new_weight = weight - (weight - 1.0) * 0.10
```

| Before | After |
|---|---|
| `2.0` | `1.900` |
| `1.5` | `1.450` |
| `0.8` | `0.820` |
| `1.0` | skipped (no write) |

Documents at `baseline_weight ≈ 1.0` (`|diff| < 0.001`) are skipped to avoid unnecessary Firestore write costs.

### 24.6 Routing Logic (autonomous-engine)

```python
# final_score drives Gemini call gating
final_score = 2.0 * baseline_weight

if baseline_weight < 1.0 or total_yield == 0:
    → explore_domains  (threshold: final_score >= 1.4, token-budgeted at 15%)
else:
    → exploit_domains  (threshold: final_score >= 1.8, unlimited)
```

### 24.7 Criticality & Fallback Behaviour

| Failure | Behaviour |
|---|---|
| Document does not exist for a domain | `baseline_weight` defaults to `1.0`, `total_yield` defaults to `0` → domain routed to explore bucket. **Pipeline does not crash.** |
| Firestore read raises an exception | **Phase 7 Fix (2026-04-14):** Caught by `try/except` in `engine.py`. Warning logged (`[ONTOLOGY] Firestore read failed`). Defaults applied. Tenant run cycle continues. |
| RLHF write fails (`orchestrator`) | Caught by `except Exception as re: print(...)`. CRM status update still applied to lead. Only the weight adjustment is silently lost. |
| Decay cron stream fails | Returns HTTP 500 to Cloud Scheduler. Cloud Scheduler retries per configured policy. |

### 24.8 Key Design Invariants

1. **No TTL on `ontology_map` documents.** They are permanent. Documents accumulate over time — one per unique domain ever seen. The decay cron prevents `baseline_weight` from drifting unboundedly, but never deletes documents.
2. **`parse_base_path()` must remain identical in both `autonomous-engine` and `orchestrator`.** If the two implementations drift, RLHF writes will target different document IDs than the routing reads — breaking the feedback loop silently.
3. **Burn-in guard is hardcoded at `total_yield >= 50`.** It is not configurable via `system_config`. For low-volume tenants, this threshold may never be reached, permanently locking them out of RLHF adjustments.
4. **No composite index is required or deployed.** All reads are direct lookups or full-collection streams. The `(baseline_weight, total_yield)` composite index was removed on 2026-04-14 as it was declared but never queried.

---

## 25. V22 ARCHITECTURE — PROPRIETARY INTENT ENGINE

*Released: 2026-04-17 | Commit: `6f60251` | Branch: main (PROD-FROZEN)*

This section documents the three net-new architectural paradigms introduced in the V22 production freeze. Together, they eliminate the platform's dependency on pure LLM query generation, replace manual routing with a self-improving mathematical heuristic network, and add a real-time financial intelligence layer visible to tenants.

---

### 25.1 The Hybrid Starter Motor & Global Heuristic Engine

**Objective:** Remove reliance on Gemini for cold-start query generation by building a mathematical, self-learning buyer-syntax network in BigQuery. Gemini is retained as the fallback "starter motor" for low-confidence personas.

#### 25.1.1 BigQuery Dataset & Tables

**Dataset:** `swarm_analytics` (GCP Project: `sideio-leads-v16`)

**Table 1: `Intent_Keywords`** — Buyer-syntax confidence graph

```sql
CREATE TABLE IF NOT EXISTS `sideio-leads-v16.swarm_analytics.Intent_Keywords` (
    persona_category  STRING    NOT NULL,
    n_gram            STRING    NOT NULL,
    occurrence_count  INT64     NOT NULL DEFAULT 1,
    yield_weight      FLOAT64   NOT NULL DEFAULT 1.0,
    last_updated      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY DATE(last_updated)
OPTIONS (partition_expiration_days = 365);
```

| Column | Purpose |
|---|---|
| `persona_category` | ICP bucket — scopes n-grams to a Persona (e.g., "Enterprise SaaS Decision Makers") |
| `n_gram` | 2- or 3-gram buyer-syntax phrase extracted from approved lead text (e.g., "struggling with churn") |
| `occurrence_count` | Raw frequency — how many times this phrase appeared in approved leads |
| `yield_weight` | Confidence mass. Increments by `+0.1` per approval. Threshold: `SUM >= 1000` triggers STATISTICAL BUILD mode. |

#### 25.1.2 The Shadow Tracker — Asynchronous Accumulator

**Trigger:** Every `PUT /api/leads/{id}` with `status: approved` in `orchestrator/main.py`

**Contract:** Always fire-and-forget. The HTTP 200 to the UI must never wait for BigQuery.

```python
# orchestrator/main.py
def _async_shadow_track(lead_doc: dict, persona_category: str, tenant_id: str):
    """Fire-and-forget daemon. Thread spawned and immediately detached."""
    t = threading.Thread(
        target=_do_shadow_track,
        args=(lead_doc, persona_category, tenant_id),
        daemon=True   # dies if main process exits — no orphan threads
    )
    t.start()

def _do_shadow_track(lead_doc, persona_category, tenant_id):
    try:
        text = f"{lead_doc.get('pain_point', '')} {lead_doc.get('dm', '')}"
        ngrams = _extract_ngrams(text, n_sizes=[2, 3])
        bq = bigquery.Client(project=PROJECT_ID)
        for gram in ngrams:
            # MERGE (upsert) — safe for concurrent Approvals
            bq.query(UPSERT_NGRAM_SQL, job_config=...).result(timeout=10.0)
        print(f"[SHADOW TRACKER] Upserted {len(ngrams)} n-grams for persona={persona_category}")
    except Exception as e:
        # BQ timeout or schema error NEVER propagates upstream
        print(f"[SHADOW TRACKER] Non-blocking insert failed: {e}")
```

#### 25.1.3 Confidence Threshold Router

**Location:** `pipeline-main/main.py::generate_smart_query`

**Configuration:** `system_config/router` Firestore document — field `confidence_threshold` (default: `1000`). Adjustable at runtime without redeployment.

```
Confidence Score = SUM(yield_weight) FROM Intent_Keywords WHERE persona_category = X

IF score >= threshold:  → STATISTICAL BUILD  (pure BQ math, zero Gemini)
IF score < threshold:   → GEMINI_FALLBACK    (LLM starter motor)
```

**Hardening:** The BQ confidence query runs inside `concurrent.futures.ThreadPoolExecutor` with a strict `timeout=3.0`. If BQ exceeds 3 seconds, the circuit breaker trips and the router **defaults to GEMINI_FALLBACK** — the pipeline never blocks.

**Cold-Start Guarantee:** Every new Persona starts at `confidence = 0` → routes to GEMINI_FALLBACK. As the tenant approves leads, the Shadow Tracker accumulates weight. The system becomes self-sufficient asymptotically.

---

### 25.2 The Negative Knowledge Graph (BigQuery Exclusion Matrix)

**Objective:** Build a self-updating suppression list that prevents competitors, authors, and noise domains from re-entering the pipeline indefinitely. The shield learns from every rejection.

#### 25.2.1 BigQuery Table: `Negative_Signals`

```sql
CREATE TABLE IF NOT EXISTS `sideio-leads-v16.swarm_analytics.Negative_Signals` (
    entity_name       STRING    NOT NULL,
    root_domain       STRING    NOT NULL,
    rejection_reason  STRING    NOT NULL,  -- Enum: "Competitor" | "Author"
    tenant_id         STRING    NOT NULL,  -- "GLOBAL" for L0 admin overrides
    timestamp         TIMESTAMP NOT NULL   DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY DATE(timestamp)
OPTIONS (partition_expiration_days = 730);   -- 2-year retention
```

**Scope:** Tenant-scoped signals (`tenant_id = uid`) apply only to that tenant's queries. Global signals (`tenant_id = 'GLOBAL'`) are written by L0 admins and apply cross-tenant.

#### 25.2.2 The RLHF Rejection Hook

**Trigger:** `PUT /api/leads/{id}` with `rejection_reason: "Competitor"` or `rejection_reason: "Author"`.

```python
# orchestrator/main.py — PUT /api/leads/{id}
if rejection_reason and rejection_reason.lower() in NEG_SIGNAL_REASONS:
    _async_neg_signal_insert(
        entity_name=lead_doc.get("company_name", ""),
        root_domain=extract_root_domain(lead_doc.get("url", "")),
        rejection_reason=rejection_reason,
        tenant_id=tenant_id
    )

NEG_SIGNAL_REASONS = frozenset({"competitor", "author"})
```

**Async contract:** `_async_neg_signal_insert` spawns a daemon thread (`_do_neg_signal_insert`) and returns immediately. The 200 OK to the UI is never delayed. The BQ streaming insert runs asynchronously; failures are logged but never re-raised.

#### 25.2.3 The Serper Shield — Query-Level Suppression

**Location:** `pipeline-main/main.py::_fetch_neg_shield` → called before every `generate_smart_query` execution.

```python
def _fetch_neg_shield(tenant_id: str) -> tuple[list[str], list[str]]:
    """
    Returns (blocked_domains, blocked_entities).
    Hard 3-second circuit breaker via concurrent.futures.
    Falls back to ([], []) on ANY failure — pipeline never blocks.
    """
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(bq.query, FETCH_NEG_SHIELD_SQL, job_config=...)
            job = fut.result(timeout=3.0)           # ← 3s hard timeout
        rows = list(job.result(timeout=3.0))
        blocked_domains  = list({r["root_domain"]  for r in rows if r["root_domain"]})
        blocked_entities = list({r["entity_name"]  for r in rows if r["entity_name"]})
        return blocked_domains[:20], blocked_entities[:20]  # capped at 20 each
    except concurrent.futures.TimeoutError:
        print("[NEG SHIELD] BQ timeout (>3s) — proceeding without shield.")
        return [], []
    except Exception as e:
        print(f"[NEG SHIELD] Fetch failed (non-fatal): {e}")
        return [], []
```

**Injection into query blacklist:**
```python
blocked_domains, blocked_entities = _fetch_neg_shield(tenant_id)

# Appended to every Serper query's blacklist string
neg_domain_ops  = " ".join(f"-site:{d}"          for d in blocked_domains)
neg_entity_ops  = " ".join(f"-intitle:\"{e}\""   for e in blocked_entities)
blacklist += f" {neg_domain_ops} {neg_entity_ops}"
```

**Result:** Rejected competitor domains are **permanently excluded from all future Serper queries** for that tenant — and from global queries when `tenant_id = 'GLOBAL'`. The suppression list is self-expanding, requiring zero manual maintenance.

---

### 25.3 The L1 ROI & Analytics Matrix

**Objective:** Provide tenants with a real-time, financially credible dashboard that quantifies the exact dollar value Sideio Lead Sniper generates. Uses conservative industry benchmarks (HubSpot CPL study, BLS SDR wage survey) as defaults to maintain trust.

#### 25.3.1 Firestore Schema Update: `users/{tenant_id}.unit_economics`

Added to the `users` document in V22. Populated via `PUT /api/analytics/unit-economics`.

```json
{
  "unit_economics": {
    "avg_cpl":              50,     // Cost-per-lead benchmark (USD). Source: HubSpot State of Marketing
    "sdr_hourly_rate":      15,     // SDR hourly wage (USD). Source: BLS Occupational Outlook
    "avg_deal_size":        0,      // Average closed deal value. Default 0 = credibility guard (see §25.3.3)
    "est_conversion_rate":  0.02,   // Lead-to-close rate. Default 2%. Source: Salesforce Benchmarks
    "currency":             "USD"   // Supported: USD, INR, GBP, EUR, AUD, SGD, AED
  }
}
```

**Field rules:**
- `avg_deal_size: 0` is the default. Pipeline Value metric stays `$0` until the tenant explicitly sets a non-zero value. This is a **deliberate credibility guard** — the system refuses to project pipeline revenue using an unverified ADS.
- Currency does not affect calculation logic; it is used only by the frontend formatter.

#### 25.3.2 API Endpoints

**`GET /api/analytics/roi`**

```
Query param: ?date_range=N  (default: 30, in days)
Auth:        Bearer <Firebase ID Token>
```

Execution:
1. Queries `leads` collection: `WHERE tenant_id == X AND status == "converted" AND updatedAt >= now - N days`
2. Reads `unit_economics` from `users/{tenant_id}` (falls back to defaults if absent)
3. Computes all four metrics
4. Returns full payload

Response schema:
```json
{
  "metrics": {
    "n_approved":      42,
    "ad_savings":      2100.00,
    "labor_savings":   157.50,
    "total_offset":    2257.50,
    "pipeline_value":  0.00,
    "roi_ratio":       4.5
  },
  "unit_economics": {
    "avg_cpl": 50, "sdr_hourly_rate": 15,
    "avg_deal_size": 0, "est_conversion_rate": 0.02, "currency": "USD"
  },
  "date_range_days":   30,
  "generated_at":      "2026-04-17T04:00:00Z"
}
```

---

**`PUT /api/analytics/unit-economics`**

```
Body: { "avg_cpl": 65, "avg_deal_size": 12000, "sdr_hourly_rate": 20,
        "est_conversion_rate": 0.03, "currency": "INR" }
Auth: Bearer <Firebase ID Token>
```

Execution:
1. Validates all fields (numeric range checks)
2. `users/{tenant_id}.set({ "unit_economics": payload }, merge=True)` — atomic Firestore update

#### 25.3.3 Mathematical Models

All four metrics are computed server-side in `GET /api/analytics/roi`. `N_approved = count of converted leads in window`.

| Metric | Formula | Benchmark Basis |
|---|---|---|
| **Ad Spend Offset** | `N_approved × avg_cpl` | HubSpot: avg B2B CPL = $50–$75 |
| **Labor Savings** | `(N_approved × 15 min / 60) × sdr_hourly_rate` | 15 min = avg manual SDR time per lead (BLS study) |
| **Total Value Offset** | `ad_savings + labor_savings` | Combined elimination of two variable cost lines |
| **Pipeline Value** | `N_approved × est_conversion_rate × avg_deal_size` | **$0 if `avg_deal_size == 0`** — credibility guard |
| **ROI Ratio** | `total_offset / (N_approved × $0.10)` | `$0.10` ≈ estimated Sideio cost per approved lead |

**Credibility Guard Design Note:** `pipeline_value` is the highest-magnitude metric and the most tempting to inflate. The system **deliberately keeps it at $0 until the tenant provides their actual ADS**. This prevents the dashboard from showing aspirational revenue numbers that the tenant's finance team cannot validate — a common complaint with AI-generated ROI claims.

#### 25.3.4 Frontend ROI Dashboard Module (`public/app.js`)

New client-side module appended to `app.js` in V22.

| Function | Purpose |
|---|---|
| `loadROIDashboard(dateRange)` | Calls `GET /api/analytics/roi`, renders 4 hero cards with shimmer loading |
| `animateCounter(el, val, currency)` | 900ms `easeOutExpo` counter animation on all card values |
| `formatROICurrency(amount, currency)` | Maps USD/INR/GBP/EUR/AUD/SGD/AED with K/M abbreviation |
| `openUnitEconomicsModal()` | Pre-fills modal from `window._roiLastUE` (last API response cache) |
| `saveUnitEconomics()` | `PUT /api/analytics/unit-economics` → recalculates → closes modal after 1.2s |

**Auto-load:** `loadROIDashboard(30)` is called in `loadDashboard()`'s `Promise.all` alongside `loadMe()`, `loadCampaigns()`, `loadLeads()` — all four execute in parallel on login.

**4 Hero Cards rendered:**

| Card | ID | Sub-label source |
|---|---|---|
| Ad Spend Saved | `roi-ad-savings` | `{currency} {avg_cpl}/lead × {N} approved` |
| Labor Hours Saved | `roi-labor-savings` | `at {currency} {sdr_rate}/hr SDR rate` |
| Total Value Offset | `roi-total-offset` | ROI ratio vs. Sideio cost |
| Pipeline Value | `roi-pipeline-value` | `"⚙️ Set avg deal size to unlock"` if `avg_deal_size == 0` |

**Date range selector:** `#roi-range-select` dropdown (7d / 30d / 90d / All Time) calls `loadROIDashboard(value)` on change.

---

### 25.4 V22 Amputation Record — Legacy Feature Removal

The following features were **permanently removed** in commit `6f60251` on 2026-04-17.

| Feature | Removed From | Lines Removed | Root Cause for Removal |
|---|---|---|---|
| "Suggest up to 10 websites" textarea | `public/index.html` L990-994 (edit-campaign-modal) | 5 | UI field exposed a pipeline vector that was actively degrading query quality |
| `target_urls` DOM read + `slice(0,10)` | `public/app.js` L905-910 (saveCampaignAction DOM path) | 6 | Dead code — textarea removed |
| `target_urls` in PUT payload | `public/app.js` L863 (saveEditedCampaign) | 1 | Payload field no longer needed |
| `target_urls` in POST payload | `public/app.js` L963 (saveCampaignAction) | 1 | Same |
| `target_urls: []` in `deployPredictiveCard` | `public/app.js` L3016 | 1 | Same |
| `target_urls: []` in `saveChildCampaign` | `public/app.js` L3139 | 1 | Same |
| `urlsEl` pre-fill in `openEditModal` | `public/app.js` L836-840 | 5 | DOM element no longer exists |
| **`site:domain1 OR site:domain2` injection loop** | `services/pipeline-main/main.py` L1346-1352 | **7** | **Root cause: this loop was injecting user-submitted domains into Serper queries, overriding the intent keywords generated by the Hybrid Starter Motor. Google's SERP API was silently dropping the N-gram operators when the query exceeded token limits due to the domain expansion. Amputating this loop immediately restored full N-gram signal fidelity.** |

**Total dead code removed: 27 lines across 3 files.**

**Important:** The `target_urls` field **still exists** in legacy Firestore `campaigns` documents and in the backend `PUT /api/campaigns/{id}` handler (it is stored if sent by a client). It is simply **never read by the producer** (`pipeline-main/main.py::produce`). This is a deliberate soft migration — no Firestore migration script is required.

---

### 25.5 V22 Design Invariants (Additions to Section 16 + 22)

15. **The Serper query builder has no user-controlled domain injection.** `target_urls` is permanently removed from the `produce()` data path. Any future attempt to re-introduce manual URL injection must go through the Negative Shield mechanism, not via direct `site:` operator injection in the query string.

16. **Shadow Tracker threads are daemon threads.** They must always be spawned with `daemon=True`. If the Flask worker process exits (e.g., Cloud Run scale-to-zero), daemon threads are killed immediately — preventing leaked BQ connections that could hold open billing sessions.

17. **BQ Negative Shield has a hard 3-second ceiling.** The `_fetch_neg_shield` function **must always** be called inside `concurrent.futures.ThreadPoolExecutor` with `timeout=3.0`. If this timeout is removed or extended, BQ latency will directly propagate into Serper query latency, potentially pushing the producer past Cloud Tasks' 10-minute deadline.

18. **`confidence_threshold` is Firestore-configurable at `/system_config/router`.** Changing this value does not require a code deploy — the pipeline reads it at the start of each `generate_smart_query` call. Default: `1000`. Raising it biases toward Gemini longer; lowering it accelerates STATISTICAL BUILD adoption.

19. **Pipeline Value stays `$0` until `avg_deal_size > 0`.** The backend enforces this: `pipeline_value = 0 if unit_economics.avg_deal_size == 0 else (N * rate * ADS)`. The frontend enforces this: the `roi-pipeline-sub` element shows `"⚙️ Set avg deal size to unlock"` when the value is zero. Both guards must be maintained in sync.

20. **`unit_economics` defaults are industry benchmarks, not arbitrary values.** If you update the defaults, document the source. Current defaults:
    - `avg_cpl: 50` → HubSpot State of Marketing Report 2024 (B2B avg CPL)
    - `sdr_hourly_rate: 15` → US BLS SDR Median Wage 2024
    - `est_conversion_rate: 0.02` → Salesforce State of Sales 2024 (avg lead-to-close rate)
    - `avg_deal_size: 0` → Credibility guard (intentional zero, not a benchmark)

---

### 25.6 V24 Releases (V24.1.18 - V24.1.20) — Query Precision & Inbound Radar Geo Fixes

The following features and hotfixes were introduced in June 2026 under versions **V24.1.18**, **V24.1.19**, and **V24.1.20**:

#### 25.6.1 OSINT Boolean Precedence & Spacing Optimizer (V24.1.18)
* **Parenthetical OR Grouping Mandate**: Enforced strict parenthetical bounds on all `OR` clauses in both B2B and B2C prompts in `services/pipeline-main/services/query_brain.py` to prevent Google Search implicit `AND` precedence from diluting query results.
* **Regex Post-Sanitizer**: Added `_clean_query_syntax()` in `query_brain.py` to automatically correct missing spaces before opening parentheses (e.g., `"phrase"(Group)` -> `"phrase" (Group)` and `site:boards.net(...)`) and strip wildcard prefixes in `site:` operators (e.g. `site:*.org` -> `site:.org`).
* **Serper Space Stripping Fix**: Updated `sanitize_query` in `services/pipeline-main/services/serper_service.py` to preserve space separation before opening parentheses `(` during query token reassembly.

#### 25.6.2 Inbound Radar Geo Targeting & GMB Review Hotfixes (V24.1.19)
* **Dynamic Geo-Targeting**: Updated `InboundSentimentService` (`services/orchestrator/services/inbound_sentiment_service.py`) and `InboundMapsService` (`services/orchestrator/services/inbound_maps_service.py`) to resolve a critical geo leak that hardcoded US indexing (`"gl": "us"`). The services now dynamically fetch the campaign's target country and location (`gl` and `location`).
* **Noise and Competitor Filtering**:
  - Expanded `GLOBAL_NEGATIVE` in the Inbound Sentiment queries to prune SEO directories, blogs, listicles, and general informational wiki posts.
  - Hardened the Gemini scoring prompt in `inbound_sentiment_service.py` with a strict general `SELLER EXCLUSION RULE` (excluding local competitors/agents across all industries) and an **Informational Filter** to classify generic guide pages as `NONE` with a score of `0.0`.

#### 25.6.3 Universal Query Spacing Hardening (V24.1.20)
* **Inbound Radar Query Cleansing**: Integrated the `_clean_query_syntax` spacing corrector into `InboundSentimentService._search_serper` to format and sanitize all radar queries before executing them on Serper, ensuring no database-polluted or template-generated queries leak spacing syntax bugs.
* **Pipeline-Main Safety Net**: Applied `_clean_query_syntax` universally to the final output list of `generate_smart_query` in `query_brain.py` to clean all assembled B2B and B2C queries.
* **Paid-Tier Spacing Safety**: Hardened `sanitize_query` in `serper_service.py` to run spacing corrections even when `SERPER_PAID_TIER=true` (which skips token reassembly), ensuring all queries remain syntactically clean on the search engine.

---

*Architecture document: V24.1.20 release updated.*

