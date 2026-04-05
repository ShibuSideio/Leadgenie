# Lead Sniper / Sideio Smart Growth (V12.99.1)
**Full Technical Specification Document (TSD)**

---

## 1. REPOSITORY DIRECTORY TREE

The project operates out of a monolithic repository deploying to distributed microservices.

```bash
/sideio_leads
├── /public                     # React Frontend / Firebase Static Hosting
│   ├── index.html              # Main DOM scaffolding & Auth initialization
│   ├── app.js                  # Deep logic: Routing, UI Binding, DOM sorting, L0 Governance
│   ├── index.css               # Styling and layout semantics
│   └── 404.html                # Fallback routing
├── /services                   # Microservice Backend
│   ├── /orchestrator          # Cloud Run REST API / DB Operations
│   │   ├── Dockerfile
│   │   ├── main.py             # Event listener, wallet checks, GCP Task Queuing, RLHF tracking
│   │   └── requirements.txt
│   ├── /pipeline-main         # Cloud Run Worker / AI Extraction Engine
│   │   ├── Dockerfile
│   │   ├── main.py             # Core pipeline: Symptom Dorking, Intent Filtering, Vertex AI
│   │   └── requirements.txt
│   └── /scraper-heavy         # Cloud Run Playwright Container
│       ├── Dockerfile
│       ├── main.py             # Playwright async chromium logic, explicit resource aborts
│       └── requirements.txt
├── .firebaserc
├── firebase.json
├── firestore.rules
├── firestore.indexes.json      # Maps composite index for: tenant_id ASC, status IN
├── cloudbuild.yaml             # CI/CD instructions
└── ARCHITECTURE.md             # This document
```

---

## 2. EXHAUSTIVE FIRESTORE SCHEMA

### 2.1 The `users` Collection
The tenant anchor containing strict L0 definitions, monetization quotas, and RLHF weights.

```json
{
  "_id": "uid_from_firebase_auth",
  "email": "user@example.com",
  "role": "admin", // "super_admin" strictly grants L0 Dashboard access
  "tenant_id": "uid_from_firebase_auth",
  "agreed_to_terms": "2026-04-01T12:00:00Z", // Required for platform access
  "crm_webhook_url": "https://hooks.zapier.com/hooks/catch/...", 
  "wa_token": "gAAAAAB...", // Encrypted via Fernet in memory
  "wa_phone_id": "123456789",
  "admin_phone": "13125550199",
  "is_active": true,
  "approval_status": "approved", // Enum: pending, approved, rejected
  "wallet": {
    "allocated_credits": 20000,
    "consumed_credits": 314
  },
  "preferences_weights": {
    "hiring_intent": 2,
    "tech_wordpress": -5,
    "tech_react": 1
  },
  "createdAt": "2026-03-01T12:00:00Z",
  "updatedAt": "2026-04-05T12:00:00Z"
}
```

### 2.2 The `campaigns` Collection
The contextual framework for lead mapping.

```json
{
  "_id": "camp_uuid_789",
  "tenant_id": "uid_from_firebase_auth",
  "name": "Q3 Commercial Cleaning Push",
  "bio": "We offer B2B janitorial services for offices.",
  "status": "active", // Enum: active, paused
  "target_location": "Austin, TX",
  "keywords": ["facility management", "office cleaning"],
  "leads_generated": 105,
  "createdAt": "2026-03-15T12:00:00Z"
}
```

### 2.3 The `leads` Collection
The fundamental atomic execution target strictly generated and evolved by the pipeline logic.

```json
{
  "_id": "hashlib_sha256_hash(tenant_id_root_domain)",
  "tenant_id": "uid_from_firebase_auth",
  "matched_campaigns": ["camp_uuid_789", "camp_uuid_101"], // Deduplication Array
  "url": "https://techcorp.com",
  "status": "new", // Enum: new, contacted, ignored, failed, processing, completed
  "score": 8, // Derived 1-10 Vertex AI Fit Score
  "pain_point": "Complaining about high turnover on LinkedIn.",
  "icebreaker_angle": "Focus on facility hygiene boosting employee retention.",
  "dm": "Hey Name, noticed... ",
  "hiring_intent_found": "Yes",
  "tech_stack_found": ["react", "hubspot"],
  "email": "hr@techcorp.com",
  "phone": "3125550199",
  "linkedin": "https://linkedin.com/in/...",
  "error": null, // Populated explicitly on "failed" status
  "interactions": [
    {
      "action": "status_ignored",
      "date": "2026-04-05T14:30:00Z"
    }
  ],
  "createdAt": "2026-04-05T14:00:00Z",
  "updatedAt": "2026-04-05T14:30:00Z"
}
```

---

## 3. THE 7-STEP PIPELINE EXECUTION FLOW (Code-Level)

