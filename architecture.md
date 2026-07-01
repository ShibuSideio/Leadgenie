# LeadGenie (Sideio) — Platform Architecture V24.5
**Technical Specification Document**
*Last Updated: 2026-07-01 | Version: V24.5.0 — Enterprise Remediation Complete*

---

## 1. SYSTEM OVERVIEW

LeadGenie is a fully automated, multi-tenant lead generation SaaS platform. It discovers, scores, and delivers hyper-personalised outreach messages for paying tenants — autonomously, 24/7, without manual input.

**Core loop:**
1. Cloud Scheduler cron hits the Orchestrator every 5 minutes
2. Orchestrator validates quota, checks drip cadence, enqueues a Cloud Task per active campaign
3. Pipeline-Main runs: query generation → Serper search → pre-filter → scrape → score → write to Firestore
4. The PWA frontend listens via `onSnapshot` and renders leads in real-time

**Supported business archetypes (sourcing vectors):**
- `B2B` — business-to-business; corporate buyer signals
- `B2C` — business-to-consumer; individual buyer pain signals
- `B2B2C` — dual ICP: institutional buyer + individual end-user (50/50 query split)
- `D2C` — direct-to-consumer brand; competitor comparison and product-switching signals

---

## 2. REPOSITORY STRUCTURE

```
/sideio_leads
├── /public                          # Firebase Static Hosting (PWA)
│   ├── index.html                   # DOM scaffolding, Firebase SDK init
│   ├── app.js                       # All frontend logic (~4,000 lines)
│   ├── styles.css                   # CSS design system
│   ├── sw.js                        # Service Worker (cache bust on deploy)
│   └── manifest.json                # PWA manifest
├── /services
│   ├── /orchestrator                # Cloud Run: REST API Gateway + Cron Dispatcher
│   │   ├── api/routers/             # Modular Flask blueprints (campaigns, leads, settings…)
│   │   ├── services/intelligence/   # shadow_tracker.py, neg_signal.py
│   │   ├── core/config.py           # Shared env-var config
│   │   └── requirements.txt
│   ├── /pipeline-main               # Cloud Run: AI Extraction Engine (Cartographer)
│   │   ├── api/routers/             # dispatch.py, produce.py
│   │   ├── core/constants.py        # CONSUMER_ARCHETYPES, D2C_ARCHETYPES, B2B2C_ARCHETYPES
│   │   ├── services/                # query_brain.py, serper_service.py, neg_shield.py,
│   │   │                            # prism_pipeline.py, gemini_service.py, telemetry.py
│   │   └── requirements.txt
│   ├── /scraper-heavy               # Cloud Run: Playwright headless browser
│   ├── /digital-twin-engine         # Cloud Run: Website analyser + market trend cache
│   ├── /shadow-learner-aggregator   # Cloud Run: RLHF swarm weight aggregator
│   ├── /autonomous-engine           # Cloud Run Job: nightly digital exhaust scraper
│   ├── /whatsapp-webhook            # Cloud Run: WhatsApp Business API receiver (DISABLED)
│   └── /email-summary               # Cloud Run: Email digest sender
├── /terraform                       # GCP infrastructure as code
├── .firebaserc                      # Firebase project binding
├── firebase.json                    # Hosting config + Firestore rules pointer
├── firestore.rules                  # Multi-tenant security rules
├── firestore.indexes.json           # Composite indexes
├── cloudbuild.yaml                  # CI/CD: parallelised enterprise pipeline
└── architecture.md                  # This document
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
**Vertex AI / Gemini:** `gemini-2.5-flash` via `google-genai` SDK

### Environment Variables

```bash
# Orchestrator & Pipeline-Main (shared)
PROJECT_ID=sideio-leads-v16
LOCATION=asia-south1
QUEUE=lead-pipeline-queue
PIPELINE_URL=https://lead-pipeline-main-222247989819.asia-south1.run.app/dispatch
ORCHESTRATOR_URL=https://orchestrator-222247989819.asia-south1.run.app
ENCRYPTION_KEY=<fernet_key>          # Fallback symmetric cipher

# Security (V24.2)
INTERNAL_CRON_SECRET=<secret>        # MANDATORY — 503 returned if unset

# Pipeline-Main extras
SCRAPER_HEAVY_URL=https://scraper-heavy-<hash>.a.run.app/scrape
PIPELINE_BASE_URL=https://lead-pipeline-main-<hash>.a.run.app

