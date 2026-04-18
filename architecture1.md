# Sideio Leads — V23 Architecture Reference

> **Status:** Production (Cloud Run · asia-south1)  
> **Last updated from codebase:** Commit `dec5b06` (2026-04-17)

---

## 1. System Overview

Sideio Leads is a multi-tenant, enterprise B2B lead-generation SaaS. It discovers potential clients by running AI-driven search queries via the Serper API, scrapes the resulting websites, scores them against tenant-defined Personas, and delivers ranked leads to the tenant dashboard. The system is fully asynchronous — no component blocks a web request to do multi-second work.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Frontend (Firebase Hosting — PWA)                                          │
│  public/  — HTML/CSS/JS SPA served from Firebase CDN                       │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │ HTTPS (Firebase Auth JWT)
                           ▼
┌──────────────────────────────────────────────────────────────────────────── ┐
│  Orchestrator  (Cloud Run · orchestrator · asia-south1)                     │
│  Flask app with Blueprint routers. Public-facing REST API + internal        │
│  cron/task webhooks.                                                        │
└───┬──────────────┬──────────────────────┬────────────────────────────────── ┘
    │ Firestore    │ Cloud Tasks enqueue  │ BigQuery streaming insert
    ▼              ▼                      ▼
┌──────────┐  ┌───────────────┐      ┌──────────────────────────────────────┐
│Firestore │  │ Cloud Tasks   │      │ BigQuery — sideio_telemetry dataset  │
│(default) │  │ lead-pipeline │      │  Tables: neg_signals, shadow_track,  │
│          │  │    -queue     │      │  ontology_decay, rag_embeddings       │
└──────────┘  └──────┬────────┘      └──────────────────────────────────────┘
                     │ HTTP POST with OIDC token (SA: lead-pipeline-sa)
                     ▼
┌──────────────────────────────────────────────────────────────────────────── ┐
│  Pipeline-Main  (Cloud Run · lead-pipeline-main · asia-south1)              │
│  Flask app. Worker service: runs Serper, Gemini, and Playwright scraping.   │
│  --no-allow-unauthenticated  (Cloud Run IAM requires OIDC)                  │
└───┬──────────────┬───────────────┬────────────────────────────────────────  ┘
    │ Secret Mgr   │ Vertex AI     │ Serper API / httpx scraping
    ▼              ▼               ▼