### Step 1: Orchestrator Trigger
**Location:** `services/orchestrator/main.py::execute_campaign`
- **Execution:** Handles `POST /api/campaigns/{id}/execute` mapped from the frontend "Find Clients" button.
- **Verification:** Natively pulls `user_ref = db.collection("users").document(tenant_id)` and evaluates `wallet_balance = wallet.get("allocated_credits", 0) - wallet.get("consumed_credits", 0)`. Strict bypass mapping checks `if role == "super_admin"` to skip wallet restrictions.

### Step 2: Cloud Task Queuing
**Location:** `services/orchestrator/main.py::trigger_pipeline_worker`
- **Execution:** Constructs a Google Cloud Task JSON execution pointing natively to `pipeline-main`.
- **Payload:**
```json
{
  "http_request": {
    "http_method": "POST",
    "url": "https://pipeline-main-abc.a.run.app/process",
    "headers": { "Content-Type": "application/json" },
    "body": "base64_encoded_mapping({\"tenant_id\": \"...\", \"campaign_id\": \"...\"})",
    "oidc_token": { "service_account_email": "tasks-invoker@..." }
  }
}
```

### Step 3: Symptom Dorking
**Location:** `services/pipeline-main/main.py::generate_smart_query`
- **Execution:** Generates the contextual B2B constraints utilizing Vertex AI `gemini-2.5-flash`.
- **Exact Prompt:**
```text
The user solves this business problem: '{bio}'. Generate 3 highly specific Google Search operators to find targets PUBLICLY EXPERIENCING this problem. 
Rule 1: You MUST include at least one query targeting social/professional networks using 'site:linkedin.com', 'site:facebook.com', or 'site:reddit.com'. 
Rule 2: You MUST append negative keywords to exclude retail/informational sites (e.g., '-shop -cart -amazon -wiki'). 
Return ONLY a JSON list of 3 strings.
```

### Step 4: Serper Execution
**Location:** `services/pipeline-main/main.py::search_serper`
- **Execution:** Appends location dynamically.
- **Exact Payload:** Map structure executed to `google.serper.dev/search`:
```python
payload_dict = {"q": f"{base_query} AND {campaign_location}", "num": 20, "location": campaign_location, "gl": country_code}
```

### Step 5: B2B Value-Chain Gate
**Location:** `services/pipeline-main/main.py::pre_filter_gemini`
- **Execution:** Before executing Playwright operations, the raw search snippet list passes through a secondary Gemini analysis strictly designed to block SEO storefront leakage.
- **Exact Prompt:**
```text
CRITICAL INTENT CHECK: Read the user's bio: '{bio}'. Is the website EXPERIENCING the problem the user solves, or are they SELLING a solution to it? You MUST reject any URL that is an SEO blog, a competitor, or a direct-to-consumer (D2C) retail catalog. Only approve targets that match the user's intended value chain.

CRITICAL: Evaluate the business location. If the target location is '{location_target}', and this website explicitly serves a different geographic region (e.g., a Dubai business for a Kochi search), you MUST reject the URL immediately. Return a failed state.

YOUR OUTPUT MUST BE STRICTLY A LINE-BY-LINE LIST OF ONLY URLs matching high-value leads.
```

### Step 6: Heavy Scraping
**Location:** `services/scraper-heavy/main.py::fetch_page_content`
- **Execution:** Active instantiation.
- **Interception Logic:** 
```python
await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font", "stylesheet"] else route.continue_())
await page.goto(url, timeout=15000, wait_until="domcontentloaded")
```
- **Contact Extraction:** 
Native DOM interaction avoiding string-scrape limits:
```javascript
document.querySelectorAll('a[href^="mailto:"]').forEach(...)
```

### Step 7: Final Enrichment & DM Drafting
**Location:** `services/pipeline-main/main.py::final_score_and_dm`
- **Execution:** Reads truncated DOM content and generates final fields.
- **Exact Prompt:**
```text
You are an Elite B2B Profiler. Score this lead 1-10 based on campaign goals and product bio: '{bio}'.

You MUST extract contact information. You MUST identify a specific human decision-maker. If the extracted text is just a generic corporate homepage, an advertisement, or lacks a specific human contact, you MUST score it 0.

For the "hiring_intent_found" field: Return ONLY the string 'Yes' or 'No'. Do not include any explanation, context, or reasoning. If unknown, return 'No'.

Provide a JSON object with: score, dm, pain_point, icebreaker_angle, hiring_intent_found.
```

---

## 4. RLHF & SELF-LEARNING (Function Mapping)

The system is self-optimizing out-of-the-box utilizing existing reads strictly bound to native database sequences.

### The UI Trigger Loop
**Location:** `services/orchestrator/main.py` lines 448-471 `elif request.path.startswith("/api/leads/")`.
When the UI triggers `Ignore` or `Converted`, the orchestrator natively executes mathematical backpropagation:
```python
delta = 1 if status == "converted" else -1
for tech in tech_stack:
    pref_updates[f"preferences_weights.tech_{tech}"] = firestore.Increment(delta)
```