# Autonomous Engine
DAILY_GEMINI_BUDGET=1000
```

### Secret Manager Secrets

| Secret Name | Used By | Purpose |
|---|---|---|
| `serper_api_key` | pipeline-main | Serper.dev search API key |
| `FIREBASE_SA_KEY` | Cloud Build | Firebase deploy service account JSON |
| `kms_wa_key_path` | orchestrator, pipeline-main | KMS key ring path for WhatsApp token (feature disabled) |
| `DECODO_STANDARD_PROXY` | scraper-heavy | Standard rotating proxy URL |
| `DECODO_PREMIUM_PROXY` | scraper-heavy | Premium WAF-bypass proxy URL |

---

## 4. FIRESTORE DATABASE SCHEMA

### 4.1 `users` Collection
Primary tenant anchor. Document ID = Firebase Auth UID.

```json
{
  "tenant_id": "uid_from_firebase_auth",
  "email": "user@example.com",
  "role": "admin",
  "is_active": true,
  "approval_status": "approved",
  "beta_expiry": "2026-10-01T00:00:00Z",
  "agreed_to_terms": "<SERVER_TIMESTAMP>",
  "crm_webhook_url": "https://hooks.zapier.com/hooks/catch/...",
  "visitor_signals_enabled": true,
  "wallet": {
    "allocated_credits": 20000,
    "consumed_credits": 314,
    "reserved_credits": 2
  },
  "preferences_weights": {
    "hiring_intent": 2,
    "tech_wordpress": -5
  },
  "dynamic_blocklist": ["checkout", "add to cart"],
  "unit_economics": {
    "cpl": 50,
    "sdr_rate": 80,
    "deal_size": 5000,
    "conversion_rate": 0.05
  },
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

**Notes:**
- `role`: `"admin"` (default) or `"super_admin"` (grants L0 dashboard + quota bypass)
- `approval_status`: `"pending"` blocks all pipeline execution; set to `"approved"` by L0 admin
- `visitor_signals_enabled` (V24.5): opt-out flag for Inbound Radar; returns 204 immediately if false
- `wallet.reserved_credits`: in-flight credits held during pipeline execution; settled on completion
- `wallet.consumed_credits`: base counter only. True total = `consumed_credits + SUM(wallet_shards/0-9)`

### 4.2 `users/{tenant_id}/wallet_shards/{0-9}` Sub-Collection
Distributed credit counters (bypass Firestore write contention).
```json
{ "consumed_credits": 42 }
```

### 4.3 `campaigns` Collection

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
  "sourcing_vector": "B2B",
  "persona_id": "<firestore_persona_doc_id>",
  "persona_bio": "Denormalised bio from linked Persona Vault entry.",
  "persona_keywords": "keyword1, keyword2",
  "persona_name": "Enterprise SaaS Decision Makers",
  "leads_generated": 105,
  "next_drip_due": "<TIMESTAMP>",
  "drip_interval_minutes": 60,
  "unprocessed_queue": [],
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

**Notes:**
- `sourcing_vector`: one of `B2B`, `B2C`, `B2B2C`, `D2C` — drives query generation, Serper temporal window, and Gemini prompt branching
- `unprocessed_queue`: array of Serper result objects awaiting Gemini profiling; capped at 200 (backpressure at depth 150)
- `next_drip_due`: updated on every produce run (V24.4 fix — was only set on first fill)
- `keywords`: stored as comma-separated string, parsed to array in pipeline

### 4.4 `tenant_profiles/{tenant_id}/personas/{persona_id}` Sub-Collection
Persona Vault: named AI agent configurations scoped to a tenant.

```json
{
  "name": "Enterprise SaaS Decision Makers",
  "bio": "[Who we help]: ...\n[The problem we solve]: ...\n[Our unfair advantage]: ...",
  "keywords": "cto, vp engineering, saas",
  "is_legacy": false,
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

### 4.5 `leads` Collection
Core atomic lead document. Document ID is a deterministic SHA-256 hash.

```json
{
  "_id": "sha256(tenant_id + '_' + root_domain)",
  "tenant_id": "uid_from_firebase_auth",
  "matched_campaigns": ["camp_uuid_789"],
  "source_url": "https://techcorp.com",
  "status": "new",
  "score": 8,
  "normalized_score": 80,
  "origin_engine": "cartographer",
  "pain_point": "Complaining about high turnover on LinkedIn.",
  "icebreaker_angle": "Focus on facility hygiene boosting employee retention.",
  "dm": "Hey [Name], noticed...",
  "hiring_intent_found": "Yes",
  "tech_stack_found": ["react", "hubspot"],
  "decision_maker_name": "John Doe",
  "decision_maker_title": "VP of Operations",
  "company_size_tier": "Mid-Market",
  "primary_objection_hypothesis": "May lack budget for external tooling.",
  "email": "hr@techcorp.com",
  "phone": "3125550199",
  "linkedin": "https://linkedin.com/in/...",
  "contact_endpoints": ["hr@techcorp.com", "+13125550199"],
  "crm_delivery_status": "delivered",
  "interactions": [
    { "action": "status_ignored", "date": "<SERVER_TIMESTAMP>" }
  ],
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

**Status Enum:** `processing` → `new` → `reviewed` → `contacted` → `converted` | `ignored` | `failed` | `enrichment_pending`

**Key fields (V24.2+):**
- `normalized_score` (0–100): unified scale across engines. Outbound = `score × 10`. Inbound = `intent_score × 100`
- `origin_engine`: `"cartographer"` (Serper-driven) or `"autonomous"` (nightly engine) or `"inbound"` (Inbound Radar)
- `crm_delivery_status`: `"delivered"` | `"pending_retry"` | `"failed_permanent"` (V24.4 CRM retry)
- `enrichment_pending`: set when Medium-tier URL has < 300 chars of text — awaiting full scrape (V24.4)

**Score gate:** Only leads scoring `>= 7` are written as `"new"`. Leads below 7 delete the document.
**Deduplication key:** `sha256(tenant_id + '_' + root_domain)` — same domain can never appear twice per tenant.

### 4.6 `global_lead_locks` Collection
Cross-tenant exclusivity lock. Prevents two tenants from being served the same lead.

```json
{
  "_id": "sha256(exact_path_or_root_domain)",
  "locked_until": "<TIMESTAMP +14 days>"
}
```

### 4.7 `scraped_cache` Collection
Caches Playwright scrape results for 30 days.

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

### 4.8 `system_telemetry/feature_flags` Document
Firestore-controlled feature flags.

```json
{
  "whatsapp_enabled": false
}
```

`whatsapp_enabled` defaults to `false`. WhatsApp notifications are disabled by policy (AGENTS.md) and gated here. Do not set to `true` without explicit approval.

### 4.9 BigQuery Tables (`swarm_analytics` dataset)

#### `Intent_Keywords`
| Column | Type | Description |
|---|---|---|
| `persona_category` | STRING | Campaign/persona name — scopes N-grams by ICP |
| `n_gram` | STRING | Buyer-syntax phrase (e.g., "struggling with") — PII-scrubbed (V24.2) |
| `occurrence_count` | INTEGER | Raw frequency |
| `yield_weight` | FLOAT | Quality-weighted confidence mass (V24.5: conversion +2.0, rejection −0.5, occurrence +1.0) |

#### `Negative_Signals`
| Column | Type | Description |
|---|---|---|
| `tenant_id` | STRING | Tenant scope (or `"GLOBAL"` for platform-wide suppressions) |
| `root_domain` | STRING | Domain to suppress in Serper queries |
| `entity_name` | STRING | Entity label for neg shield |
| `sourcing_vector` | STRING | Vector scope (`"B2B"`, `"B2C"`, etc., or `"GLOBAL"`) — V24.3 vector isolation |
| `reason` | STRING | Rejection reason: `competitor`, `wrong_industry`, `not_icp`, `low_quality` (V24.5) |

---

## 5. SECURITY ARCHITECTURE (V24.2)

### 5.1 OIDC Validation (L9-3)
Internal cron endpoints (`/api/internal/cron/*`) verify Google OIDC tokens:
- Token audience validated against `ORCHESTRATOR_URL` — prevents cross-service token replay (OWASP A2:2021)
- Firebase ID tokens are explicitly rejected (wrong issuer)
- If `ORCHESTRATOR_URL` is unset, validation is skipped with a `log.warning` (observable; not silent)

### 5.2 Mandatory Cron Secret (L9-2)
`INTERNAL_CRON_SECRET` env var is now required:
- If unset → `503 Service not configured` returned on all inbound sentiment trigger requests
- If set but mismatched → `401 unauthorized`
- Cloud Tasks queue header (`X-CloudTasks-QueueName`) is accepted as an alternative auth path

### 5.3 PII Scrubbing Before BigQuery (L8-2, GDPR)
`_scrub_pii()` is applied to all `pain_point` + `dm` text before N-gram extraction and BQ write:
- Email addresses → `[EMAIL]`
- Phone numbers → `[PHONE]`
- Salutation-prefixed names (`Mr. John Smith`) → `[NAME]`

### 5.4 WhatsApp Feature Flag (L4-8)
`_maybe_notify_whatsapp()` reads `system_telemetry/feature_flags.whatsapp_enabled` before sending. Defaults to `false`. Fail-safe: if Firestore read fails, notification is skipped (not sent).

### 5.5 Firestore Security Rules
```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /leads/{document} {
      allow read:           if request.auth != null && resource.data.tenant_id == request.auth.uid;
      allow create:         if request.auth != null && request.resource.data.tenant_id == request.auth.uid;
      allow update, delete: if request.auth != null && resource.data.tenant_id == request.auth.uid;
    }
    match /campaigns/{document} {
      allow read:           if request.auth != null && resource.data.tenant_id == request.auth.uid;
      allow create:         if request.auth != null && request.resource.data.tenant_id == request.auth.uid;
      allow update, delete: if request.auth != null && resource.data.tenant_id == request.auth.uid;
    }
    match /{document=**} {
      allow read, write: if false;
    }
  }
}
```
All other collections (`users`, `global_lead_locks`, `scraped_cache`, etc.) are only accessible via Firebase Admin SDK inside backend services, which bypasses rules entirely.

---

## 6. PIPELINE EXECUTION FLOW (10 Steps)

### Step 1: Cloud Scheduler Cron Trigger
- **Schedule:** Every 5 minutes
- **Target:** `POST /api/internal/cron/sweep` on the Orchestrator
- **Auth:** OIDC token with audience = `ORCHESTRATOR_URL` (V24.2)

### Step 2: Per-Campaign Drip Rate Check
```python
if next_drip_due and next_drip_due > now_utc:
    continue  # Campaign not due yet
```
After queuing: `next_drip_due` = `now + drip_interval_minutes` (default: 60 min).

### Step 3: Quota & Wallet Validation
1. Skip if `role == "super_admin"` (unlimited)
2. Check `approval_status == "approved"`, else return 403
3. True balance = `allocated_credits − consumed_credits − SUM(wallet_shards) − reserved_credits`
4. If balance ≤ 0: skip campaign

### Step 4: Cloud Task Dispatch with Jitter
```python
jitter_seconds = random.randint(1, 290)  # Stagger over 5-minute window
task = {
    "http_request": {
        "url": PIPELINE_URL,  # /dispatch endpoint
        "body": json.dumps({"tenant_id": ..., "campaign_id": ...}).encode(),
        "oidc_token": {"service_account_email": sa_email, "audience": base_url}
    },
    "schedule_time": now + jitter_seconds
}
```

### Step 5: Smart Query Generation — Hybrid Confidence Router
**Location:** `pipeline-main/services/query_brain.py`

#### 5a. Shadow Tracker — Buyer Syntax Accumulator
Triggered asynchronously on every lead approval (`PUT /api/leads/{id}`).

```python
# shadow_tracker.py::_do_shadow_track (daemon thread)
scrubbed_text = _scrub_pii(pain_point)   # V24.2: PII removed before BQ write
ngrams = extract_ngrams(scrubbed_text, n=[2, 3])
for gram in ngrams:
    # BigQuery MERGE into swarm_analytics.Intent_Keywords
    # V24.5: yield_delta varies by event_type:
    #   conversion  → +2.0
    #   rejection   → -0.5
    #   occurrence  → +1.0
```

#### 5b. Confidence Threshold Router
```python
total_confidence = bq.query(
    "SELECT SUM(yield_weight) FROM Intent_Keywords WHERE persona_category = @cat"
).result()

THRESHOLD = firestore.system_config.get("intent_confidence_threshold", 1000)
# V24.2: Firestore read failure now logged at WARNING (was silent except:pass)
```

| Condition | Route | Query Source |
|---|---|---|
| `SUM(yield_weight) >= 1000` | **STATISTICAL** | Top 3 N-grams from BigQuery — zero Gemini cost |
| `SUM(yield_weight) < 1000` | **GEMINI_FALLBACK** | LLM starter motor generates symptom dorks |

**STATISTICAL path (V24.3 fix):** Generates dorks for all 3 top N-grams (was only `top_ngrams[0]`).

#### 5c. Archetype-Aware Query Assembly
Routing is gated by `sourcing_vector` (read from `core/constants.py — CONSUMER_ARCHETYPES`):

| Vector | Gemini Prompt Branch | Serper Temporal Window | Neg Shield Scope |
|---|---|---|---|
| B2B | Standard — enterprise buyer signals, filetype dorks | All-time | B2B-scoped |
| B2C | Consumer mandate — forum/review dorks, dialog-cue dorks (`"pm me"`, `"still available"`) | `tbs=qdr:m` (past month) | B2C-scoped |
| D2C | D2C product comparison mandate — `site:reddit.com`, `site:trustpilot.com`, `inurl:compare` | `tbs=qdr:m` | D2C-scoped |
| B2B2C | Dual-ICP mandate — 50% institutional + 50% end-user signals | `tbs=qdr:m` | B2B2C-scoped |

**B2C keyword fallback (V24.3):** When `ctx.intents` is empty and only bio keywords are available, consumer campaigns use intent template queries (`"looking for"`, `"anyone selling"`, `"pm me"`) instead of quoting raw bio words. Raw bio words produce SEO directory results, not buyer signals.

#### 5d. Blacklist Priority Rebuild (V24.5)
Assembly order: RLHF-learned exclusions → neg shield domains → persona NOT signals → static defaults.
The 350-char cap trims from the tail — campaign-specific learned exclusions survive, static defaults are trimmed first.

#### 5e. Vector-Isolated Negative Signal Shield (V24.3)
`fetch_neg_shield(tenant_id, sourcing_vector)` now queries BigQuery with:
```sql
WHERE (tenant_id = @tenant_id OR tenant_id = 'GLOBAL')
  AND (sourcing_vector = @vector OR sourcing_vector = 'GLOBAL' OR sourcing_vector IS NULL)
```
B2B rejected domains (e.g., `clutch.co`) no longer suppress B2C/D2C search results.
Cache is keyed by `tenant_id::sourcing_vector` with 10-minute TTL.

### Step 6: Serper Search Execution
**Location:** `pipeline-main/services/serper_service.py`

```python
payload = {"q": f"{query} AND {location}", "num": 20, "location": location, "gl": country_code}
# Consumer campaigns: payload["tbs"] = "qdr:m"  (past month freshness)
response = httpx.post("https://google.serper.dev/search", headers={"X-API-KEY": key}, data=payload)
```

Post-flight noise filter removes enterprise/aggregator domains, noise URL paths (`/legal`, `/pricing`, `/docs`, `/login`), and noise snippet patterns.

**Queue backpressure (V24.4):** If `unprocessed_queue` depth > 150, produce skips Serper fetch entirely (`200 skipped_queue_full`). Prevents `[:200]` trimming from discarding fresh signals when the consumer hasn't caught up.

### Step 7: Gemini Pre-Filter Gate
All deduplicated Serper snippets pass through a Gemini LLM gate before scraping:
- Rejects: SEO blogs, competitors, business directories, manufacturers
- For consumer campaigns: evaluates the specific post intent, not the platform
- Geo rule: if the site explicitly serves a different region → reject
- Failure: pre-filter gate timeout is logged at `WARNING` with impact note (V24.4)

### Step 8: Global Exclusivity Lock + Deduplication
```python
lock_ref = db.collection("global_lead_locks").document(lock_entity)
if lock_doc.exists and lock_doc.to_dict().get("locked_until") > now_utc:
    continue  # Locked by another tenant for 14 days
lock_ref.set({"locked_until": now_utc + timedelta(days=14)})

lead_id = hashlib.sha256(f"{tenant_id}_{root_domain}".encode()).hexdigest()
doc_ref.create({"status": "processing", ...})  # Raises AlreadyExists if duplicate
```

Lock-delete failures are logged at `ERROR` (V24.2 — was silent `except: pass`).

### Step 9: Three-Tier Scraping Strategy

**Tier 1 — Social Short-Circuit (Free):**
`linkedin.com`, `facebook.com`, `reddit.com`, `instagram.com`, `x.com`, `twitter.com`, `quora.com`, `youtube.com` → skip scraping, use Serper snippet directly.

**Tier 2 — Lightweight httpx:**
Synchronous `httpx.get(url, timeout=10)`. WAF detection, tech stack X-Ray, `mailto:`/`tel:` extraction.
If content < 500 chars → raises `ValueError("DEFERRED")` → escalates to Tier 3.

**Tier 3 — Playwright Heavy (DEFERRED):**
Cloud Task to `scraper-heavy/scrape`:
- Headless Chromium, `DECODO_STANDARD_PROXY`
- 20-second `asyncio.wait_for()` kill switch
- WAF detection → re-launch with `DECODO_PREMIUM_PROXY`
- Strips `script`, `style`, `noscript`, `nav`, `footer`, `iframe`

**Medium-tier enrichment pending (V24.4):**
Medium-tier URLs with < 300 chars are marked `enrichment_pending` instead of being scored on snippet data. Scoring a 2-sentence snippet produces leads where all fields are "Unknown".

### Step 10: RLHF Pre-Screen + Gemini Scoring

**A. Python Fast-Fail Gate:** Heuristic blocklist check (global + tenant dynamic). Score > 3 → `failed`.

**B. Token Reduction — Density Extraction:** Top 10 most relevant paragraphs by keyword overlap with bio. Reduces Gemini token consumption ~80%.

**C. Multi-Vector Serper Enrichment:** GMB rating, LinkedIn presence, hiring intent signals.

**D. RLHF Python Interceptor:**
```python
fit_score = preferences_weights.get("hiring_intent", 0) * native_hiring_intent
for tech in tech_stack:
    fit_score += preferences_weights.get(f"tech_{tech}", 0)
if fit_score <= -3:
    doc_ref.delete()  # Skip Gemini — saves 1 token sequence
```

**E. Few-Shot Conversion Context:** Last 3 `converted` leads' DMs injected into Gemini prompt for tone enforcement.

**F. Gemini Scoring (gemini-2.5-flash):**
Response locked to strict JSON schema via `GenerationConfig(response_mime_type="application/json", response_schema=schema)`. Key output fields: `score`, `dm`, `pain_point`, `icebreaker_angle`, `normalized_score` written by dispatch as `min(score * 10, 100)`.

**Score gate:** Only leads `>= 7` written as `"new"`. Below 7 → document deleted.
**Credit settlement (V24.4):** Credit is settled on score-drop path (was only settled on success/finalize).

---

## 7. RLHF SELF-LEARNING SYSTEM (V24.5)

### 7.1 UI Action → Backpropagation
When a lead status changes to `converted`, `ignored`, `reviewed`, or `contacted`:
```python
delta = 1 if status == "converted" else -1

# Hiring intent + tech stack weight updates
pref_updates["preferences_weights.hiring_intent"] = firestore.Increment(delta)
for tech in tech_stack:
    pref_updates[f"preferences_weights.tech_{tech}"] = firestore.Increment(delta)

# Ignored leads → dynamic blocklist
if status == "ignored":
    words = re.findall(r'\b\w{4,}\b', pain_point.lower())[:3]
    pref_updates["dynamic_blocklist"] = firestore.ArrayUnion(words + tech_stack[:2])
```

### 7.2 Shadow Tracker — Yield-Weight Quality Signal (V24.5)
`async_shadow_track(event_type=...)` writes different yield deltas to BigQuery:

| Event Type | `@yield_delta` | Effect |
|---|---|---|
| `conversion` | `+2.0` | High-quality signal — heavily weights the N-gram |
| `rejection` | `−0.5` | Negative signal — decays the N-gram's confidence |
| `occurrence` | `+1.0` | Standard approval signal |

### 7.3 RLHF Signal Pool (V24.5 fix)
`query_brain.py` includes `status in ["reviewed", "contacted", "converted"]` in the RLHF Firestore query. Previously excluded `"reviewed"` leads, biasing the signal pool toward fast-actioned leads only.

### 7.4 Expanded Rejection Vocabulary (V24.5)
`NEG_SIGNAL_REASONS` in `neg_signal.py`:

| Reason | BQ Impact | Score Penalty |
|---|---|---|
| `competitor` | Suppresses domain in neg shield | −0.20 |
| `wrong_industry` | Suppresses domain in neg shield | −0.15 |
| `not_icp` | Suppresses domain in neg shield | −0.10 |
| `low_quality` | Suppresses domain in neg shield | −0.10 |

---

## 8. INBOUND RADAR (Visitor Signal Pipeline)

The Inbound Radar converts website visitor signals (page visits, form submissions, scroll depth) into qualified leads by running Gemini intent inference on the visitor's behaviour.

### 8.1 Opt-Out Gate (V24.5)
`visitor_signals_enabled` field on `users` document. If `false` → `204 No Content` immediately. Prevents Inbound Radar from running for tenants who have disabled it.

### 8.2 Intent Scoring
`intent_score` is a 0.0–1.0 float. Inbound leads are written with:
```python
"normalized_score": round(sig.get("intent_score", 0.5) * 100)
```

### 8.3 Lead Promotion
Inbound signals scoring above the tenant's intent floor are promoted to the `leads` collection with `origin_engine: "inbound"`.

---

## 9. ANALYTICS & ROI ENGINE

### 9.1 ROI Metrics (`GET /api/analytics/roi`)
Query params:
- `?date_range=N` — look-back window in days
- `?vertical=B2C` — filter by `sourcing_vector` (V24.5)

Computes: Ad Savings, Labor Savings, Pipeline Value from tenant's `unit_economics` configuration.

### 9.2 Serper Telemetry (`GET /api/analytics/serper-telemetry`)
- Today sub-query uses `CAST(timestamp AS TIMESTAMP)` (V24.5 fix — was returning 0 today-count)
- NULL `credit_cost` defaults to `0` in aggregation (V24.5 fix — was phantom-billing 1 credit)

### 9.3 Unit Economics (`PUT /api/analytics/unit-economics`)
Persists `cpl`, `sdr_rate`, `deal_size`, `conversion_rate` to `users/{id}.unit_economics`.

---

## 10. CRM INTEGRATION (V24.4)

### 10.1 Webhook Delivery with Retry
On lead promotion or status change, if `crm_webhook_url` is configured:
```python
httpx.post(crm_webhook_url, json={
    "lead_id": doc_id,
    "score": lead_data.get("score"),
    "dm": lead_data.get("dm"),
    "intent_signal": lead_data.get("intent_signal"),
    "contact_endpoints": lead_data.get("contact_endpoints", []),
}, timeout=5)
```

On failure (V24.4):
1. `crm_delivery_status` set to `"pending_retry"` on the lead document
2. A 3-hour retry Cloud Task is enqueued to `/api/internal/crm-retry`
3. If task enqueue also fails → `crm_delivery_status` set to `"failed_permanent"`

### 10.2 GET /api/leads Filtering (V24.4)
Supports `?sort_by=score&min_score=N` query params for CRM-style pipeline views.

---

## 11. ORCHESTRATOR REST API REFERENCE

**Base URL:** `https://orchestrator-222247989819.asia-south1.run.app`
**Auth:** `Authorization: Bearer <Firebase ID Token>` on all user endpoints.

| Method | Path | Description |
|---|---|---|
| GET | `/api/me` | User profile + wallet balance |
| PUT | `/api/me` | Update `agreed_to_terms` or `crm_webhook_url` |
| GET | `/api/campaigns` | List tenant campaigns |
| POST | `/api/campaigns` | Create campaign (quota check first) |
| PUT | `/api/campaigns/{id}` | Update campaign |
| POST | `/api/campaigns/{id}/run` | Manual campaign dispatch |
| GET | `/api/leads` | List leads (`?sort_by=score&min_score=N` supported) |
| PUT | `/api/leads/{id}` | Update status + trigger RLHF backprop |
| POST | `/api/settings` | Save settings (KMS encrypted where applicable) |
| GET | `/api/analytics/roi` | ROI matrix (`?date_range=N&vertical=X`) |
| PUT | `/api/analytics/unit-economics` | Persist unit economics |
| GET | `/api/analytics/serper-telemetry` | Serper usage telemetry |
| POST | `/api/visitor-signals` | Inbound Radar signal intake |
| POST | `/api/internal/cron/sweep` | Master cron — OIDC only |
| POST | `/api/internal/crm-retry` | CRM webhook retry — Cloud Tasks only |

---

## 12. AUTONOMOUS ENGINE — NIGHTLY DIGITAL EXHAUST SCRAPER

- **Type:** Cloud Run Job (non-HTTP; not a Cloud Run Service)
- **Schedule:** `0 2 * * *` (2 AM IST daily via Cloud Scheduler)
- **Task timeout:** 3600s (1-hour hard limit)

**Logic:**
1. Reads all active tenants from `users/`
2. For each tenant: harvests digital exhaust (social signals, hiring intent, public sentiment)
3. Scores signals via Gemini (gated by `DAILY_GEMINI_BUDGET`)
4. Writes pre-scored leads to `predictive_cache` collection (TTL: 72h via Firestore TTL policy on `expire_at` field)

**Predictive cache leads** are served with zero Serper cost during the next campaign run via the Epsilon-Greedy Router.

---

## 13. CI/CD PIPELINE (cloudbuild.yaml)

Triggered on every push to `main` branch. Fully parallelised — all Docker builds run simultaneously.

```
Group 1 (parallel): build all 6 service images + firebase-deploy
Group 2 (parallel): push all 6 images to GCR
Group 3 (parallel): deploy all 6 Cloud Run services/jobs
Group 4 (sequential): provision Cloud Scheduler job (idempotent create-or-update)
```

**Build substitutions (all required):**
```yaml
_PROJECT_ID: "sideio-leads-v16"
_REGION: "asia-south1"
_FIREBASE_PROJECT: "lead-sniper-prod"
_PIPELINE_SA_EMAIL: "lead-pipeline-sa@sideio-leads-v16.iam.gserviceaccount.com"
_PIPELINE_URL: "https://lead-pipeline-main-222247989819.asia-south1.run.app/dispatch"
_ORCH_URL: "https://orchestrator-222247989819.asia-south1.run.app"
```

**Cache bust rule:** Always bump the version string in `index.html` and `sw.js` when touching frontend files.

---

## 14. FRONTEND ARCHITECTURE

**Stack:** Vanilla JavaScript, Firebase SDK v8 compat, Chart.js, Firebase Hosting (PWA).

### 14.1 Real-Time Lead Feed
```javascript
unsubscribeLeads = firebase.firestore()
    .collection('leads')
    .where('tenant_id', '==', user.uid)
    .onSnapshot((snapshot) => {
        rawLeadsCache = [];
        snapshot.forEach(doc => { ... });
        rawLeadsCache.sort((a, b) => (b.normalized_score || b.score * 10 || 0) - (a.normalized_score || a.score * 10 || 0));
        renderLeads();
    });
```

### 14.2 DOM Virtualisation
IntersectionObserver with 800px pre-load margin. Only viewport-visible leads are rendered to DOM. Off-screen leads preserve scroll height with empty innerHTML.

### 14.3 Wallet Alert Thresholds
- `credits <= 0` → Red banner + disables "Find New Clients"
- `credits < 50` → Warning banner
- `credits >= 50` → Banner hidden

### 14.4 Service Worker
```javascript
// Critical Firebase bypass — must be at the very top of fetch handler
if (url.hostname.includes('googleapis.com') ||
    url.hostname.includes('google.com')     ||
    url.hostname.includes('firestore')) {
    event.respondWith(fetch(event.request));
    return;  // Never cache Firestore WebChannel streams
}
```
Firestore `onSnapshot` uses long-poll WebChannel requests that are non-cloneable. If the SW intercepts them, it throws `"Failed to convert value to 'Response'"` and causes disconnect loops.

---

## 15. ERROR HANDLING & OBSERVABILITY

### 15.1 Structured Logging Policy (V24.2)
All error paths use `structlog` with event names and context fields:
```python
log.warning("lead_lock_delete_failed",
            lock_entity=lock_entity,
            url=url[:80],
            error=str(e))
```
**`except: pass` is prohibited.** All exception swallowing is replaced with at minimum `log.warning`. Critical paths (credit refund, lock release, zombie recovery) use `log.error`.

### 15.2 Log Level Policy
| Severity | Used For |
|---|---|
| `log.debug` | Cache hits, normal path confirmations |
| `log.info` | Successful operations, telemetry counts |
| `log.warning` | Degraded operation, non-fatal failures, feature flag fallbacks |
| `log.error` | Data integrity failures (credit refund, lock release, BQ write) |

### 15.3 Retry Boundaries
| Component | Retry Strategy |
|---|---|
| Gemini API | tenacity: `wait_exponential(min=2, max=10)`, 5 attempts, `ResourceExhausted` only |
| Gemini timeout | `concurrent.futures` hard 45s kill switch |
| Playwright | `asyncio.wait_for()` hard 20s kill switch |
| Neg shield BQ | `ThreadPoolExecutor` hard `_EFFECTIVE_TIMEOUT_S + 0.5` kill switch; stale cache fallback |
| Cloud Task retry | Bounded: 3 attempts max on all queued tasks |

---

## 16. KEY DESIGN INVARIANTS (NEVER BREAK)

1. **Tenant isolation:** Every Firestore document a tenant touches must have `tenant_id == user.uid`. Every BQ query must be scoped by `tenant_id`.
2. **Lead dedup ID:** Always `sha256(tenant_id + '_' + root_domain)`. Never auto-generate.
3. **Score gate:** Only leads `>= 7` are written as `"new"`. Everything below is deleted from Firestore immediately.
4. **Firestore rules:** `leads` and `campaigns` are the only collections the frontend can read/write directly. All other collections are Admin SDK only.
5. **SW Firebase bypass:** Never let the service worker intercept `googleapis.com` or `google.com` traffic.
6. **Wallet shards:** True balance = `allocated_credits − consumed_credits − SUM(wallet_shards/0-9) − reserved_credits`.
7. **OIDC for cron:** `/api/internal/cron/sweep` validates OIDC tokens with audience = `ORCHESTRATOR_URL` — never Firebase ID tokens.
8. **WhatsApp disabled:** `whatsapp_enabled` Firestore flag must remain `false`. Do not re-enable without explicit approval (AGENTS.md).
9. **Webhook disabled:** Webhook features are out of scope. Do not re-enable without explicit approval.
10. **Consumer archetypes:** Defined once in `pipeline-main/core/constants.py`. Import from there — never duplicate.
11. **No silent failures:** All `except: pass` is prohibited. Every exception must be logged with enough context to debug without reproduction.
12. **PII before BQ:** Always apply `_scrub_pii()` before writing lead text to BigQuery N-gram tables.

---

## 17. VERSION HISTORY (RECENT)

| Version | Date | Key Changes |
|---|---|---|
| V24.5.0 | 2026-07-01 | RLHF yield-weight quality signal, expanded rejection vocabulary, visitor signal opt-out, analytics vertical filter, serper telemetry fixes |
| V24.4.0 | 2026-07-01 | Queue backpressure, enrichment_pending stub, credit settlement on score-drop, CRM webhook Cloud Task retry, lead sort/filter |
| V24.3.0 | 2026-07-01 | D2C/B2B2C prompt branches, `tbs=qdr:m` consumer freshness, vector-isolated neg shield, B2C intent template fallback, core/constants.py shared module |
| V24.2.0 | 2026-07-01 | OIDC audience validation, mandatory INTERNAL_CRON_SECRET, PII scrubbing, WhatsApp feature flag, normalized_score, all except:pass replaced |
| V24.1.25 | 2026-06-30 | Gemini model swap to gemini-2.5-flash |
| V24.1.24 | 2026-06-30 | Dialog-Cue Dorking, Context-Aware LLM Inference |
| V24.1.23 | 2026-06-30 | Consumer search freshness (qdr:y, superseded by qdr:m in V24.3) |
| V24.1.22 | 2026-06-30 | Inbound Radar bypass for social media domain listicle path |
| V24.1.21 | 2026-06-30 | GMB review scanning via CID, SEO noise pre-screening |
| V24.1.20 | 2026-06-29 | Query spacing syntax hardening across all execution channels |