┌──────────┐  ┌──────────┐   ┌──────────────┐
│ Secret   │  │ Vertex AI│   │ google.serper│
│ Manager  │  │ us-central1│  │ .dev/search  │
└──────────┘  └──────────┘   └──────────────┘
```

---

## 2. Cloud Run Services

| Service | Image tag | Auth | Memory | Concurrency |
|---|---|---|---|---|
| `orchestrator` | `lead-orchestrator:$SHA` | allow-unauthenticated | 512 Mi | 80 |
| `lead-pipeline-main` | `lead-pipeline-main:$SHA` | **no-allow-unauthenticated** | auto | 80 |
| `scraper-heavy` | `lead-scraper-heavy:$SHA` | internal | auto | auto |
| `digital-twin-engine` | `lead-digital-twin:$SHA` | internal | auto | auto |
| `autonomous-engine` | `lead-autonomous-engine:$SHA` | internal | auto | auto |
| `shadow-learner-aggregator` | `lead-shadow-learner:$SHA` | internal | auto | auto |
| `email-summary` | `lead-email-summary:$SHA` | internal | auto | auto |
| `whatsapp-webhook` | `lead-whatsapp-webhook:$SHA` | allow-unauthenticated | auto | auto |

All services run in `asia-south1`. Cloud Build CI/CD is triggered on push to `main`.

---

## 3. Orchestrator (`services/orchestrator/`)

### 3.1 Entry Points

| File | Purpose |
|---|---|
| `main_v23.py` | Production Gunicorn entry — `gunicorn main_v23:app` |
| `main_legacy.py` | Legacy entry (kept for rollback) |

### 3.2 Blueprints (API Routers)

| Module | Prefix | Auth | Key Endpoints |
|---|---|---|---|
| `internal.py` | `/api/internal` | OIDC / X-CloudTasks-QueueName | `/cron/sweep`, `/cron/reflection`, `/cron/ontology-decay`, `/credits/settle`, `/telemetry/bq-push`, `/purge` |
| `campaigns.py` | `/api/campaigns` | Firebase JWT | CRUD campaigns, launch, DT child campaigns |
| `leads.py` | `/api/leads` | Firebase JWT | List leads, markConverted, pushToCRM |
| `personas.py` | `/api/personas` | Firebase JWT | Persona Vault CRUD |
| `settings.py` | `/api/settings` | Firebase JWT | Tenant profile, wallet, preferences |
| `me.py` | `/api/me` | Firebase JWT | Current user profile |
| `analytics.py` | `/api/analytics` | Firebase JWT | ROI, pipeline stats, telemetry |
| `data_reads.py` | `/api/data` | Firebase JWT | Misc read endpoints |
| `l0_admin.py` | `/api/l0` | super_admin role | Tenant management, credit allocation, system config |

### 3.3 Core Modules

| Module | Purpose |
|---|---|
| `core/config.py` | All env vars: `PROJECT_ID`, `LOCATION`, `QUEUE`, `PIPELINE_URL`, `ORCHESTRATOR_SA_EMAIL`, Fernet key |
| `core/clients.py` | Lazy Firestore + Cloud Tasks singletons (thread-safe, post-fork safe) |
| `core/helpers.py` | `check_quota`, `reserve_credits`, `_atomic_settle_txn`, `_handle_bq_push_task`, `handle_purge`, Gemini wrapper |
| `core/logging.py` | structlog JSON logger — all events go to Cloud Logging |
| `core/auth.py` | Firebase token verification middleware |
| `core/exceptions.py` | Domain exceptions |
| `api/middleware.py` | CORS preflight, auth injection |

### 3.4 Services Layer

| Module | Purpose |
|---|---|
| `services/auth_service.py` | Firebase Admin SDK token validation |
| `services/analytics_service.py` | ROI computation, pipeline_value, sdr_hours_saved |
| `services/intelligence/neg_signal.py` | Negative signal recording → BigQuery |
| `services/intelligence/shadow_tracker.py` | Shadow lead tracking for ML training |

### 3.5 Repository Layer

| Module | Purpose |
|---|---|
| `repositories/firestore_repo.py` | Typed Firestore read/write wrappers |

---

## 4. Pipeline-Main (`services/pipeline-main/`)

The worker service. Never receives public traffic — all requests arrive via Cloud Tasks with OIDC tokens issued by `lead-pipeline-sa`.

### 4.1 Key Routes

| Route | Method | Purpose |
|---|---|---|
| `GET /health` | GET | Liveness probe |
| `POST /produce` | POST | **Producer** — Serper search + dedup, writes URLs to `unprocessed_queue` |
| `POST /dispatch` | POST | **Consumer** — Pops URLs, runs Gemini scoring + scraping, writes leads |
| `POST /finalize` | POST | Gemini enrichment, DM generation, credit settlement |

Both `/produce` and `/dispatch` guard with `X-CloudTasks-QueueName` header check as defense-in-depth.

### 4.2 gRPC Lazy Init (Pre-Fork Deadlock Fix)

Three singletons are initialized **lazily** (post-Gunicorn fork) via `threading.Lock()` double-checked locking:

```python
db         → get_db()          # firestore.Client()
sm_client  → get_sm_client()   # secretmanager.SecretManagerServiceClient()
vertexai   → ensure_vertexai_init()  # vertexai.init(location="us-central1")
```

### 4.3 Producer Pipeline (`/produce`)

```
Payload: {tenant_id, campaign_id}
│
├── TRACE-1..9 (granular logging points)
│
├── 1. Fetch campaign doc from Firestore
├── 2. Resolve bio + keywords (Persona Vault fields → legacy fields)
│       persona_bio → campaign.bio
│       persona_keywords → campaign.keywords
│
├── 3. generate_smart_query()  [Vertex AI Gemini 2.5 Flash, 45s timeout]
│       → BQ: fetch top-performing keyword n-grams for this tenant
│       → Gemini: expand keywords into dork-style search queries
│       → Returns list of ~5 smart Serper queries
│
├── 4. search_serper()  [httpx, timeout=30s, tenacity retry on 429]
│       per smart_keyword:
│         → POST https://google.serper.dev/search
│         → Secret Manager: SERPER_API_KEY (uppercase)
│         → filter_serper_noise()  — removes low-signal results
│         → social URLs → scraped_cache (snippet hand-off for consumer)
│
├── 5. Global deduplication
│       Hash: SHA256(tenant_id + domain)  B2B  |  SHA256(tenant_id + full_url)  social
│       Checks existing `leads` collection
│
├── 6. Write fresh_urls → campaign.unprocessed_queue (Firestore merge, cap 200)
│
└── 7. _async_gcs_dump() — daemon thread → GCS sideio-raw-firehose-lake
```

### 4.4 Consumer Pipeline (`/dispatch`)

```
Payload: {tenant_id, campaign_id}
│
├── Guarded: X-CloudTasks-QueueName header
│
├── 1. Fetch campaign doc (try/except + log on Firestore timeout)
├── 2. Fetch all active campaigns for tenant (swarm context)
├── 3. Persona injection (persona_bio → bio)
├── 4. Fetch user preferences (try/except, fallback={})
│
├── 5. Destructive queue pop (batch=10 URLs, atomic Firestore write)
│
├── 6. Snippet hydration from scraped_cache (Producer hand-off)
│
├── 7. pre_filter_gemini()  [30s hard timeout via ThreadPoolExecutor]
│       Tiers URLs: High / Medium / Low
│       Fallback: all URLs → High (pipeline never abandons batch)
│
├── 8. Velocity gate: if recent_lead_count >= threshold → drop Medium
│
├── 9. For each approved URL:
│       a. Global Exclusivity Lock (Firestore transaction, 14-day window)
│       b. Deterministic dedup: leads.create() → AlreadyExists = skip
│       c. PrismPipeline.process_url()
│            WalledGarden mode   → 3-way Serper triangulation (social domains)
│            B2B2C mode          → intermediary discovery
│            GeneralDomain mode  → httpx.get + BeautifulSoup + WAF detect
│       d. final_score_and_dm()  [Gemini 2.5 Flash — hyper-personalized DM]
│       e. Write lead doc to Firestore leads collection
│       f. _settle_credit()  → Cloud Tasks → /api/internal/credits/settle
│
└── 10. Zombie lead recovery (status=processing AND createdAt < 15min ago)
```

---

## 5. Orchestrator Cron Sweep (`POST /api/internal/cron/sweep`)

The heartbeat of the entire system. Runs every 5 minutes via Cloud Scheduler.

### 5.1 Pre-Loop Gates (apply globally, abort entire sweep)

| Gate | Exit code | Log event |
|---|---|---|
| OIDC verification fails | 401/403 | — |
| Circuit breaker open (Serper error rate > 15% OR scraper OOM > 5%) | 503 | `circuit_breaker_open` |

### 5.2 Per-Campaign Loop Gates (each gate = one `continue`, next campaign)

| Gate | Log event | Fields logged |
|---|---|---|
| `campaign.tenant_id` missing | `BYPASS_NO_TENANT_ID` | campaign_id |
| `available_credits <= 0` | `BYPASS_QUOTA_EXHAUSTED` | credits, consumed, reserved, available |
| `next_produce_due > now_utc` (24h interval) | `BYPASS_PRODUCE_NOT_YET_DUE` | hours_remaining |
| *(consumer only)* `queue_depth == 0` | `BYPASS_DRIP_QUEUE_EMPTY` | queue_depth |
| `next_drip_due > now_utc` (4h interval) | `BYPASS_DRIP_NOT_YET_DUE` | hours_remaining |

### 5.3 Timestamp Handling

`next_produce_due` and `next_drip_due` are stored as **ISO-8601 strings** by the sweep's `finally:` blocks. The sweep parses them with `fromisoformat()`. Legacy documents with Firestore `DatetimeWithNanoseconds` objects are also handled via `hasattr(.timestamp)`.

### 5.4 Clock Advancement Pattern (Finally Block)

```python
try:
    tasks_client.create_task(...)   # may fail: IAM, quota, network