### Function Map A: Generative Historical Mining
**Location:** `services/pipeline-main/main.py` inside `generate_smart_query()`.
The function synchronously pulls historical intent strings:
```python
# Fetches previous successes context
query = db.collection("leads").where("tenant_id", "==", tenant_id).where("status", "in", ["contacted", "converted"]).limit(20)
pain_points = [d.to_dict().get("pain_point", "") for d in docs]
prompt = f"Analyze these successful lead extractions. Extract exactly 3 short conceptual B2B phrases identifying high-value trends... Data: {json.dumps(pain_points)}"
# Result is mapped to historical_str and appended directly to the search query.
```

### Function Map B: The Interceptor Drop
**Location:** `services/pipeline-main/main.py` around line 413.
Blocks vertex AI credit depletion:
```python
fit_score = 0
for tech in tech_stack:
    fit_score += preferences_weights.get(f"tech_{tech}", 0)
if fit_score <= -3:
    print(f"[RLHF] Target {target_domain} logically dropped (Fit Score: {fit_score}).")
    doc_ref.delete()
    continue
```

---

## 5. ERROR HANDLING & STATE RECOVERY

To prevent silent lead isolation within the `"processing"` execution state, the pipeline actively wraps failure matrices at every layer of failure probability.

**Location:** `services/pipeline-main/main.py`

### 5.1 Scraper Fallback Defense
The heavy Playwright scraper utilizes a native REST fallback execution protected by a dual try-catch map guaranteeing it never breaks the parent executing thread on `TimeoutError` or connection saturation logic.
```python
    except Exception as e: # Parent lightweight exception
        try:
            heavy_resp = httpx.post(SCRAPER_HEAVY_URL, json={"url": url}, timeout=45)
            # ...
        except Exception as he:
            print(f"Heavy Scraper fatal crash for {url}: {he}")
        return "", [], [], [] # Safely proceeds with empty state
```

### 5.2 The Unified Loop Crash Handler
The entirety of the parsing, deduplication, cache routing, DB saving, and AI transcription blocks are completely enveloped natively. If any structural failure triggers inside the memory container, the lead document is explicitly flagged `"failed"` instead of hanging in `"processing"`.
```python
            try:
                # Cache check mapped here
                cache_ref = db.collection("scraped_cache").document(url.replace('/','_'))
                # All scraping, Vertex AI formatting, and doc updates
                # ...
            except Exception as loop_e:
                print(f"Pipeline execution crashed: {loop_e}")
                db.collection("leads").document(lead_id).update({
                    "status": "failed", 
                    "error": "Pipeline execution crashed"
                })
                continue
```

---

## 6. FRONTEND UX & TELEMETRY (React)

The Client UX focuses exclusively on native interaction rendering, converting machine telemetry directly into actionable execution funnels. Data binds locally dynamically from Firebase.

### 6.1 The Actionable Pipeline
*   **3-Part Analytics Funnel:** A high-level visual representation explicitly mapped to the `leads` array evaluating: 
    *   *Discovered Today:* Total documents generated in a 24-hr sequence.
    *   *Actionable:* Lead documents strictly evaluated as `status == 'new'`.
    *   *Ignored:* Lead documents mapped with `status == 'ignored'`.
*   **Semantic Tech Badges:** The React client identifies specific technology stack sets native to the extracted `tech_stack_found` array payload and overlays structured metadata Badges dynamically atop the HTML Card sequence (e.g., rendering the HubSpot or React icon).
*   **Competitor Intercept:** Native UI conditionals flag any URL domains matching known B2B aggregator entities implicitly.
*   **Single-Click Execution:** Deprecating the legacy Autonomous WhatsApp Auto-Send, the UI now features a `Copy Message` invocation. It actively copies the Generative AI drafted `dm` text to the user's native system Clipboard and immediately triggers a PUT mapping the lead to `"status": "contacted"`.
*   **CRM Webhook Push:** Integrating via `fetch(crm_webhook_url, { mode: 'no-cors' })`, the CRM Push button acts as a dynamic pass-through relay enabling Webhook.site and Zapier integrators immediately.

### 6.2 The L0 Super Admin Matrix
The system utilizes optimized localized structural sorting strictly to bypass Firebase cost overhead for tenant queries.
*   **Macro Analytics:** By querying `db.collection("leads").where(...).count()`, the `/api/l0/telemetry` endpoint calculates total global scale without ever executing thousands of document reads natively.
*   **In-Memory UI Sorting:** The L0 Governance telemetry table utilizes `window.sortL0Table('email' | 'wallet' | 'leads')` within Javascript. This intercepts the DOM table representation and inherently sorts the JSON arrays directly in the client's vRAM, entirely bypassing backend query execution costs.
