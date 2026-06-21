# Sideio Lead Sniper — V23 Comprehensive Architecture Reference
## Full Technical Specification for Engineering Teams

> **Version:** V23.5 (Updated 2026-06-21 — Archetype Refactor + Query Quality Hardening)
> **Codebase mapped:** `D:\.gemini\antigravity\scratch\sideio_leads`
> **Document purpose:** Complete intern-grade rebuild reference — maps architecture to current live code

---

## TABLE OF CONTENTS

1. [System Overview](#1-system-overview)
2. [Repository Structure](#2-repository-structure)
3. [GCP Infrastructure](#3-gcp-infrastructure)
4. [Firestore Data Model](#4-firestore-data-model)
5. [Security & Authentication](#5-security--authentication)
6. [The 10-Step Pipeline Execution Flow](#6-the-10-step-pipeline-execution-flow)
7. [Orchestrator REST API Reference](#7-orchestrator-rest-api-reference)
8. [Frontend Architecture](#8-frontend-architecture)
9. [Service Worker](#9-service-worker)
10. [RLHF Self-Learning System](#10-rlhf-self-learning-system)
11. [Digital Twin Engine](#11-digital-twin-engine)
12. [Autonomous Engine & Epsilon-Greedy Router](#12-autonomous-engine--epsilon-greedy-router)
13. [Ontology Map Collection](#13-ontology-map-collection)
14. [BigQuery Telemetry Tables](#14-bigquery-telemetry-tables)
15. [L1 ROI & Analytics Matrix](#15-l1-roi--analytics-matrix)
16. [WhatsApp Hot Lead Alerts](#16-whatsapp-hot-lead-alerts)
17. [CI/CD Pipeline](#17-cicd-pipeline)
18. [Error Handling & Resilience Patterns](#18-error-handling--resilience-patterns)
19. [Dependencies & Requirements](#19-dependencies--requirements)
20. [Intern Rebuild Checklist](#20-intern-rebuild-checklist)
21. [Design Invariants — Never Break These](#21-design-invariants--never-break-these)
22. [V22 Amputation Record](#22-v22-amputation-record)
- [Appendix A: Firestore Composite Index](#appendix-a-firestore-composite-index)
- [Appendix B: Key File → Function Cross-Reference](#appendix-b-key-file---function-cross-reference)
- [Appendix C: V23.5 Changelog — Sourcing Vector Archetype Refactor](#appendix-c-v235-changelog--sourcing-vector-archetype-refactor)
- [Appendix D: Schema Boundary Isolation (V23.5)](#appendix-d-schema-boundary-isolation-v235)

---

## 1. SYSTEM OVERVIEW

**Sideio Lead Sniper** is a fully automated, multi-tenant lead generation SaaS supporting both B2B and B2C verticals. It discovers, scores, and delivers hyper-personalized outreach messages to paying tenants — autonomously, 24/7, without manual input.

### 1.1 Core Execution Loop

```
Every 5 min: Cloud Scheduler
      |
      v
Orchestrator /api/internal/cron/sweep
      |  (For each active campaign: quota check -> drip rate check)
      v
Cloud Tasks enqueue -> Pipeline-Main /produce (Serper search + URL dedup)
      |
      v
Cloud Tasks enqueue -> Pipeline-Main /dispatch (Scrape -> Score -> Write leads)
      |
      v
Firestore `leads` collection
      |
      v
Frontend onSnapshot -> Real-time lead feed (no polling)
```

### 1.2 High-Level Architecture Diagram

```
+--------------------------------------------------------------+
|  BROWSER (Firebase Hosting CDN - PWA)                        |
|  public/index.html + public/app.js + public/styles.css       |
|  Firebase Auth (Google OAuth) + Firestore onSnapshot         |
+---------------------------+----------------------------------+
                            | HTTPS + Firebase Auth JWT
                            v
+--------------------------------------------------------------+
|  ORCHESTRATOR  (Cloud Run · asia-south1)                     |
|  Flask V23.4 · 8 Blueprint routers · allow-unauthenticated   |
|  Entry: services/orchestrator/main_v23.py                    |
+------+------------+-------------------+-----------------------+
       |            |                   |
       | Firestore  | Cloud Tasks       | BigQuery streaming insert
       | Admin SDK  | enqueue           | (shadow tracker, neg signals)
       v            v                   v
+----------+  +-----------+  +------------------------------+
| Firestore|  | Cloud Tasks|  | BigQuery (swarm_analytics)   |
| (Native) |  | lead-pipe  |  | Intent_Keywords              |
|          |  | line-queue |  | Negative_Signals             |
+----------+  +-----+-----+  | serper_audit_logs            |
                    |         +------------------------------+
                    | OIDC JWT
                    v
+--------------------------------------------------------------+
|  PIPELINE-MAIN  (Cloud Run · no-allow-unauthenticated)       |
|  /produce: Serper search + dedup + write unprocessed_queue   |
|  /dispatch: Pop URLs + Gemini gate + scrape + score + write  |
+------+---------------+--------------------+-----------------  +
       | Vertex AI     | Serper API         | scraper-heavy
       | (Gemini 2.5)  | httpx direct       | Cloud Task
       v               v                    v
+------------+  +----------+  +--------------------------+
| Vertex AI  |  | Serper   |  | SCRAPER-HEAVY            |
| us-central1|  | .dev     |  | Playwright Chromium      |
| gemini-2.5-|  | /search  |  | Decodo rotating proxies  |
| flash      |  +----------+  +--------------------------+
+------------+
```

### 1.3 What Makes This System Different

| Feature | Description |
|---|---|
| **Zero-manual-input** | Campaigns run 24/7 on cron. No user action needed after setup |
| **Multi-tenant isolation** | Every document tagged with `tenant_id`. Firestore rules enforce it |
| **RLHF self-learning** | Every approval/rejection updates BigQuery n-gram confidence weights |
| **Epsilon-greedy routing** | 15% leads from pre-scored cache (zero Serper cost), 85% live Serper |
| **3-tier scraping** | Social short-circuit -> httpx/BeautifulSoup -> Playwright heavy |
| **Digital Twin** | AI-scans your website, extracts company DNA, generates campaign templates |
| **Negative Knowledge Graph** | Competitor/author domains auto-excluded from all future searches |

---

## 2. REPOSITORY STRUCTURE

```
sideio_leads/
+-- public/                          # Firebase Static Hosting (PWA)
|   +-- index.html                   # Full DOM + Firebase SDK init
|   +-- app.js                       # All frontend logic (~4200 lines, V23.4)
|   +-- styles.css                   # CSS design system (glassmorphism + V17)
|   +-- sw.js                        # Service Worker (cache v10-3)
|   +-- manifest.json                # PWA manifest
|
+-- services/
|   +-- orchestrator/                # Cloud Run: REST API Gateway + Cron Dispatcher
|   |   +-- main_v23.py              # PRODUCTION ENTRYPOINT (Blueprint registry)
|   |   +-- api/routers/
|   |   |   +-- campaigns.py         # Campaign CRUD + ignite + child campaigns
|   |   |   +-- internal.py          # Cron sweep + BQ telemetry + credit settle
|   |   |   +-- leads.py             # Lead status updates + RLHF backprop
|   |   |   +-- personas.py          # Persona Vault CRUD
|   |   |   +-- settings.py          # Tenant profiles + analyze-website
|   |   |   +-- me.py                # /api/me user profile endpoint
|   |   |   +-- analytics.py         # ROI matrix endpoints
|   |   |   +-- data_reads.py        # GET campaigns + tenant_profiles
|   |   |   +-- l0_admin.py          # Super admin dashboard
|   |   |   +-- serper_telemetry.py  # Serper audit log read endpoint
|   |   +-- core/
|   |   |   +-- config.py            # All env vars
|   |   |   +-- clients.py           # Lazy Firestore + Cloud Tasks singletons
|   |   |   +-- helpers.py           # All shared business logic + archetype classifier
|   |   |   +-- logging.py           # structlog JSON logger -> Cloud Logging
|   |   +-- services/intelligence/
|   |       +-- shadow_tracker.py    # BQ n-gram accumulator
|   |       +-- neg_signal.py        # BQ negative signal writer
|   |
|   +-- pipeline-main/               # Cloud Run: AI Extraction Engine
|   |   +-- api/routers/
|   |   |   +-- produce.py           # /produce: Serper search + dedup (357 lines)
|   |   |   +-- dispatch.py          # /dispatch: Gemini gate + scrape + score
|   |   +-- services/
|   |       +-- query_brain.py       # Hybrid Starter Motor (BQ -> Gemini fallback)
|   |       +-- serper_service.py    # Serper API calls + noise filtering
|   |       +-- prism_pipeline.py    # URL routing (WalledGarden/B2B2C/General)
|   +-- scraper-heavy/               # Cloud Run: Playwright Headless Browser
|   +-- digital-twin-engine/         # Cloud Run: Website Analyser
|   +-- autonomous-engine/           # Cloud Run JOB (no HTTP): Nightly Scraper
|   +-- shadow-learner-aggregator/   # Cloud Run: RLHF Swarm Weight Aggregator
|   +-- whatsapp-webhook/            # Cloud Run: WhatsApp Business API
|   +-- email-summary/               # Cloud Run: Weekly Digest Sender
|
+-- migration/                       # Database migration scripts
|   +-- patch_sourcing_vector.py     # Migrate campaigns to archetype-based vectors
|   +-- scrub_oman_b2b_arrays.py     # Clear zombie B2B data from B2C campaigns
|
+-- firebase.json                    # Hosting config + Firestore rules pointer
+-- firestore.rules                  # V13.22 multi-tenant security rules
+-- firestore.indexes.json           # Composite index: tenant_id + timestamp
+-- cloudbuild.yaml                  # CI/CD: parallelized enterprise pipeline
```

---

## 3. GCP INFRASTRUCTURE

### 3.1 Cloud Run Services

| Service | Cloud Run Name | Auth | Memory | Notes |
|---|---|---|---|---|
| Orchestrator | `orchestrator` | allow-unauthenticated | 512 Mi | Public REST API |
| Pipeline Main | `lead-pipeline-main` | no-allow-unauthenticated | 1 Gi | Cloud Tasks OIDC only |
| Scraper Heavy | `scraper-heavy` | internal | 2 Gi | Playwright Chromium |
| Digital Twin Engine | `digital-twin-engine` | internal | 512 Mi | Website analyser |
| Shadow Learner | `shadow-learner-aggregator` | internal | 256 Mi | BQ RLHF aggregator |
| WhatsApp Webhook | `whatsapp-webhook` | allow-unauthenticated | 128 Mi | Meta webhook receiver |
| Email Summary | `email-summary` | internal | 128 Mi | SendGrid digest |
| **Autonomous Engine** | **`autonomous-engine`** | **Cloud Run JOB** | **512 Mi** | **Not HTTP. Nightly batch.** |
| Frontend | Firebase Hosting | Public CDN | — | Global CDN distribution |

**Region:** `asia-south1` for all services
**GCP Project:** `sideio-leads-v16`
**Firebase Project:** `lead-sniper-prod`

### 3.2 Cloud Tasks

**Queue:** `lead-pipeline-queue` (region: `asia-south1`)

Tasks dispatched by the system:
- Orchestrator cron sweep -> `POST /produce` on pipeline-main
- Orchestrator cron sweep -> `POST /dispatch` on pipeline-main
- Pipeline dispatch -> `POST /scrape` on scraper-heavy (WAF sites only)
- Pipeline finalize -> `POST /api/internal/credits/settle` on orchestrator
- Orchestrator -> `POST /api/internal/telemetry/bq-push` (BQ streaming)

### 3.3 Cloud Scheduler Jobs

| Job Name | Target | Schedule | Auth |
|---|---|---|---|
| `pipeline-sweep` | `POST /api/internal/cron/sweep` | Every 5 minutes | OIDC |
| `ai-reflection` | `POST /api/internal/cron/reflection` | Weekly | OIDC |
| `ontology-decay` | `POST /api/internal/cron/ontology-decay` | Weekly | OIDC |

### 3.4 Environment Variables

```bash
# Orchestrator
PROJECT_ID=sideio-leads-v16
LOCATION=asia-south1
QUEUE=lead-pipeline-queue
PIPELINE_URL=https://lead-pipeline-main-222247989819.asia-south1.run.app/dispatch
ORCHESTRATOR_URL=https://orchestrator-222247989819.asia-south1.run.app
ORCHESTRATOR_SA_EMAIL=lead-pipeline-sa@sideio-leads-v16.iam.gserviceaccount.com
ENCRYPTION_KEY=<fernet_key>           # Fallback symmetric cipher for WA tokens

# Pipeline-Main
SERPER_API_KEY_NAME=SERPER_API_KEY    # MUST be uppercase (smoke test enforces)
SCRAPER_HEAVY_URL=https://scraper-heavy-<hash>.a.run.app/scrape

# Autonomous Engine
DAILY_GEMINI_BUDGET=1000
DISCOVERY_ALLOCATION=0.15
MOCK_MODE=false
```

### 3.5 GCP Secret Manager Secrets

| Secret Name | Used By | Purpose |
|---|---|---|
| `SERPER_API_KEY` (uppercase) | pipeline-main | Serper.dev search API key |
| `FIREBASE_SA_KEY` | Cloud Build | Firebase deploy service account JSON |
| `kms_wa_key_path` | orchestrator, pipeline-main | KMS key ring path for WhatsApp token |
| `DECODO_STANDARD_PROXY` | scraper-heavy | Standard rotating proxy URL |
| `DECODO_PREMIUM_PROXY` | scraper-heavy | Premium WAF-bypass proxy URL |

### 3.6 Service Account IAM (`lead-pipeline-sa`)

Required roles:
- `roles/run.invoker` on `lead-pipeline-main` (Cloud Run)
- `roles/cloudtasks.enqueuer` on `lead-pipeline-queue`
- `roles/secretmanager.secretAccessor`
- `roles/bigquery.dataEditor` on `swarm_analytics` dataset

---

## 4. FIRESTORE DATA MODEL

> **Critical:** Only `leads` and `campaigns` collections are accessible to the frontend via client SDK. All other collections are accessible only via Firebase Admin SDK (backend only — bypasses security rules).

### 4.1 `users` Collection

Document ID: Firebase Auth UID

```json
{
  "_id": "firebase_uid",
  "email": "user@example.com",
  "role": "admin",
  "tenant_id": "firebase_uid",
  "is_active": true,
  "approval_status": "pending",
  "beta_expiry": "2026-10-01T00:00:00Z",
  "agreed_to_terms": "<SERVER_TIMESTAMP>",
  "crm_webhook_url": "https://hooks.zapier.com/...",
  "wa_token": "gAAAAAB... (KMS or Fernet encrypted)",
  "wa_phone_id": "123456789",
  "admin_phone": "13125550199",
  "wallet": {
    "allocated_credits": 20000,
    "consumed_credits": 314,
    "total_consumed": 400,
    "reserved_credits": 10
  },
  "unit_economics": {
    "avg_cpl": 50,
    "sdr_hourly_rate": 15,
    "avg_deal_size": 0,
    "est_conversion_rate": 0.02,
    "currency": "USD"
  },
  "preferences_weights": {
    "hiring_intent": 2,
    "tech_wordpress": -5
  },
  "dynamic_blocklist": ["checkout", "add to cart"],
  "createdAt": "<SERVER_TIMESTAMP>"
}
```

**Key field rules:**
- `role`: `"admin"` (default) or `"super_admin"` (grants L0 dashboard + quota bypass)
- `approval_status`: `"pending"` blocks ALL pipeline execution. Set to `"approved"` by L0 admin via `POST /api/l0/users/{uid}/approve`
- `wallet.total_consumed`: Written by `_atomic_settle_txn()`. True balance = `max(total_consumed, consumed_credits + SUM(wallet_shards))`
- `wa_token`: KMS-encrypted (primary) or Fernet-encrypted (fallback). Never stored plaintext

### 4.2 `users/{tenant_id}/wallet_shards/{0-9}` Sub-Collection

Distributed credit counters. 10 shards bypass Firestore 1-write/sec limit.

```json
{ "consumed_credits": 42 }
```

### 4.3 `campaigns` Collection

Document ID: Auto-generated Firestore ID

```json
{
  "_id": "auto_firestore_id",
  "tenant_id": "firebase_uid",
  "name": "Q3 Commercial Cleaning Push",
  "bio": "We offer B2B janitorial services for offices.",
  "status": "active",
  "keywords": "facility management, office cleaning",
  "location": "Austin, TX",
  "gl": "us",
  "leads_generated": 105,
  "next_drip_due": "<ISO-8601 TIMESTAMP>",
  "next_produce_due": "<ISO-8601 TIMESTAMP>",
  "drip_interval_minutes": 60,
  "unprocessed_queue": [],
  "sourcing_vector": "B2C",
  "persona_id": "<firestore_persona_doc_id>",
  "persona_bio": "Denormalised bio from linked Persona Vault.",
  "persona_keywords": "keyword1, keyword2",
  "persona_name": "Enterprise SaaS Decision Makers",
  "persona_targeting_signals": ["NOT enterprise"],
  "zero_wait_enqueued": true,
  "createdAt": "<SERVER_TIMESTAMP>"
}
```

**Critical notes:**
- `next_produce_due`/`next_drip_due`: ISO-8601 strings (NOT Firestore Timestamp). Set by cron sweep.
- `unprocessed_queue`: Array of Serper result objects. Populated by `/produce`, drained by `/dispatch` (batch of 10).
- `sourcing_vector`: **V23.5 Archetype Model.** One of `B2B`, `B2C`, `B2B2C`, `D2C`. Classified by Gemini via `classify_sourcing_vector()` in `helpers.py`. Legacy campaigns may still have old values (`Classic B2B`, `Social/Forum Listening`, `Review Hijacking`, `Maps/GMB Targeting`) which are backwards-compatible (treated as B2B routing). See [Sourcing Vector Archetype System](#65-sourcing-vector-archetype-system--consumer-routing) for full details.
- `persona_bio/keywords/name`: Denormalised from Persona Vault at creation. Updated on persona PUT.
- `target_urls`: **PERMANENTLY AMPUTATED in V22**. May exist in legacy docs but is never read by pipeline.

### 4.4 `tenant_profiles/{tenant_id}/personas/{persona_id}` Sub-Collection

The Persona Vault: named ICP agent configurations.

```json
{
  "_id": "auto_firestore_id",
  "tenant_id": "firebase_uid",
  "name": "Enterprise SaaS Decision Makers",
  "bio": "[Who we help]: CTOs...\n[Problem we solve]: ...\n[Unfair advantage]: ...",
  "keywords": "cto, vp engineering, saas, b2b",
  "targeting_signals": ["NOT enterprise", "hiring"],
  "is_legacy": false,
  "createdAt": "<SERVER_TIMESTAMP>"
}
```

Rules:
- Deleting a persona blocked (HTTP 409) if active campaigns still reference its `persona_id`
- On persona PUT: orchestrator clears `predictive_cache` for affected campaigns and denormalises updated bio/keywords back onto those campaign documents

### 4.5 `leads` Collection

Document ID: `sha256(tenant_id + '_' + root_domain)` — deterministic, NEVER auto-generated

```json
{
  "_id": "sha256_hash",
  "tenant_id": "firebase_uid",
  "matched_campaigns": ["campaign_id_1"],
  "url": "https://techcorp.com",
  "status": "new",
  "score": 8,
  "confidence_tier": "High",
  "pain_point": "Complaining about high turnover on LinkedIn.",
  "icebreaker_angle": "Focus on facility hygiene boosting retention.",
  "dm": "Hey [Name], noticed...",
  "hiring_intent_found": "Yes",
  "tech_stack_found": ["react", "hubspot"],
  "decision_maker_name": "John Doe",
  "decision_maker_title": "VP of Operations",
  "company_size_tier": "Mid-Market",
  "primary_objection_hypothesis": "They might lack budget.",
  "email": "hr@techcorp.com",
  "phone": "3125550199",
  "origin_engine": "cartographer",
  "credit_settled": false,
  "createdAt": "<SERVER_TIMESTAMP>"
}
```

**Status enum:** `processing` -> `new` -> `contacted` -> `converted` | `ignored` | `failed`
**Score gate:** Only leads scoring `>= 7` survive. Scores below 7 -> document **deleted** immediately.
**`origin_engine`:** `"autonomous"` (pre-scored cache) or `"cartographer"` (live Serper). Drives the Predictive Match badge.

### 4.6 `global_lead_locks` Collection

Cross-tenant exclusivity. Prevents two tenants from being assigned the same lead.

```json
{ "_id": "sha256(root_domain)", "locked_until": "<TIMESTAMP + 14 days>" }
```

### 4.7 `scraped_cache` Collection

30-day cache of Playwright scrape results.

```json
{
  "_id": "url_keyed",
  "url": "https://techcorp.com",
  "text": "<truncated DOM text, max 100KB>",
  "tech_stack": ["wordpress"],
  "emails": ["contact@techcorp.com"],
  "phones": ["+13125550199"],
  "expireAt": "<TIMESTAMP + 30 days>"
}
```

### 4.8 `predictive_cache` Collection

Pre-scored leads from the nightly autonomous engine. TTL: 72 hours.

```json
{
  "_id": "sha256(tenant_id + '_' + root_domain)",
  "tenant_id": "firebase_uid",
  "url": "https://example.com",
  "score": 8,
  "pain_point": "AI-extracted pain signal from public posts",
  "dm": "Pre-drafted outreach message",
  "origin_engine": "autonomous",
  "expire_at": "<TIMESTAMP + 72 hours>"
}
```

> WARNING: TTL policy must be enabled MANUALLY in GCP Console -> Firestore -> Indexes -> TTL -> Collection: `predictive_cache` -> Field: `expire_at`. Cloud Build cannot enable TTL programmatically.

### 4.9 `ontology_map` Collection

Global domain intelligence repository. Self-updating weight table.

```json
{
  "_id": "techcrunch.com",
  "base_path": "techcrunch.com",
  "baseline_weight": 1.15,
  "total_yield": 73,
  "last_seen": "<SERVER_TIMESTAMP>",
  "last_decayed": "<SERVER_TIMESTAMP>"
}
```

- `baseline_weight > 1.0` = exploit bucket (proven domain)
- `baseline_weight < 1.0` = explore bucket (underperforming)
- RLHF adjustments only apply when `total_yield >= 50` (burn-in guard)
- Monthly decay: `new_weight = weight - (weight - 1.0) * 0.10`

### 4.10 Other Collections

| Collection | Purpose |
|---|---|
| `market_trend_cache` | RLHF-validated market trend hooks per product name |
| `system_config/router` | Epsilon-greedy router config + confidence threshold (default: 1000) |
| `system_telemetry/circuit_breaker_state` | Serper 429 rate + scraper OOM rate |
| `usage_metrics/{tenant_id}` | Serper search count per tenant |

---

## 5. SECURITY & AUTHENTICATION

### 5.1 Three-Layer Auth Model

```
Layer 1: Public API -> Orchestrator
         Authorization: Bearer <Firebase ID Token>
         Verified via: firebase_admin.auth.verify_id_token(token)

Layer 2: Cloud Scheduler -> Orchestrator cron
         Authorization: Bearer <Google OIDC Token>
         Verified via: google.oauth2.id_token.verify_oauth2_token()
         Audience: Orchestrator Cloud Run URL

Layer 3: Orchestrator -> Pipeline-Main (Cloud Tasks)
         OIDC token signed by lead-pipeline-sa
         Cloud Run IAM: --no-allow-unauthenticated
         Defense-in-depth: X-CloudTasks-QueueName header
```

### 5.2 `authenticate_request` Flow (All User Endpoints)

1. Extract Bearer token from `Authorization` header
2. Call `firebase_admin.auth.verify_id_token(token)` - validates cryptographic signature
3. Look up `users/{uid}` in Firestore
4. If user doesn't exist -> auto-create with `approval_status: "pending"`, `wallet: {0, 0}`
5. If `is_active == false` and not `super_admin` -> raise ValueError -> 401 returned
6. Return `(uid, tenant_id, user_role)` tuple

### 5.3 Firestore Security Rules (V13.22)

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
      allow read, write: if false;  // All other collections: backend Admin SDK only
    }
  }
}
```

---

## 6. THE 10-STEP PIPELINE EXECUTION FLOW

### Step 1: Cloud Scheduler Cron Trigger
- File: `services/orchestrator/api/routers/internal.py` -- `POST /api/internal/cron/sweep`
- Schedule: Every 5 minutes
- Verified via Google OIDC token (NOT Firebase ID token)
- Queries all `campaigns` where `status == "active"`, limit 500

### Step 2: Per-Campaign Drip Rate Check

```python
next_drip_due = campaign_data.get("next_drip_due")  # ISO-8601 string
if next_drip_due and datetime.fromisoformat(next_drip_due) > now_utc:
    continue  # Campaign not due yet. Skip.
# Clock always advances even on dispatch failure:
finally:
    campaign_ref.update({"next_produce_due": (now_utc + timedelta(hours=24)).isoformat()})
```

### Step 3: Quota & Wallet Validation

```python
def check_quota(tenant_id):
    # 1. Skip if super_admin (unlimited)
    # 2. Verify approval_status == "approved"
    # 3. Read BOTH wallet paths (handles schema drift)
    total_consumed  = int(wallet.get("total_consumed", 0))
    legacy_consumed = int(wallet.get("consumed_credits", 0))
    shard_sum       = sum(s.to_dict()["consumed_credits"] for s in wallet_shards.stream())
    consumed        = max(total_consumed, legacy_consumed + shard_sum)
    if (credits - consumed) <= 0:
        return False, 402, "Quota exhausted."
```

### Step 4: Cloud Task Dispatch with Jitter

```python
jitter_seconds = random.randint(1, 290)  # Stagger over 5-minute window
task = {
    "http_request": {
        "http_method": "POST",
        "url": PIPELINE_URL,  # /dispatch endpoint
        "body": json.dumps({"tenant_id": ..., "campaign_id": ...}).encode(),
        "oidc_token": {"service_account_email": sa_email, "audience": base_url}
    },
    "schedule_time": now + jitter_seconds
}
```

OIDC SA email: from `ORCHESTRATOR_SA_EMAIL` env var (zero I/O) or GCE metadata fallback (1s hard cap).

### Step 5: Smart Query Generation - Hybrid Confidence Router

File: `services/pipeline-main/services/query_brain.py::generate_smart_query`

**Path A - STATISTICAL BUILD** (when `SUM(yield_weight) >= 1000`):
- Reads top 3 n-grams from BigQuery `swarm_analytics.Intent_Keywords`
- Zero Gemini cost -- pure math
- Example query: `"struggling with churn" OR "high turnover" OR "losing customers"`

**Path B - GEMINI_FALLBACK** (cold start / new personas):

The Gemini prompt is **archetype-aware** (V23.5). When `sourcing_vector` is a consumer archetype (B2C, B2B2C, D2C), the prompt explicitly:
- Forbids B2B corporate jargon ("brand story", "positioning", "market fit", "lead generation")
- Demands Google Dork Boolean format for `symptom_dorks` (operators: OR, AND, quotes, intitle:, inurl:)
- Demands short keyword-based search strings for `translated_queries` (NOT conversational sentences)
- Injects a `VECTOR GUARD` + `QUERY FORMAT GUARD` into the system instruction

When `sourcing_vector` is B2B (or any legacy value), the standard B2B prompt is used with professional context framing.

**Routing decision:**
```
SUM(yield_weight) >= 1000 -> STATISTICAL BUILD (zero Gemini)
SUM(yield_weight) <  1000 -> GEMINI_FALLBACK (LLM starter motor)
BQ timeout > 3 seconds    -> GEMINI_FALLBACK (circuit breaker)
```

Configurable at runtime: `Firestore system_config/router.confidence_threshold` (default: 1000)

### Step 5.5: Query Assembly & Self-Negation Prevention (V23.5)

After Gemini returns `symptom_dorks`, `translated_queries`, and `historical_phrases`, the Query Brain assembles final Serper query strings by appending a global blacklist of exclusions.

**Blacklist layers (cumulative):**
1. `_DEFAULT_BLACKLIST`: `-wiki -jobs -careers -investors -support -"login" -www.zoominfo.com -www.ibm.com -www.amazon.com`
2. Persona `NOT <phrase>` targeting signals → `-"phrase"`
3. Negative Signal Shield (BQ `Negative_Signals` table) → `-site:domain` + `-intitle:"entity"`
4. Negative RLHF domains/titles from campaign-scoped lead history

**Self-negation prevention (`_deconflict_blacklist()`):**
Before appending the blacklist to each query string, the assembler regex-extracts all positive `site:domain.com` operators from the query body. If the blacklist contains a `-site:domain.com` for the same domain, that exclusion is stripped for that specific query. This prevents Gemini-generated symptom dorks from being silently nullified by the global blacklist.

**Consumer vector guards (V23.5):**
When `_is_consumer_archetype(sourcing_vector)` is True:
- RLHF tenant-wide fallback is skipped entirely (prevents B2B pain point leakage)
- `pain_points` are suppressed → `historical_str` is always empty
- Post-generation B2B jargon scrubber strips any leaked corporate terms from `translated_queries`

Final query always appends Negative Knowledge Graph shield:
```python
blocked_domains, _ = _fetch_neg_shield(tenant_id)  # 3s BQ timeout
neg_ops = " ".join(f"-site:{d}" for d in blocked_domains[:20])
blacklist += f" {neg_ops}"
```

### Step 6: Serper Search Execution

File: `services/pipeline-main/services/serper_service.py::search_serper`

```python
payload = {"q": f"{query} AND {location}", "num": 20, "gl": country_code}
response = httpx.post("https://google.serper.dev/search",
                      headers={"X-API-KEY": serper_key}, data=payload, timeout=30.0)
results = response.json().get("organic", [])
```

Noise filter removes: enterprise aggregators (ibm.com, amazon.com, g2.com, zoominfo.com),
noise URL paths (/legal, /pricing, /docs, /author/, /login), noise snippets ("sign in", "access denied").

### Step 7: Gemini B2B Intent & Geo Gate

All deduplicated Serper snippets pass through Gemini before scraping begins:

```
CRITICAL INTENT CHECK: Is the website EXPERIENCING the problem, or SELLING a solution?
Reject: SEO blogs, competitors, D2C retail, business directories (JustDial, Alibaba, Yelp)
Social Platform Rule: Evaluate the SPECIFIC POST intent, not the platform itself
Geo Rule: If target is '{location}' and site explicitly serves a different region -> REJECT
Output: Line-by-line approved URLs only, each starting with 'http'
```

30-second hard timeout via `concurrent.futures.ThreadPoolExecutor`. On timeout: all URLs promoted
to High confidence tier (pipeline never abandons the batch).

### Step 8: Global Exclusivity Lock + Deduplication

```python
# Cross-tenant exclusivity (14-day window)
lock_ref = db.collection("global_lead_locks").document(lock_entity)
if lock_doc.exists and lock_doc.to_dict().get("locked_until") > now_utc:
    continue  # Domain locked by another tenant
lock_ref.set({"locked_until": now_utc + timedelta(days=14)})

# Tenant dedup (deterministic ID)
lead_id = hashlib.sha256(f"{tenant_id}_{root_domain}".encode()).hexdigest()
doc_ref.create({"status": "processing", ...})  # Raises AlreadyExists if duplicate
```

### Step 9: PrismPipeline - Three-Tier Scraping Strategy

File: `services/pipeline-main/services/prism_pipeline.py`

**Tier 1 - Social Short-Circuit (Free, zero HTTP):**
Social domains (linkedin.com, facebook.com, reddit.com, etc.) -> Use Serper snippet directly.

**Tier 2 - Lightweight httpx Scraper (GeneralDomain):**
```python
r = httpx.get(url, timeout=10)
# BeautifulSoup parsing, WAF detection, tech stack fingerprinting
# If content < 500 chars -> raises ValueError("DEFERRED") -> Tier 3
```

**Tier 3 - Playwright Heavy Scraper (DEFERRED):**
```python
# scraper-heavy/main.py
# 1. Load DECODO_STANDARD_PROXY from Secret Manager
# 2. Launch headless Chromium (--disable-dev-shm-usage --single-process --no-sandbox)
# 3. Abort: image/media/font/stylesheet resource types (prevent OOM)
# 4. Hard 20-second asyncio.wait_for() kill switch
# 5. WAF detected -> re-launch with DECODO_PREMIUM_PROXY (high-cost bypass)
# 6. Strip: script/style/noscript/nav/footer/iframe from DOM
# 7. Harvest mailto: and tel: via JavaScript evaluate
# 8. Queue Cloud Task -> pipeline-main/finalize with full payload
```

### Step 10: RLHF Pre-Screen + Vertex AI Scoring

**A. Python Fast-Fail Gate (Cost Guard):**
```python
global_b2b_blocklist = ['add to cart', 'shopping bag', 'checkout', ...]
dynamic_blocklist = tenant_doc.get("dynamic_blocklist", [])
fail_score = sum(text.lower().count(term) for term in (global_b2b_blocklist + dynamic_blocklist))
if fail_score > 3:
    doc_ref.update({"status": "failed", "error": "Dropped by Python Heuristics"})
    continue  # Never calls Vertex AI
```

**B. RLHF Python Interceptor:**
```python
fit_score = preferences_weights.get("hiring_intent", 0) * native_hiring_intent
for tech in tech_stack:
    fit_score += preferences_weights.get(f"tech_{tech}", 0)
if fit_score <= -3:
    doc_ref.delete()  # Dropped BEFORE Vertex AI -- saves token spend
```

**C. Vertex AI Final Scoring (gemini-2.5-flash):**
Locked to strict JSON schema via `GenerationConfig(response_mime_type="application/json")`.
Returns: score, dm, pain_point, icebreaker_angle, hiring_intent_found, tech_stack_found,
decision_maker_name, decision_maker_title, company_size_tier, primary_objection_hypothesis.

Wrapped with:
- `tenacity` retry: `wait_exponential(min=2, max=10)`, `stop_after_attempt(5)`, on `ResourceExhausted`
- 45-second `ThreadPoolExecutor future.result(timeout=45.0)` kill switch

**Score gate:** Leads scoring `>= 7` -> written as `"new"`. Below 7 -> **deleted from Firestore**.

---

## 7. ORCHESTRATOR REST API REFERENCE

**Base URL:** `https://orchestrator-222247989819.asia-south1.run.app`
**Auth:** `Authorization: Bearer <Firebase ID Token>` for all user-facing endpoints
**CORS:** Strict allowlist -- only `lead-sniper-prod.web.app` and `lead-sniper-prod.firebaseapp.com`

### 7.1 Complete Endpoint Table

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/me` | User | User profile + wallet balance (shard-aggregated) |
| PUT | `/api/me` | User | Update `agreed_to_terms` or `crm_webhook_url` |
| GET | `/api/campaigns` | User | List all tenant campaigns (limit 100) |
| POST | `/api/campaigns` | User | Create new campaign (zero-wait enqueue included) |
| PUT | `/api/campaigns/{id}` | User | Update campaign (tenant ownership enforced) |
| DELETE | `/api/campaigns/{id}` | User | Delete campaign |
| POST | `/api/campaigns/{id}/run` | User | Epsilon-Greedy Router: cache exploit + Serper explore |
| POST | `/api/campaigns/{id}/ignite` | User | Force immediate pipeline dispatch |
| GET | `/api/leads` | User | List all tenant leads (limit 100) |
| PUT | `/api/leads/{id}` | User | Update lead status + RLHF backprop + Shadow Tracker |
| GET | `/api/personas` | User | List all Persona Vault entries |
| POST | `/api/personas` | User | Create new persona |
| PUT | `/api/personas/{id}` | User | Update persona (triggers campaign denormalisation) |
| DELETE | `/api/personas/{id}` | User | Delete persona (409 if campaigns reference it) |
| POST | `/api/settings` | User | Save WhatsApp credentials (KMS encrypted) |
| POST | `/api/tenant_profiles` | User | Save/update Digital Twin company profile |
| POST | `/api/analyze-website` | User | Digital Twin: scan URL + return campaign recommendations |
| GET | `/api/analytics/roi` | User | L1 ROI Matrix (?date_range=N days) |
| PUT | `/api/analytics/unit-economics` | User | Persist custom unit economics |
| GET | `/api/l0/telemetry` | super_admin | Global macro lead counts + all tenant summaries |
| GET | `/api/l0/trends` | super_admin | Active campaigns ranked by leads generated |
| GET | `/api/l0/users` | super_admin | All user profiles with usage metrics |
| POST | `/api/l0/users/suspend` | super_admin | Toggle `is_active` for any tenant |
| POST | `/api/l0/users/{id}/mint` | super_admin | Add credits to tenant wallet |
| POST | `/api/l0/users/{id}/approve` | super_admin | Set approved + mint credits + set expiry |
| POST | `/api/internal/cron/sweep` | OIDC | Master cron: dispatch pipeline tasks |
| POST | `/api/internal/credits/settle` | Cloud Tasks | Atomic credit settlement |
| POST | `/api/internal/telemetry/bq-push` | Cloud Tasks | BQ streaming insert |
| POST | `/api/internal/telemetry/serper-audit` | OIDC/Cloud Tasks | Serper audit log |
| POST | `/purge` | Internal | DPDP compliance: erase all data for a tenant |

### 7.2 Key Endpoint Behaviors

**`POST /api/campaigns`** creates a campaign and immediately dispatches a zero-wait Cloud Task
to `/produce` without waiting for the cron sweep. Sets `zero_wait_enqueued: true` on the campaign doc.

**`PUT /api/leads/{id}`** triggers the full RLHF chain:
1. Updates status + updatedAt
2. RLHF backpropagation (preferences_weights in users doc)
3. If ignored: populates dynamic_blocklist from pain_point keywords
4. If rejection_reason == "Competitor"/"Author": fires async neg_signal -> BQ
5. If status == "approved": fires async shadow_track -> BQ Intent_Keywords

---

## 8. FRONTEND ARCHITECTURE

### 8.1 Technology Stack

- **Runtime:** Vanilla JavaScript (no build step, no framework, no npm)
- **Auth:** Firebase SDK v8 compat (`firebase.auth()`)
- **Database:** Firebase SDK v8 compat (`firebase.firestore()`) -- direct `onSnapshot`
- **Charts:** Chart.js (Doughnut funnel)
- **PWA:** Service Worker + `manifest.json`
- **Hosting:** Firebase Hosting (global CDN)

### 8.2 App Boot Sequence

```javascript
firebase.auth().onAuthStateChanged(async user => {
    if (user) {
        authContainer.classList.add('hidden');
        appContainer.classList.remove('hidden');
        loadDashboard();
    }
});

async function loadDashboard() {
    await Promise.all([
        loadMe(),            // /api/me -> wallet balance, approval status
        loadCampaigns(),     // /api/campaigns -> render campaign list
        loadLeads(),         // -> Firestore onSnapshot subscription
        loadROIDashboard(30) // /api/analytics/roi -> ROI cards
    ]);
}
```

### 8.3 Real-Time Lead Feed (`onSnapshot`)

```javascript
function loadLeads() {
    if (unsubscribeLeads) unsubscribeLeads();  // Cleanup previous listener

    unsubscribeLeads = firebase.firestore()
        .collection('leads')
        .where('tenant_id', '==', user.uid)
        .onSnapshot((snapshot) => {
            rawLeadsCache = [];
            snapshot.forEach(doc => { let d = doc.data(); d.id = doc.id; rawLeadsCache.push(d); });
            rawLeadsCache.sort((a, b) => (b.score || 0) - (a.score || 0));
            renderLeads();
        }, async (error) => {
            if (error.code === 'permission-denied') {
                // J-16 FIX: Force token refresh + re-subscribe after 1h expiry
                const u = firebase.auth().currentUser;
                if (u) { await u.getIdToken(true); setTimeout(loadLeads, 2000); }
            }
        });
}
```

### 8.4 DOM Virtualization (Virtual Observer)

Only viewport-visible leads are rendered -- prevents FPS drops on large lead arrays.

```javascript
let virtualObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting && !entry.target.getAttribute('data-rendered')) {
            const lead = rawLeadsCache.find(l => l.id === entry.target.getAttribute('data-lead-id'));
            const newCard = window.createLeadCardV2(lead.id, lead);
            entry.target.replaceWith(newCard);  // replaceWith preserves scroll position
            virtualObserver.observe(newCard);
            newCard.setAttribute('data-rendered', 'true');
        }
    });
}, { rootMargin: "800px" });  // Pre-load 800px before viewport
```

### 8.5 Lead Card V2 Architecture

**Folded state (default):**
```
[Company URL ->]                              [FIRE 8/10]
AI Match | 2h ago
Complaining about high customer acquisition costs.
[Predictive] [Exclusive] [Hiring] [Competitor: SalesLoft]
v See opening message & full intelligence
[Contact This Lead] [-> CRM] [...]
```

**Expanded state (click):**
```
YOUR OPENING MESSAGE
 Hey [Name], noticed your CAC has been climbing this quarter...
WHY THIS LEAD
 Active hiring for Head of Growth. Prime timing.
LIKELY OBJECTION
 They use HubSpot - may feel they have enough tooling.
```

Score visualization: gradient heat bar (fire emoji 9-10, lightning 7-8, thumbs up 5-6)

Source labels:
- `origin_engine: "autonomous"` -> `AI Match`
- `origin_engine: "cartographer"` -> `Web Signal`

### 8.6 Conversational Campaign Modal ("Find New Clients")

**Step 1:** Natural language intent textarea + quick-start chips + character hint  
**Step 2:** Smart confirmation: parsed "who", product bio, location chips (flag emojis), launch button

Key functions:
- `fcParseIntent(sentence)` -- extracts who/where from natural language using regex
- `fcBuildCampaignName(who, where)` -- auto-generates "target - UK - Apr 2026"
- `fcLaunch()` -- final validation -> populates hidden fields -> calls `saveCampaignAction()`
- Auto geo-detection via `ipapi.co/json/` on modal open

### 8.7 Digital Twin Modal (Website -> Campaign)

4-view wizard:
- **View A:** URL input + Analyze button
- **View B:** Animated progress bar (2.5s animation during API call)
- **View C:** Company profile + 3 target personas + recommended campaign cards
- **View D:** Manual entry fallback (WAF-blocked sites)

Campaign cards stored in `window._pendingCards[idx]` (NOT btoa -- emoji-safe, J-8 fix).

### 8.8 Key JavaScript Functions

| Function | Purpose |
|---|---|
| `loadDashboard()` | Boot: fires me + campaigns + leads + ROI in parallel |
| `loadMe()` | /api/me -> wallet display + greeting |
| `loadLeads()` | Creates Firestore onSnapshot subscription |
| `renderLeads()` | Invokes VirtualObserver with rawLeadsCache |
| `createLeadCardV2()` | Builds full folded lead card DOM element |
| `saveCampaignAction()` | POST /api/campaigns + zero-wait enqueue |
| `deployPredictiveCard()` | Deploy child campaign from _pendingCards[idx] |
| `dtPrefillAndLaunch()` | Launch campaign from Digital Twin View C |
| `saveTenantProfileAction()` | POST /api/tenant_profiles |
| `loadROIDashboard()` | GET /api/analytics/roi -> animate hero cards |
| `fcParseIntent()` | Extract who/where from natural language |
| `openChildCampaignModal()` | Opens child campaign modal + loads predictive cards |
| `populatePersonaDropdown()` | Fills dropdown from _personasMap + pre-selects |

---

## 9. SERVICE WORKER

File: `public/sw.js`  
Cache version: `sideio-v10-3`

**CRITICAL Firebase bypass (v10-3 fix):**
```javascript
self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);
    if (
        url.hostname.includes('googleapis.com') ||
        url.hostname.includes('google.com')     ||
        url.hostname.includes('firestore')
    ) {
        event.respondWith(fetch(event.request));
        return;  // NEVER cache Firestore WebChannel streams
    }
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
```

Why critical: Firestore `onSnapshot` uses long-poll HTTP (WebChannel) that returns opaque,
non-cloneable response bodies. If the SW intercepts them, it throws "Failed to convert value
to Response" causing 30-second disconnect loops. Added in v10-3.

To force SW update: bump `CACHE_NAME` (e.g., `sideio-v10-4`).

---

## 10. RLHF SELF-LEARNING SYSTEM

### 10.1 Signal Flow -- What Happens on Lead Action

```
User clicks Converted / Ignored
    |
    v
PUT /api/leads/{id} -> orchestrator/leads.py
    |
    +-- 1. Update lead status + updatedAt in Firestore
    |
    +-- 2. RLHF Backpropagation (synchronous, Firestore)
    |       delta = +1 (converted) or -1 (ignored)
    |       users/{uid}.preferences_weights.hiring_intent += delta
    |       users/{uid}.preferences_weights.tech_{stack} += delta
    |       If ignored: dynamic_blocklist += pain_point keywords
    |
    +-- 3. Shadow Tracker -- daemon thread (async, BigQuery)
    |       Extracts 2- and 3-gram buyer phrases from pain_point + dm
    |       UPSERT into swarm_analytics.Intent_Keywords
    |       Per-gram: occurrence_count++, yield_weight += 0.1
    |
    +-- 4. Negative Signal -- daemon thread (async, BigQuery)
            Only fires if rejection_reason == "Competitor" or "Author"
            INSERT into swarm_analytics.Negative_Signals
            Future Serper queries: -site:{domain} automatically injected
```

### 10.2 Shadow Tracker (N-gram Accumulator)

```python
def _async_shadow_track(lead_doc, persona_category, tenant_id):
    t = threading.Thread(
        target=_do_shadow_track, args=(lead_doc, persona_category, tenant_id),
        daemon=True  # Killed on process exit -- no orphan threads
    )
    t.start()
    # Returns immediately -- HTTP 200 never waits for BigQuery
```

### 10.3 RLHF Pipeline Pre-screen

```python
# Before calling Vertex AI -- saves token spend
fit_score = 0
if native_hiring_intent:
    fit_score += preferences_weights.get("hiring_intent", 0)
for tech in tech_stack:
    fit_score += preferences_weights.get(f"tech_{tech}", 0)
if fit_score <= -3:
    doc_ref.delete()  # Deleted BEFORE Vertex AI call -- saves cost
```

---

## 11. DIGITAL TWIN ENGINE

### 11.1 Backend: `POST /api/analyze-website`

File: `services/orchestrator/api/routers/settings.py`

```python
# Retry loop: 2 HTTP attempts with 1.5s delay (J-4 fix)
for _attempt in range(2):
    r = httpx.get(url, timeout=httpx.Timeout(connect=4.0, read=7.0))
    if _is_waf_response(raw_html, status_code):
        return jsonify({"error": "WAF_BLOCKED", "code": "WAF_BLOCKED"}), 422
    _fetch_success = True
    break

# Gemini analysis (15s timeout via _call_gemini_bounded)
# Schema validation (J-7 fix): empty response returns 422 GEMINI_EMPTY_RESPONSE
```

WAF fingerprints: "just a moment", "cloudflare", "cf-browser-verification",
"please verify you are human", "ray id", "access denied"

### 11.2 Tenant Profile Schema (`tenant_profiles` collection)

```json
{
  "_id": "firebase_uid",
  "tenant_id": "firebase_uid",
  "bio": "Company description extracted from website",
  "keywords": "First 120 chars of target description",
  "gl": "uk",
  "recommended_campaigns": [
    {"product_name": "...", "market_trend_hook": "...", "unfair_advantage": "..."}
  ],
  "createdAt": "<SERVER_TIMESTAMP>"
}
```

### 11.3 Knowledge Base Upload

```
User uploads PDF/TXT -> Firebase Storage: gs://bucket/knowledge_bases/{tenant_id}/{filename}
     |
     v (app.js calls)
POST /api/tenant_profiles/extract-kb
     |
     v (orchestrator: download from GCS into io.BytesIO)
PyPDF2 strips text -> truncate to 10KB -> firestore.ArrayUnion([extracted_text])
```

---

## 12. AUTONOMOUS ENGINE & EPSILON-GREEDY ROUTER

### 12.1 Autonomous Engine

File: `services/autonomous-engine/engine.py`  
Schedule: `0 2 * * *` (2 AM IST daily)  
Type: Cloud Run JOB -- NOT a Service. No HTTP endpoint.

```python
def main():
    tenants = get_all_active_tenants()  # users where approval_status == approved
    for tenant in tenants:
        signals = harvest_digital_exhaust(tenant)  # Job postings, funding news
        for signal in signals:
            if _can_use_gemini():  # Token kill-switch check
                lead = score_and_cache(signal)
                store_in_predictive_cache(lead)
                update_ontology_map(signal["domain"])

def _can_use_gemini():
    total_calls = sum_usage_shards()  # BQ-based usage tracking
    return total_calls < DAILY_GEMINI_BUDGET  # Default: 1000
```

### 12.2 Epsilon-Greedy Router

Location: `POST /api/campaigns/{id}/run` in `campaigns.py`

```python
batch_size    = 10
exploit_ratio = 0.10  # 10% from cache, 90% from Serper (configurable via DISCOVERY_ALLOCATION)

autonomous_target   = int(batch_size * exploit_ratio)   # = 1
cartographer_target = batch_size - autonomous_target    # = 9

cached_leads        = _pop_from_predictive_cache(tenant_id, autonomous_target)
autonomous_promoted = len(cached_leads)                 # Actual served (may be < target)

# DEFICIT REALLOCATION -- CRITICAL SAFETY GUARANTEE
deficit           = autonomous_target - autonomous_promoted
cartographer_actual = cartographer_target + deficit  # All deficit goes to Serper

# Response:
# {"status": "dispatched", "autonomous_promoted": 1, "cartographer_queued": 9, "total": 10}
```

Safety: Empty cache -> deficit = autonomous_target -> 100% to Serper. No crash.

---

## 13. ONTOLOGY MAP COLLECTION

Purpose: Global domain intelligence repository. Every domain that has ever produced a cached lead
gets an entry. The autonomous engine reads `baseline_weight` to route between exploit/explore.

### 13.1 Document ID Derivation (`parse_base_path`)

MUST be identical in BOTH `autonomous-engine/engine.py` AND `orchestrator/core/helpers.py`:

```python
SOCIAL_DOMAINS = {"reddit.com", "facebook.com", "linkedin.com", "quora.com", ...}

def parse_base_path(url: str) -> str:
    domain = hostname.removeprefix("www.")
    if any(domain.endswith(s) for s in SOCIAL_DOMAINS):
        segments = [s for s in path.split("/") if s]
        return "/".join([domain] + segments[:2])  # "reddit.com/r/Entrepreneur"
    return domain  # "techcrunch.com"
```

### 13.2 RLHF Feedback Loop

```
1. Autonomous engine -> predictive_cache write
   -> ontology_map.update(total_yield++, last_seen)

2. User marks CRM "Won"
   -> if total_yield >= 50: ontology_map.update(baseline_weight += +0.15)  # reward

3. User marks CRM "Lost"
   -> if total_yield >= 50: ontology_map.update(baseline_weight += -0.05)  # penalty

4. Monthly decay cron (POST /api/internal/cron/ontology-decay)
   -> new_weight = weight - (weight - 1.0) * 0.10  # regression to mean
```

Asymmetry: Rewards (+0.15) are 3x magnitude of penalties (-0.05). Biases toward exploration
of weakly-performing domains rather than premature exclusion.

---

## 14. BIGQUERY TELEMETRY TABLES

Dataset: `swarm_analytics` (project: `sideio-leads-v16`, location: `asia-south1`)

### 14.1 `Intent_Keywords` Table

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

Written by: `_async_shadow_track()` daemon thread on every lead approval.
Read by: `generate_smart_query()` with 3-second hard timeout.

### 14.2 `Negative_Signals` Table

```sql
CREATE TABLE IF NOT EXISTS `sideio-leads-v16.swarm_analytics.Negative_Signals` (
    entity_name       STRING    NOT NULL,
    root_domain       STRING    NOT NULL,
    rejection_reason  STRING    NOT NULL,   -- "Competitor" | "Author"
    tenant_id         STRING    NOT NULL,   -- "GLOBAL" for L0 admin overrides
    timestamp         TIMESTAMP NOT NULL    DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY DATE(timestamp)
OPTIONS (partition_expiration_days = 730);
```

Written by: `_async_neg_signal_insert()` daemon thread on rejection.
Read by: `_fetch_neg_shield()` with 3-second hard timeout (fallback: [], []).

### 14.3 `serper_audit_logs` Table

Written by pipeline-main after every Serper API call via POST /api/internal/telemetry/serper-audit.

Columns: timestamp, campaign_id, tenant_id, raw_query, serper_parameters (JSON string),
result_count, credit_cost, engine, serper_status_code, error_message

---

## 15. L1 ROI & ANALYTICS MATRIX

### 15.1 Financial Models

| Metric | Formula | Default Benchmark Source |
|---|---|---|
| Ad Spend Saved | `N * avg_cpl` | HubSpot State of Marketing 2024 ($50 B2B avg CPL) |
| Labor Hours Saved | `(N * 15 min / 60) * sdr_hourly_rate` | BLS: 15 min avg manual SDR time/lead |
| Total Value Offset | `ad_savings + labor_savings` | Combined |
| Pipeline Value | `N * est_conversion_rate * avg_deal_size` | **$0 if avg_deal_size == 0** |
| ROI Ratio | `total_offset / (N * $0.10)` | $0.10 estimated cost per lead |

N = count of converted leads in date_range window.

Credibility Guard: `pipeline_value` stays $0 until tenant sets `avg_deal_size > 0`.
Both backend AND frontend enforce this. Both guards must stay in sync.

### 15.2 Endpoints

```
GET /api/analytics/roi?date_range=30   -> ROI metrics JSON
PUT /api/analytics/unit-economics      -> Update benchmark values
```

---

## 16. WHATSAPP HOT LEAD ALERTS

Triggered automatically when a lead scores >= 8:

```python
wa_payload = {
    "messaging_product": "whatsapp",
    "to": admin_phone,
    "type": "interactive",
    "interactive": {
        "type": "button",
        "body": {"text": f"FIRE Hot Lead!\nCompany: {url}\nScore: {score}/10\n..."},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": f"approve_{lead_id}", "title": "Approve & Send"}},
                {"type": "reply", "reply": {"id": f"ignore_{lead_id}", "title": "Ignore"}}
            ]
        }
    }
}
httpx.post(f"https://graph.facebook.com/v18.0/{wa_phone_id}/messages",
           json=wa_payload, headers={"Authorization": f"Bearer {wa_token}"}, timeout=5)
```

WhatsApp token is KMS-encrypted at rest (see Section 5.4). Button replies handled by
`whatsapp-webhook` Cloud Run service.

---

## 17. CI/CD PIPELINE

File: `cloudbuild.yaml`
Trigger: Push to `main` branch

### 17.1 Build Steps (Parallelized)

```
Step Group 1 (parallel -- all fire at once):
  syntax-check-appjs          -> node:20-slim validation
  python-v23-smoke-gate       -> local_smoke_tests.py (fails fast on config errors)
  build-orchestrator          -> gcr.io/$PROJECT_ID/lead-orchestrator
  build-pipeline-main         -> gcr.io/$PROJECT_ID/lead-pipeline-main
  build-scraper-heavy         -> gcr.io/$PROJECT_ID/scraper-heavy
  build-whatsapp-webhook      -> gcr.io/$PROJECT_ID/whatsapp-webhook
  build-email-summary         -> gcr.io/$PROJECT_ID/email-summary
  build-autonomous-engine     -> gcr.io/$PROJECT_ID/autonomous-engine
  firebase-deploy             -> Firebase Hosting + Firestore rules/indexes

Step Group 2 (parallel -- after Group 1):
  push all Docker images to GCR

Step Group 3 (parallel -- after Group 2):
  deploy-orchestrator         -> Cloud Run Service (allow-unauthenticated)
  deploy-pipeline-main        -> Cloud Run Service (no-allow-unauthenticated, SA: lead-pipeline-sa)
  deploy-scraper-heavy        -> Cloud Run Service (2Gi memory)
  deploy-whatsapp-webhook     -> Cloud Run Service (allow-unauthenticated)
  deploy-email-summary        -> Cloud Run Service (no-allow-unauthenticated)
  deploy-autonomous-engine    -> Cloud Run Job (gcloud run jobs deploy)

Step Final (sequential):
  gcloud-job-provision-autonomous-engine  -> Create/update Cloud Run Job
  gcloud-job-scheduler-autonomous-engine  -> Create/update Cloud Scheduler (idempotent)
```

### 17.2 Smoke Gate Assertions (`local_smoke_tests.py`)

- `SERPER_API_KEY_NAME` must be exactly `SERPER_API_KEY` (uppercase). Lowercase causes 403.
- All Blueprint modules importable
- Config constants resolve correctly

### 17.3 Idempotent Scheduler Provisioning

```bash
gcloud scheduler jobs create http lead-sniper-nightly --schedule="0 2 * * *" --quiet || \
gcloud scheduler jobs update http lead-sniper-nightly --schedule="0 2 * * *" --quiet
# Prevents Cloud Build failure on 2nd+ deploys when job already exists
```

---

## 18. ERROR HANDLING & RESILIENCE PATTERNS

### 18.1 Universal Loop Crash Handler (Pipeline)

```python
try:
    # cache check, scraping, RLHF, Vertex scoring, doc write
except Exception as loop_e:
    print(f"Pipeline crashed: {loop_e}")
    db.collection("leads").document(lead_id).update({"status": "failed"})
    continue  # Never hangs in "processing" state
```

### 18.2 Circuit Breaker (Cron Sweep)

If `serper_error_rate > 15%` OR `scraper_oom_rate > 5%`:
```python
return jsonify({"error": "Circuit breaker open"}), 503
# Cloud Scheduler retries automatically. Prevents Serper overspend.
```

### 18.3 Vertex AI Timeout + Rate Limit Retry

```python
@retry(wait=wait_exponential(min=2, max=10), stop=stop_after_attempt(5),
       retry=retry_if_exception_type(ResourceExhausted))
def _invoke_model():
    return model.generate_content(prompt, generation_config=config)

future = executor.submit(_invoke_model)
response = future.result(timeout=45.0)  # Hard 45s kill switch
```

### 18.4 Lazy Firestore Init (Pre-Fork Deadlock Prevention)

Problem: `db = firestore.Client()` at module scope -> initialized before Gunicorn fork ->
child workers inherit open gRPC channel -> mutex contention -> indefinite hangs.

Fix: All handlers call `get_db()` which resolves lazily via double-checked locking:
```python
_db_instance = None
_db_lock = threading.Lock()

def get_db():
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                _db_instance = firestore.Client()
    return _db_instance
```

### 18.5 Timestamp Handling

All campaign timestamps stored as ISO-8601 strings, NOT Firestore Timestamp objects:
```python
campaign_ref.update({"next_produce_due": (now_utc + timedelta(hours=24)).isoformat()})
```
Legacy documents with DatetimeWithNanoseconds are handled via `hasattr(.timestamp)` check.

---

## 19. DEPENDENCIES & REQUIREMENTS

### 19.1 orchestrator/requirements.txt

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
httpx>=0.26.0           # NOT pinned to ==0.26.0 (google-genai requires newer)
vertexai                 # For classify_sourcing_vector (Synaptic Router)
structlog                # JSON structured logging
```

### 19.2 pipeline-main/requirements.txt

```
google-cloud-firestore==2.14.0  # Pinned: prevents grpcio version conflicts
google-cloud-secret-manager>=2.16.2
grpcio==1.60.0
google-cloud-aiplatform>=1.45.0
firebase-admin>=6.5.0
Flask==3.0.0
gunicorn==21.2.0
httpx>=0.26.0
beautifulsoup4==4.12.3
cryptography==41.0.7
tenacity==8.2.3          # Vertex AI retry with exponential backoff
google-cloud-tasks>=2.14.2
google-cloud-bigquery
```

### 19.3 scraper-heavy/requirements.txt

```
playwright==1.42.0      # After install: playwright install chromium
Flask==3.0.0
gunicorn==21.2.0
google-cloud-tasks==2.14.2
google-cloud-secret-manager>=2.16.2
```

---

## 20. INTERN REBUILD CHECKLIST

### Phase 1: GCP Project Setup (~2 hours)

```
[ ] Create GCP project: sideio-leads-v16
[ ] Enable APIs: Cloud Run, Cloud Tasks, Cloud Firestore, Secret Manager,
    Cloud KMS, Cloud Build, Vertex AI, BigQuery, Cloud Scheduler
[ ] Create service accounts:
    - lead-pipeline-sa (for pipeline-main + Cloud Tasks)
    - scraper-heavy-sa, whatsapp-webhook-sa, email-summary-sa
[ ] Grant IAM to lead-pipeline-sa:
    - roles/run.invoker on lead-pipeline-main
    - roles/cloudtasks.enqueuer on lead-pipeline-queue
    - roles/secretmanager.secretAccessor
    - roles/bigquery.dataEditor on swarm_analytics
[ ] Create Cloud Tasks queue: lead-pipeline-queue (region: asia-south1)
[ ] Create Firestore database in Native mode (asia-south1)
[ ] Create BigQuery dataset: swarm_analytics (asia-south1)
[ ] Create BQ tables:
    - Intent_Keywords (see Section 14.1 for DDL)
    - Negative_Signals (see Section 14.2 for DDL)
    - serper_audit_logs
[ ] Store secrets in Secret Manager:
    - SERPER_API_KEY (exact uppercase - smoke test enforces this)
    - FIREBASE_SA_KEY (service account JSON for Firebase deploy)
    - DECODO_STANDARD_PROXY
    - DECODO_PREMIUM_PROXY
    - kms_wa_key_path
```

### Phase 2: Firebase Setup (~30 minutes)

```
[ ] Create Firebase project: lead-sniper-prod (link to sideio-leads-v16 GCP project)
[ ] Enable Google Sign-In in Firebase Auth Console
[ ] Set Firebase Hosting public dir to: public/
[ ] Update ALLOWED_ORIGINS in orchestrator core/config.py with your Firebase URLs
[ ] Deploy Firestore security rules:
    firebase deploy --only firestore
    NOTE: Files must NOT have BOM. Re-save with PowerShell: Set-Content -Encoding UTF8
[ ] Enable Firestore TTL MANUALLY:
    GCP Console -> Firestore -> Indexes -> TTL
    Collection: predictive_cache | Field: expire_at
[ ] Note your Firebase config object from Project Settings -> Your Apps
```

### Phase 3: Deploy via Cloud Build (~30 minutes)

```
[ ] Connect GitHub repo to Cloud Build trigger on main branch
[ ] Set Cloud Build substitution variables:
    _PROJECT_ID: sideio-leads-v16
    _REGION: asia-south1
    _FIREBASE_PROJECT: lead-sniper-prod
    _PIPELINE_SA_EMAIL: lead-pipeline-sa@sideio-leads-v16.iam.gserviceaccount.com
    _PIPELINE_URL: https://lead-pipeline-main-[hash].asia-south1.run.app/dispatch
    _SCRAPER_URL: https://scraper-heavy-[hash].asia-south1.run.app/scrape
    _ORCH_URL: https://orchestrator-[hash].asia-south1.run.app
[ ] Push to main -> Cloud Build auto-deploys all services in parallel
[ ] Get Cloud Run URLs after deploy, update substitution vars if URLs changed
```

### Phase 4: Configure Cloud Scheduler (~10 minutes)

```
[ ] Verify Cloud Build created the sweep job, or create manually:
    gcloud scheduler jobs create http pipeline-sweep \
      --location=asia-south1 \
      --schedule="*/5 * * * *" \
      --uri="https://[orchestrator-url]/api/internal/cron/sweep" \
      --oidc-service-account-email="lead-pipeline-sa@..." \
      --oidc-token-audience="https://[orchestrator-url]"
[ ] Add IAM policy for OIDC:
    gcloud run services add-iam-policy-binding orchestrator \
      --member="serviceAccount:lead-pipeline-sa@..." \
      --role="roles/run.invoker"
```

### Phase 5: Verify Pipeline is Working

```
[ ] Open Firebase Hosting URL -> sign in with Google
[ ] Check Firestore: users/{uid} created with approval_status: "pending"
[ ] Approve self: POST /api/l0/users/{uid}/approve with {"amount": 20000, "days": 180}
    (You need a super_admin account - set role in Firestore Console first)
[ ] Create a campaign in the UI
[ ] Wait max 5 minutes for cron sweep
[ ] Check Cloud Logging:
    - Orchestrator: look for "QUEUED Campaign" log entries
    - Pipeline-main: look for "TRACE-1" through "TRACE-9" sequence
    - Pipeline-main: look for "Gemini approved X URLs"
[ ] Check Firestore leads collection: docs with status: "new"
[ ] Verify frontend onSnapshot: leads appear without page refresh
[ ] Check global_lead_locks: 14-day lock entries being written
```

### Phase 6: Onboard First Real Tenant

```
[ ] Tenant signs in -> auto-created with approval_status: "pending"
[ ] L0 admin calls:
    POST /api/l0/users/{uid}/approve
    Body: {"amount": 20000, "days": 180}
    This sets approval_status: "approved" AND mints 20,000 credits AND sets beta_expiry
[ ] Tenant creates first campaign using "Find New Clients" modal
[ ] Verify wallet balance decrements as leads are generated
```

---

## 21. DESIGN INVARIANTS -- NEVER BREAK THESE

These are hard constraints. Breaking any one causes silent data corruption, billing overruns, or security holes.

1. **Firestore rules:** `leads` and `campaigns` are the ONLY collections the frontend reads/writes.
   All other collections are Admin SDK only (backend).

2. **Tenant isolation:** Every document a tenant touches MUST have `tenant_id == user.uid`.
   Enforced in Firestore rules AND in the Orchestrator auth middleware.

3. **Lead dedup ID:** Always `sha256(tenant_id + '_' + root_domain)`. NEVER auto-generated.
   Changing this formula breaks all deduplication.

4. **Score gate:** Only leads scoring `>= 7` survive. Everything below is DELETED
   (not status: "failed" -- deleted from Firestore).

5. **Service Worker Firebase bypass:** NEVER let the SW intercept googleapis.com or google.com.
   Firestore WebChannel streams are not cacheable.

6. **Wallet balance formula:** `max(total_consumed, consumed_credits + SUM(wallet_shards))`.
   Do not read only one path -- both legacy and new paths coexist during migration.

7. **OIDC for cron:** `/api/internal/cron/sweep` validates OIDC via `google.oauth2.id_token`.
   NEVER use Firebase ID tokens for cron. Firebase tokens have wrong issuer.

8. **Campaign name auto-generation:** `fcBuildCampaignName()` generates the name.
   Never show a "Campaign Name" input to the user.

9. **Daemon threads for BQ:** Shadow Tracker and Negative Signal threads MUST be `daemon=True`.
   Without it, they outlive Flask workers on scale-to-zero and hold open BQ billing sessions.

10. **BQ timeouts are hard 3 seconds:** `_fetch_neg_shield()` and confidence router MUST use
    `concurrent.futures.ThreadPoolExecutor(timeout=3.0)`. Removing this pushes BQ latency into
    Serper query time causing pipeline timeouts.

11. **`target_urls` is dead:** Never read in the pipeline producer. Removed in V22 because
    the `site:domain` injection loop overrode Gemini's intent keywords.

12. **Autonomous engine is a Cloud Run JOB, not a Service:** No HTTP endpoint.
    Only triggered via `gcloud run jobs execute` through Cloud Scheduler.

13. **`predictive_cache` TTL:** Firestore TTL on `expire_at` must be enabled MANUALLY in GCP Console.
    Cloud Build cannot enable it. Default TTL: 72 hours.

14. **`parse_base_path()` must be identical in both services:** Lives in `autonomous-engine/engine.py`
    AND `orchestrator/core/helpers.py`. If they drift, RLHF writes target different doc IDs than
    routing reads -- breaking the feedback loop silently.

15. **Serper secret name is uppercase:** `SERPER_API_KEY` not `serper_api_key`.
    The smoke gate in `local_smoke_tests.py` enforces this on every build.

16. **Pipeline Value stays $0 until `avg_deal_size > 0`:** Both backend (analytics_service.py)
    and frontend (roi-pipeline-sub element) enforce this. Keep in sync.

17. **All timestamps as ISO-8601 strings:** Use `.isoformat()`. Firestore SDK can silently fail
    on Python `datetime` objects in `update()` on some SDK versions.

18. **CORS preflight returns 204 before auth check:** OPTIONS requests handled before `require_auth`.
    Otherwise browsers get CORS error on preflight and every API call fails.

19. **`tenant_profiles` is NEVER read at runtime by the pipeline.** (V23.5) The `dispatch.py` execution
    path has zero runtime coupling to the `tenant_profiles` collection. All persona data must be
    snapshot-copied into the campaign document at creation time. Any fallback to `tenant_profiles`
    causes cross-campaign branding leakage.

20. **Consumer archetype detection uses `_CONSUMER_ARCHETYPES` frozenset.** (V23.5) The canonical
    set `{"B2C", "B2B2C", "D2C"}` is defined in `query_brain.py` (pipeline-main), `helpers.py`
    (orchestrator), and `serper_service.py` (inline). All three MUST stay in sync.
    The function `_is_consumer_archetype()` is the single source of truth for consumer routing.

21. **Blacklist must never self-negate positive site: operators.** (V23.5) The `_deconflict_blacklist()`
    function in `query_brain.py` strips conflicting `-site:` exclusions from the blacklist when the
    query body already contains a positive `site:` for the same domain. Removing this function will
    cause Gemini-generated symptom dorks targeting specific sites to be silently nullified.

---

## 22. V22 AMPUTATION RECORD

Features permanently removed in V22 (commit `6f60251`, 2026-04-17):

| Feature | Files | Lines | Why |
|---|---|---|---|
| "Suggest up to 10 websites" textarea | index.html | 5 | UI field degraded query quality |
| `target_urls` DOM read | app.js | 6 | Dead code after textarea removed |
| `target_urls` in POST/PUT payload | app.js | 2 | No longer needed |
| `urlsEl` pre-fill in openEditModal | app.js | 5 | DOM element no longer exists |
| **`site:domain1 OR site:domain2` injection loop** | pipeline-main/main.py | **7** | **ROOT CAUSE: injecting user-submitted domains into Serper overrode intent keywords generated by the Hybrid Starter Motor. Google SERP API silently dropped N-gram operators when query exceeded token limits due to domain expansion. Removing restored full N-gram signal fidelity.** |

Total: 27 lines across 3 files.

`target_urls` still exists as a field in legacy Firestore campaign documents and in the PUT
/api/campaigns/{id} handler (stored if sent) -- it is simply never read by the producer.
No migration script required. This is a deliberate soft migration.

---

## APPENDIX A: FIRESTORE COMPOSITE INDEX

```json
{
  "collectionGroup": "leads",
  "fields": [
    { "fieldPath": "tenant_id", "order": "ASCENDING" },
    { "fieldPath": "timestamp", "order": "DESCENDING" }
  ]
}
```

Deploy: `firebase deploy --only firestore:indexes`

## APPENDIX B: KEY FILE -> FUNCTION CROSS-REFERENCE

| File | Key Functions |
|---|---|
| orchestrator/main_v23.py | create_app() -- Blueprint registry, CORS middleware |
| orchestrator/core/helpers.py | check_quota(), reserve_credits(), classify_sourcing_vector(), is_consumer_archetype(), _atomic_settle_txn(), _async_shadow_track() |
| orchestrator/api/routers/internal.py | cron_sweep(), bq_push(), serper_audit(), settle_credits() |
| orchestrator/api/routers/campaigns.py | create_campaign(), ignite_campaign(), consume_campaign() |
| orchestrator/api/routers/settings.py | analyze_website() (J-4 retry + J-7 schema validation) |
| pipeline-main/api/routers/produce.py | produce() -- Serper search + dedup + queue write |
| pipeline-main/api/routers/dispatch.py | dispatch() -- Gemini gate + PrismPipeline + score + write |
| pipeline-main/services/query_brain.py | generate_smart_query(), _is_consumer_archetype(), _deconflict_blacklist() -- Hybrid Confidence Router |
| pipeline-main/services/prism_pipeline.py | process_url() -- WalledGarden/B2B2C/GeneralDomain routing |
| pipeline-main/services/serper_service.py | search_serper(), filter_serper_noise(), deep_context_serper_dork(), _is_consumer_archetype() |
| autonomous-engine/engine.py | main(), _can_use_gemini(), _validate_and_cache() |
| public/app.js | loadDashboard(), loadLeads(), createLeadCardV2(), dtPrefillAndLaunch(), deployPredictiveCard(), openChildCampaignModal() |

---

*Document compiled from: architecture.md (V22, 2464 lines), architecture1.md (V23, 419 lines),
and direct codebase inspection of 25+ source files.*
*Current version: V23.5 | Compiled: 2026-06-08 | Updated: 2026-06-21*

---

## APPENDIX C: V23.5 CHANGELOG — SOURCING VECTOR ARCHETYPE REFACTOR

### Motivation

Pre-V23.5, the `sourcing_vector` field was constrained to a 4-value hardcoded industry-specific enum:
`Social/Forum Listening | Review Hijacking | Classic B2B | Maps/GMB Targeting`.

This caused **total lead starvation** for non-B2B campaigns (Real Estate, Dental, Automotive) because:
1. The classifier enum had zero consumer vectors — every campaign was force-classified as B2B.
2. `CHILD_CAMPAIGN_OVERRIDE` campaigns were hardcoded to `"Classic B2B"` — bypassing Gemini entirely.
3. The downstream pipeline had complete consumer routing support (prompts, jargon scrubbers, URL-path dedup) gated on `{"b2c", "real estate", "property"}` — but these values could never be produced.

### Solution: Business-Motion Archetypes

Replaced the industry-specific enum with 4 dynamic business-motion archetypes:

| Archetype | Meaning | Consumer? | Pipeline Behavior |
|-----------|---------|:---------:|--------------------|
| `B2B` | Sells to businesses | No | Standard B2B prompt, domain-level dedup, full RLHF |
| `B2C` | Sells to end consumers | **Yes** | Consumer prompt, URL-path dedup, pain point suppression, B2B jargon scrubber |
| `B2B2C` | Sells through intermediaries | **Yes** | Consumer routing |
| `D2C` | Direct-to-consumer brand | **Yes** | Consumer routing |

Legacy values (`Classic B2B`, `Social/Forum Listening`, etc.) are **backwards-compatible** — `is_consumer_archetype()` returns `False` for all of them.

### 6.5 Sourcing Vector Archetype System & Consumer Routing

#### Classification Flow

```
Campaign creation (campaigns.py)
  ├── bio != "CHILD_CAMPAIGN_OVERRIDE"
  │     └── classify_sourcing_vector(bio, weights) → Gemini → "B2B" | "B2C" | "B2B2C" | "D2C"
  │
  └── bio == "CHILD_CAMPAIGN_OVERRIDE"
        └── effective_bio = focus + pain + advantage
              └── classify_sourcing_vector(effective_bio, weights) → Gemini → archetype
                   (was hardcoded to "Classic B2B" before V23.5)
```

#### Consumer Detection (Single Source of Truth)

```python
# Canonical frozenset (defined in helpers.py, query_brain.py, serper_service.py)
_CONSUMER_ARCHETYPES = frozenset({"B2C", "B2B2C", "D2C"})

def _is_consumer_archetype(vector: str) -> bool:
    return (vector or "").upper().strip() in _CONSUMER_ARCHETYPES
```

#### Consumer Routing Guards (Pipeline-Main)

| Guard | File | Trigger | Effect |
|-------|------|---------|--------|
| RLHF skip | query_brain.py | Consumer + no campaign_id | Skips tenant-wide RLHF fetch |
| Consumer prompt | query_brain.py | `_is_consumer_archetype(vector_label)` | B2C-specific prompt with dork-format enforcement |
| VECTOR GUARD | query_brain.py | Consumer vector | System instruction forbidding B2B jargon |
| QUERY FORMAT GUARD | query_brain.py | Consumer vector | Enforces Boolean dork format for symptom_dorks |
| Pain point suppression | query_brain.py | Consumer vector | Forces `historical_str = ""` |
| B2B jargon scrubber | query_brain.py | Consumer vector | Post-generation filter strips "brand story", "positioning", etc. |
| URL-path dedup | produce.py, dispatch.py | Consumer vector | Uses URL-path key instead of domain-level for dedup |
| Enrichment gate | serper_service.py | Consumer vector | Skips deep_context_serper_dork (no B2B enrichment) |

### Files Changed in V23.5

| Commit | File | Change |
|--------|------|--------|
| `534e2bb` | query_brain.py | Consumer prompt branch + VECTOR GUARD system instruction |
| `f74ee8f` | query_brain.py | Consumer pain point suppression + B2B jargon scrubber |
| `6cf7e48` | produce.py | 4-layer ingestion sanitizer + campaign isolation |
| `9ed7c18` | dispatch.py | Removed live read-through to `tenant_profiles` |
| `150a175` | helpers.py, campaigns.py, internal.py, query_brain.py, produce.py, dispatch.py, serper_service.py | Archetype refactor: replaced rigid enum with B2B/B2C/B2B2C/D2C |
| `b864a93` | serper_service.py | Inlined `_is_consumer_archetype` for smoke test isolation |
| `8c0f45c` | query_brain.py | Boolean dork enforcement, self-negation prevention, QUERY FORMAT GUARD |

### Migration Scripts

| Script | Purpose |
|--------|---------|
| `migration/patch_sourcing_vector.py` | Patches specific campaigns to clean archetypes + scans for legacy values |
| `migration/scrub_oman_b2b_arrays.py` | Clears zombie B2B strings from `pain_points`, `features`, `value_propositions` arrays |

---

## APPENDIX D: SCHEMA BOUNDARY ISOLATION (V23.5)

Strict unidirectional data boundary enforced between three Firestore domains:

```
tenant_profiles (Digital Twin / Core Identity)
      │
      │ READ-ONLY at campaign creation time (snapshot-copy)
      │ ZERO runtime access from pipeline-main
      ▼
campaigns (Execution State)
      │
      │ Denormalized persona_bio, persona_keywords
      │ Independent sourcing_vector (classified per-campaign)
      ▼
leads (Output Artifacts)
      │
      │ Stores sourcing_vector from parent campaign
      │ NEVER reads back to tenant_profiles or campaigns
```

**Enforced invariants:**
- `dispatch.py` has ZERO `tenant_profiles` collection references at runtime
- `produce.py` context is instantiated fresh inside each campaign loop (`CampaignQueryContext`)
- Persona data is snapshot-copied into the campaign document — never fetched live from the Persona Vault during pipeline execution