except Exception:
    log.error(...)
finally:
    camp_doc.reference.update({
        "next_produce_due": (now_utc + timedelta(hours=24)).isoformat()
    })  # ALWAYS fires — clock never freezes regardless of dispatch failure
```

### 5.5 OIDC Task Authentication

```python
if ORCHESTRATOR_SA_EMAIL:            # from env var (zero I/O)
    sa_email = ORCHESTRATOR_SA_EMAIL
else:
    sa_email = metadata_fetch(timeout=1s)  # 1s hard cap fallback

task["http_request"]["oidc_token"] = {
    "service_account_email": sa_email,
    "audience": base_url,  # pipeline-main Cloud Run URL (without /dispatch)
}
```

---

## 6. Firestore Data Model

### Collections

| Collection | Document ID | Key Fields |
|---|---|---|
| `users` | `tenant_id` | `role`, `wallet.{allocated_credits, total_consumed, reserved_credits}`, `approval_status` |
| `users/{id}/wallet_shards` | `0`–`9` | `consumed_credits` (legacy shard counter) |
| `campaigns` | auto-ID | `tenant_id`, `status`, `persona_id`, `persona_bio`, `persona_keywords`, `unprocessed_queue[]`, `next_produce_due`, `next_drip_due`, `sourcing_vector`, `location`, `gl` |
| `leads` | SHA256(tenant+domain) | `tenant_id`, `url`, `status`, `confidence_tier`, `matched_campaigns[]`, `ai_profile`, `dm_draft`, `expire_at`, `credit_settled` |
| `personas` | auto-ID | `tenant_id`, `name`, `bio`, `keywords`, `target_personas[]` |
| `scraped_cache` | URL-keyed | `url`, `text`, `source`, `tech_stack[]`, `emails[]`, `phones[]` |
| `global_lead_locks` | domain hash | `locked_until` (14-day exclusivity window) |
| `system_telemetry` | `circuit_breaker_state` | `serper_calls_window`, `serper_429s_window`, `scraper_calls_window`, `scraper_ooms_window`, `window_reset_at` |
| `system_config` | `router` | Epsilon-greedy router weights |
| `usage_metrics` | `tenant_id` | `serper_searches` (Increment) |
| `tenant_profiles` | `tenant_id` | `target_personas[]` |
| `ontology_map` | domain path | Vector weights, visit counts |

### Credit Settlement Flow

```
lead completes (dispatch) 
    → _settle_credit(outcome="success")
        → Cloud Tasks → POST /api/internal/credits/settle
            → _atomic_settle_txn() [Firestore transaction]
                → checks lead.credit_settled (idempotency guard)
                → wallet.total_consumed += count
                → wallet.reserved_credits -= count
                → lead.credit_settled = True
