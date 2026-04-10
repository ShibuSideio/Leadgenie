import os

v18_addendum = """
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
"""

with open('architecture.md', 'r', encoding='utf-8') as f:
    existing_text = f.read()

# Replace the version strings at the top
existing_text = existing_text.replace(
    '*Last Updated: 2026-04-09 | Version: V17 — Conversational UX + V16 Autonomous Engine + Epsilon-Greedy Router*',
    '*Last Updated: 2026-04-10 | Version: V18 / V19 — Predictive Campaign Engine + Autonomous Generation Pipeline*'
)
existing_text = existing_text.replace(
    '# Lead Sniper / Sideio Smart Growth — V17',
    '# Lead Sniper / Sideio Smart Growth — V18 / V19'
)

# Append the new section
with open('architecture.md', 'w', encoding='utf-8') as f:
    f.write(existing_text + "\n" + v18_addendum)
    
print("architecture.md successfully updated with V18/V19 additions.")