```

---

## 7. Additional Microservices

| Service | Role |
|---|---|
| **digital-twin-engine** | Generates child campaigns from ontology signals. Runs Gemini to expand a parent campaign into focused sub-campaigns, each with its own keywords and sourcing vector. |
| **autonomous-engine** | Scans job postings and funding news (ingestors.py) to find intent signals. Feeds the predictive cache with high-intent domains. |
| **scraper-heavy** | Playwright-based headless browser scraper for WAF-protected sites. Called by PrismPipeline when httpx fails WAF detection. |
| **shadow-learner-aggregator** | Aggregates shadow tracking telemetry from BigQuery. Identifies campaigns that are seeing thin payloads (SHADOW_LEARNER_THIN_PAYLOAD marker) and adjusts scoring thresholds. |
| **email-summary** | Sends weekly digest emails to tenants via SendGrid summarizing lead activity, credits consumed, and top leads. |
| **whatsapp-webhook** | Receives Meta WhatsApp webhook events, matches them to existing leads, updates lead status and DM thread history. |

---

## 8. BigQuery Dataset (`sideio_telemetry`)

| Table | Purpose |
|---|---|
| `neg_signals` | Domains/URLs downvoted by tenant — fed back into query suppression |
| `shadow_track` | All produce decisions (accept/reject per URL) for ML training |
| `ontology_decay` | Weekly decay of ontology weights — reduces stale signal dominance |
| `rag_embeddings` | (reserved) Vector store for future RAG retrieval |

BQ writes are fire-and-forget via `_async_neg_signal_insert()` and `_async_shadow_track()` (daemon threads). Bulk inserts go via Cloud Tasks → `/api/internal/telemetry/bq-push`.

---

## 9. Cloud Scheduler Jobs (V23 Standard)

| Job | Target | Schedule | Auth |
|---|---|---|---|
| `pipeline-sweep` | `POST /api/internal/cron/sweep` | every 5 minutes | OIDC (orchestrator SA) |
| `ai-reflection` | `POST /api/internal/cron/reflection` | weekly | OIDC |
| `ontology-decay` | `POST /api/internal/cron/ontology-decay` | weekly | OIDC |

Legacy jobs (`autonomous-engine-trigger`, `master-cron-sweep`, `lead-sniper-orchestrator`) have been permanently removed from `cloudbuild.yaml`.

---

## 10. Security & Auth

| Layer | Mechanism |
|---|---|
| Public API → Orchestrator | Firebase Auth JWT (`Authorization: Bearer <firebase_token>`) |
| Cloud Scheduler → Orchestrator cron | Google OIDC token (`Authorization: Bearer <oidc_token>`), verified by `_verify_oidc()` |
| Orchestrator → Pipeline-Main (Cloud Tasks) | OIDC token signed by `lead-pipeline-sa`, audience = pipeline-main base URL. Cloud Run IAM: `lead-pipeline-sa` has `roles/run.invoker` |
| Pipeline-Main route guard | `X-CloudTasks-QueueName` header (injected by GCP Cloud Tasks, unforgeable from public internet) |
| Secrets | All API keys stored in GCP Secret Manager. Fetched at runtime via `get_secret(SERPER_API_KEY_NAME)`. Secret name: `SERPER_API_KEY` (uppercase enforced). |
| Encryption | Fernet symmetric encryption for tenant BYOT (Meta API tokens stored in Firestore) |

### IAM Service Account

`lead-pipeline-sa@<project>.iam.gserviceaccount.com`

Required roles:
- `roles/run.invoker` on `lead-pipeline-main` (Cloud Run)
- `roles/cloudtasks.enqueuer` on `lead-pipeline-queue`
- `roles/secretmanager.secretAccessor`
- `roles/bigquery.dataEditor` on `sideio_telemetry`

---

## 11. CI/CD Pipeline (`cloudbuild.yaml`)

```
Trigger: push to main
│
├── Step 1: syntax-check-appjs        — node:20-slim
├── Step 2: python-v23-smoke-gate     — python:3.11-slim (local_smoke_tests.py)
├── Step 3: build-pipeline-main       — docker build services/pipeline-main
├── Step 4: push-pipeline-main        — gcr.io/${PROJECT_ID}/lead-pipeline-main:$SHA
├── Step 5: build-orchestrator        — docker build services/orchestrator
├── Step 6: push-orchestrator         — gcr.io/${PROJECT_ID}/lead-orchestrator:$SHA
├── Step 7: deploy-pipeline-main      → Cloud Run lead-pipeline-main
│           Env vars: PROJECT_ID, QUEUE, LOCATION, SERPER_API_KEY_NAME (via SM)
├── Step 8: deploy-orchestrator       → Cloud Run orchestrator
│           Env vars: PROJECT_ID, QUEUE, LOCATION, PIPELINE_URL,
│                     ORCHESTRATOR_URL, ORCHESTRATOR_SA_EMAIL
├── Step 9: deploy-orchestrator-v23-preview  → --no-traffic --tag=v23-preview
│           (UAT revision, zero live traffic until manual promotion)
└── Step 10+: deploy remaining microservices (DT engine, scraper, etc.)
```

**Smoke gate** (`local_smoke_tests.py`) asserts:
- `SERPER_API_KEY_NAME` contains uppercase `SERPER_API_KEY` (not lowercase)
- Route blueprints are importable
- Config constants resolve correctly

**Manual one-time grant** (not in pipeline):
```bash
gcloud run services add-iam-policy-binding lead-pipeline-main \
  --region=asia-south1 \
  --member="serviceAccount:lead-pipeline-sa@<project>.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

---

## 12. PrismPipeline (URL Routing Engine)

Lives in `services/pipeline-main/services/prism/`. Routes each URL to the correct scraping hook:

| Mode | Trigger | Strategy |
|---|---|---|
| `WalledGarden` | Social domain (Reddit, LinkedIn, Facebook, etc.) | 3-way parallel Serper triangulation (site:, name:, persona-hint queries). No direct scraping. Results cached to `scraped_cache`. |
| `B2B2C` | B2C sourcing vector + non-social URL | Intermediary discovery — finds business entities serving the consumer segment |
| `GeneralDomain` | All other B2B URLs | httpx.get (WAF detection) → BeautifulSoup extraction → Tech-Stack X-Ray. WAF detected → delegates to WalledGarden fallback. |

---

## 13. Known Operational Constraints

| Constraint | Detail |
|---|---|
| Gunicorn pre-fork | All gRPC clients (Firestore, SecretManager, Vertex AI) must be initialized lazily post-fork. Global `firestore.Client()` at module scope deadlocks child workers. |
| ISO-8601 timestamps | Firestore `update()` with Python `datetime` objects can fail silently on some SDK versions. All timestamps are written as `.isoformat()` strings. |
| Serper secret name | Must be exactly `SERPER_API_KEY` (uppercase). Smoke test enforces this. |
| Cloud Tasks OIDC | `ORCHESTRATOR_SA_EMAIL` env var must be set on orchestrator Cloud Run service. Without it, the 1s metadata fallback may fail on cold starts → tasks dispatched without OIDC → 403 from pipeline-main IAM. |
| Credit field schema | `wallet.total_consumed` (written by `_atomic_settle_txn`) and `wallet.consumed_credits` (legacy) may both exist. Sweep reads `max()` of both. |
