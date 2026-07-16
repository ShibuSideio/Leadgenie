# LeadGenie (Sideio) — Platform Architecture V26.7.0
**Technical Specification Document**
*Last Updated: 2026-07-16 | Version: V26.7.0 — Domain-Aware LLM Gates + Multi-Entity Hosts + Campaign Velocity Quotas*

---

## 1. SYSTEM OVERVIEW

LeadGenie is a fully automated, multi-tenant OSINT-powered lead generation SaaS platform. It discovers, scores, and delivers hyper-personalised outreach messages for paying tenants — autonomously, 24/7, without manual input. V26.1 introduced a **shared deterministic intelligence layer** that infers an execution strategy from sparse campaign input, routes discovery with budget awareness, and promotes leads through an explainable confidence gate rather than a single brittle score threshold.

**Core loop:**
1. Cloud Scheduler cron hits the Orchestrator every 5 minutes
2. Orchestrator validates quota, checks drip cadence, enqueues a Cloud Task per active campaign
3. Pipeline-Main runs: **domain-profile resolve → intelligence-profile inference → domain-aware query generation → budget-aware source routing → scrape → domain-aware pre-filter → score → adaptive confidence qualification → entity extraction → write to Firestore**
4. The PWA frontend listens via `onSnapshot` and renders leads in real-time

### Latest implementation updates (V26.7.0)
- A shared heuristic planner now infers a campaign intelligence profile from sparse user input so the backend can make stronger decisions with minimal manual effort.
- Source routing uses that inferred strategy plan and a daily budget guard to avoid wasting expensive Serper spend on weak or low-evidence campaigns.
- Query generation now uses deterministic fallback logic and strategy-specific phrasing so the pipeline remains robust even when Gemini is unavailable.
- Lead promotion is gated by deterministic confidence scoring **plus** a hybrid Gemini score floor, after adapting `final_score_and_dm` output into the confidence schema (V26.5.1).
- **Domain-aware LLM gates (V26.6.0):** `pre_filter_gemini` and `final_score_and_dm` receive structured campaign runtime context (`domain_family`, `profile_confidence`, `liquidity_level`, `sourcing_vector`, `primary_strategy`, enriched ICP). Scoring rules **branch** for `PLATFORM_MINING`, `COMPETITOR_TOUCHPOINT`, and consumer vectors (no single generic B2B brochure rule for all campaigns).
- **Multi-entity host identity (V26.7.0):** known portal/aggregator hosts (Bayut, PropertyFinder, Dubizzle, G2, etc.) force **path-level** locking, lead dedup, and scraped-cache keys **even for B2B** campaigns — see `services/shared/multi_entity_hosts.py`.
- **Velocity gate isolation (V26.7.0):** tenant-wide Medium hard cap remains; each campaign also has a **soft Medium intake quota** (default 12 / 24h, configurable).
- Campaign create/update runs deterministic auto-enrichment (`system_enrichment`); self-healing enrichment backfill repairs sparse legacy campaigns.
- Query governance, campaign-scoped novelty memory, and exhaustion escalation protect Serper spend.
- Dispatch uses **`adaptive-v3`** (queue health + domain `strictness_bias` × `profile_confidence` damping).
- Non-promotions persist as `scored_out` with confidence + domain impact + promotion-path diagnostics.
- **Domain Intelligence system (SSOT):** `system_domain_profile` + optional `domain_override`; produce/dispatch emit domain impact summaries.
- **Inbound Radar** remains domain-aware with `enrichment_priority` contracts (`realtime` / `batch` / `deferred`).

**Intelligence Strategies (V26.0):**
- `PLATFORM_MINING` — Extract leads from competitor directories, aggregator platforms, review sites
- `COLLOQUIAL_DISCOVERY` — Search in the buyer's own language (e.g., "my AC keeps leaking" instead of "HVAC maintenance")
- `COMPETITOR_TOUCHPOINT` — Mine competitor reviews for dissatisfied customers
- `PROFESSIONAL_NETWORK` — Target professional networks, job boards, conference speakers
- `EVENT_TRIGGER` — Monitor hiring signals, funding events, technology adoptions

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
│   ├── /shared                      # Shared cross-service heuristics (orchestrator + pipeline)
│   │   ├── intelligence_profile.py  # Deterministic strategy-profile inference + execution plan
│   │   ├── domain_constants.py      # SSOT: KNOWN_DOMAIN_FAMILIES, is_valid_domain_family()
│   │   ├── domain_gate.py           # Shared thresholds + enrichment_priority contracts
│   │   ├── multi_entity_hosts.py    # V26.7: portal/aggregator path-level identity SSOT
│   │   └── campaign_enrichment.py   # Deterministic campaign field auto-enrichment
│   ├── /orchestrator                # Cloud Run: REST API Gateway + Cron Dispatcher
│   │   ├── api/routers/             # Modular Flask blueprints (campaigns, leads, visitor_signals…)
│   │   ├── jobs/                    # inbound_sentiment_job.py (domain-aware radar cron)
│   │   ├── services/                # inbound_sentiment_service.py, inbound_maps_service.py
│   │   ├── services/intelligence/   # shadow_tracker.py, neg_signal.py
│   │   ├── core/config.py           # Shared env-var config
│   │   └── requirements.txt
│   ├── /pipeline-main               # Cloud Run: AI Extraction Engine (Cartographer)
│   │   ├── api/routers/             # dispatch.py, produce.py
│   │   ├── core/constants.py        # CONSUMER_ARCHETYPES, D2C_ARCHETYPES, B2B2C_ARCHETYPES
│   │   ├── services/                # Core intelligence services:
│   │   │   ├── domain_intelligence.py # Domain profile inference, override, query shaping
│   │   │   ├── adaptive_policy.py   # adaptive-v3 dispatch gate policy
│   │   │   ├── query_brain.py       # AI query generation + domain-seeded platform mining
│   │   │   ├── query_governance.py  # Query portfolio governance (pre-Serper)
│   │   │   ├── source_router.py     #   V26: Multi-source OSINT router (10 signal source plugins)
│   │   │   ├── signal_sources/      #   V26: Pluggable signal source modules:
│   │   │   │   ├── base.py          #     Abstract base class for all signal sources
│   │   │   │   ├── serper_discovery.py  # Google Search via Serper API
│   │   │   │   ├── reddit.py        #     Reddit thread monitoring
│   │   │   │   ├── hackernews.py    #     HackerNews signal extraction
│   │   │   │   ├── google_reviews.py #    Google Reviews competitor mining
│   │   │   │   ├── consumer_forum.py #    Consumer forum monitoring
│   │   │   │   ├── classified_listings.py # Classified ad monitoring
│   │   │   │   ├── job_posts.py     #     Job board signal extraction
│   │   │   │   ├── rss_feed.py      #     RSS/Atom feed monitoring
│   │   │   │   └── youtube.py       #     YouTube video/comment extraction
│   │   │   ├── intelligence_mesh.py #   V26: Cross-source dedup + merge
│   │   │   ├── signal_harvest.py    #     Signal harvesting engine
│   │   │   ├── signal_cluster_analyst.py # Signal clustering and analysis
│   │   │   ├── budget_guard.py      #     Daily cost guard for costly discovery actions
│   │   │   ├── lead_confidence.py   #     Confidence scoring + Gemini eval adapter + hybrid promotion
│   │   │   ├── serper_service.py    #     Serper API client
│   │   │   ├── neg_shield.py        #     Negative signal shield (BQ)
│   │   │   ├── prism_pipeline.py    #     Headless browser scraping
│   │   │   ├── gemini_service.py    #     Gemini AI: domain/strategy-aware pre-filter + final_score_and_dm
│   │   │   ├── context_builder.py   #     Enriched ICP context builder
│   │   │   └── telemetry.py         #     Pipeline telemetry
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
| Orchestrator | `orchestrator` | `--no-allow-unauthenticated` (V25.6.0) | 512 Mi | asia-south1 |
| Pipeline Main | `lead-pipeline-main` | `--no-allow-unauthenticated` | 512 Mi | asia-south1 |
| Scraper Heavy | `scraper-heavy` | `--no-allow-unauthenticated` | 2 Gi | asia-south1 |
| Digital Twin Engine | `digital-twin-engine` | `--no-allow-unauthenticated` (V25.6.0) | 512 Mi | asia-south1 |
| Shadow Learner Aggregator | `shadow-learner-aggregator` | `--no-allow-unauthenticated` | 256 Mi | asia-south1 |
| WhatsApp Webhook | `whatsapp-webhook` | DISABLED (build/deploy removed V25.6.0) | 128 Mi | asia-south1 |
| Email Summary | `email-summary` | `--no-allow-unauthenticated` | 128 Mi | asia-south1 |
| **Autonomous Engine** | **`autonomous-engine`** | **Cloud Run Job (no HTTP)** | **512 Mi** | **asia-south1** |
| Frontend | Firebase Hosting | Public CDN | — | Global |

**GCP Project ID:** `sideio-leads-v16`
**Firebase Project:** `lead-sniper-prod`
**Cloud Tasks Queue:** `lead-pipeline-queue` (region: asia-south1)
**Vertex AI / Gemini:** `gemini-2.5-flash` via `google-genai` SDK (2-tier fallback: primary model → `gemini-2.0-flash`)

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
SERPER_DAILY_LIMIT=0                # 0 disables the budget guard
SERPER_BUDGET_STATE_PATH=/tmp/serper_budget.json
DEDUP_RECRAWL_DAYS=30              # Re-crawl horizon for lead dedup memory in producer
MEDIUM_CAMPAIGN_QUOTA_24H=12       # Per-campaign Medium soft quota (0 = disable soft quota)
MEDIUM_CAMPAIGN_QUOTA_ENABLED=true # Master switch for campaign Medium soft quota
MULTI_ENTITY_HOST_SUFFIXES=        # Optional comma-separated extra portal host suffixes
SNIPPET_CACHE_TTL_HOURS=72         # Max age for snippet-cache fallback in dispatch

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
    "total_consumed": 320,
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
- `wallet.total_consumed` (V25.6.0): authoritative consumed counter, written atomically by `_atomic_settle_txn`. True balance = `allocated_credits − max(total_consumed, consumed_credits + SUM(wallet_shards/0-9)) − reserved_credits`
- `wallet.consumed_credits`: legacy base counter. Kept for backward compatibility. The `max()` formula ensures neither path underreports consumption

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
  "effective_bio": "AI-enriched product description (richer than bio when populated)",
  "campaign_focus": "Commercial cleaning for mid-market offices",
  "status": "active",
  "keywords": "facility management, office cleaning",
  "location": "Austin, TX",
  "gl": "us",
  "sourcing_vector": "B2B",
  "intelligence_strategy": {
    "primary": "COLLOQUIAL_DISCOVERY",
    "vocabulary_notes": "Buyers say 'our office is dirty' not 'facility management services'",
    "mining_targets": ["yelp.com", "google.com/maps"],
    "confidence": 0.85
  },
  "persona_id": "<firestore_persona_doc_id>",
  "persona_bio": "Denormalised bio from linked Persona Vault entry.",
  "persona_keywords": "keyword1, keyword2",
  "persona_name": "Enterprise SaaS Decision Makers",
  "persona_targeting_signals": ["looking for outsourced facility services", "NOT freelancer"],
  "pain_point": "Buyer language observed from approved leads (accumulates over time)",
  "target_angle_hook": "Message that resonates with buyer — informs query generation",
  "target_angle_adv": "Advantage angle for outreach",
  "unfair_advantage": "Seller differentiator — used by context_builder for ICP framing",
  "system_domain_profile": {
    "version": "domain-v2",
    "domain_family": "real_estate",
    "confidence": 0.92,
    "profile_confidence": "high",
    "thin_campaign": false,
    "input_richness": "high",
    "strictness_bias": -0.3,
    "soft_domain_adjustments": false,
    "liquidity_level": "low",
    "low_liquidity_market": true,
    "preferred_sources": ["classified_listings", "serper_discovery", "consumer_forum"],
    "preferred_query_hints": ["site:propertyfinder", "site:bayut"],
    "blocked_subreddits": ["frugal", "buyitforlife"],
    "override_active": false,
    "notes": "fields_used=...; profile_confidence=high"
  },
  "domain_override": null,
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
- `intelligence_strategy` (V26.0): AI-classified strategy object set at campaign creation. Sub-fields:
  - `primary`: one of `PLATFORM_MINING`, `COLLOQUIAL_DISCOVERY`, `COMPETITOR_TOUCHPOINT`, `PROFESSIONAL_NETWORK`, `EVENT_TRIGGER`
  - `vocabulary_notes`: how the ICP speaks — fed to Gemini for colloquial query translation
  - `mining_targets`: auto-derived platform URLs for PLATFORM_MINING/COMPETITOR_TOUCHPOINT
  - `confidence`: classification confidence (0.0–1.0)
- `system_domain_profile` (V26.4–V26.5): resolved domain intelligence snapshot (see §21). Written by produce/dispatch via `resolve_campaign_domain_profile()`.
- `domain_override` (V26.5): optional manual override (`string` family or partial object). Validated on campaign create/update; takes precedence over auto-inference. `null` / `{}` clears override and forces re-infer.
- `unprocessed_queue`: array of Serper result objects awaiting Gemini profiling; capped at 200 (backpressure at depth 150)
- `next_drip_due`: updated on every produce run (V24.4 fix — was only set on first fill)
- `keywords`: stored as comma-separated string, parsed to array in pipeline
- `V26.1.0`: the backend now infers a lightweight intelligence profile from sparse campaign input and uses it to drive routing and query decisions even when the user provides minimal details
- `effective_bio`: AI-generated enriched product description. Priority Layer 1 in `context_builder.py`
- `pain_point`: accumulates real buyer language from approved leads over time. Fed back into query generation by `context_builder.py` Layer 3 — the system compounds in intelligence with each approval
- `target_angle_hook`, `unfair_advantage`, `persona_targeting_signals`: ALL consumed by `context_builder.py` (V24.6.1). Previously unused in pipeline.

> [!IMPORTANT]
> **V24.6.1 context pipeline:** `context_builder.build_enriched_context(campaign)` is the single source of truth for ICP context. It aggregates all 15+ campaign fields (including `effective_bio`, `pain_point`, `target_angle_hook`, `unfair_advantage`) into a structured context string used by both `produce.py` (query generation) and `dispatch.py` (pre-filter). Any new campaign field that should influence query generation must be added to `context_builder.py`, not to `dispatch.py` or `produce.py` individually.

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
  "contact_endpoints": [{"type": "email", "value": "hr@techcorp.com"}, {"type": "phone", "value": "+13125550199"}],
  "matched_campaigns": ["camp_uuid_789"],
  "crm_delivery_status": "delivered",
  "interactions": [
    { "action": "status_ignored", "date": "<SERVER_TIMESTAMP>" }
  ],
  "createdAt": "<SERVER_TIMESTAMP>",
  "updatedAt": "<SERVER_TIMESTAMP>"
}
```

**Status Enum:** `processing` → `new` → `reviewed` → `contacted` → `converted` | `ignored` | `failed` | `enrichment_pending` | `scored_out` | `rlhf_filtered`

> [!NOTE]
> The GET /api/leads feed (V24.5.6) filters exclusively on `status == "new"`. Zombie stubs
> (`processing`, `failed`, `enrichment_pending`) are NOT shown in the UI feed.

**Key fields (V24.2+):**
- `normalized_score` (0–100): unified scale across engines. Outbound = `score × 10`. Inbound = `intent_score × 100`
- `origin_engine`: `"cartographer"` (Serper-driven) or `"autonomous"` (nightly engine) or `"inbound"` (Inbound Radar)
- `crm_delivery_status`: `"delivered"` | `"pending_retry"` | `"failed_permanent"` (V24.4 CRM retry)
- `enrichment_pending`: set when Medium-tier URL has < 300 chars of text — awaiting full scrape (V24.4)

**Score gate (V26.3.0 → V26.5.1):** Promotion uses adaptive **confidence** thresholds with a **hybrid Gemini score floor** after adapting `final_score_and_dm` into the confidence schema. Non-promotions persist as `scored_out` with `confidence_score`, `score_drop_reason`, `promotion_path`, `scoring_context`, and compact `domain_impact_summary`.

**Deduplication key (V26.7.0 identity rules):**
- **Social + shared-platform URLs** (`linkedin.com`, `reddit.com`, `quora.com`, etc.): `sha256(tenant_id + '_' + netloc + path)` — each thread/post is a unique lead.
- **Multi-entity portal hosts** (Bayut, PropertyFinder, Dubizzle, G2, Capterra, OLX, Zillow, … — SSOT in `shared/multi_entity_hosts.py`): **always path-level** for lock, lead id, and scraped cache — **including B2B**.
- **Consumer archetypes (B2C/D2C/B2B2C)**: always URL-path dedup regardless of domain.
- **B2B normal company domains**: `sha256(tenant_id + '_' + root_domain)`.
- **Recrawl TTL (V26.2.0)**: only leads newer than `DEDUP_RECRAWL_DAYS` (default 30).
- Terminal non-promoted statuses (`scored_out`, `rlhf_filtered`, `failed*`) are excluded from produce dedup.

### 4.6 `global_lead_locks` Collection
Cross-tenant exclusivity lock (currently **3 days**, with `expire_at` for TTL cleanup).

```json
{
  "_id": "sha256(exact_path) | root_domain",
  "locked_until": "<TIMESTAMP +3 days>",
  "expire_at": "<TIMESTAMP +3 days>"
}
```

- Path-hash lock id for social / shared / consumer / **multi-entity portals**.
- Root-domain string lock id only for normal B2B company domains.
- Logs: `dispatch_multi_entity_path_identity` when portal path rules apply.

### 4.7 `scraped_cache` Collection
Caches Serper snippets and scrape text. Document id is `sha256(tenant_id + '_' + identity_key)` using the same path/domain rules as lead dedup (including multi-entity path keys).

```json
{
  "_id": "sha256(tenant_id + '_' + identity_key)",
  "url": "https://www.bayut.com/brokers/agent-123.html",
  "text": "Query: ...\nTitle: ...\nSnippet: ...",
  "source": "serper_snippet",
  "tech_stack": [],
  "emails": [],
  "phones": [],
  "cached_at": "<SERVER_TIMESTAMP>"
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
| `rejection_reason` | STRING | Rejection reason: `competitor`, `wrong_industry`, `not_icp`, `low_quality` |
| `sourcing_vector` | STRING | Vector scope (`"B2B"`, `"B2C"`, etc., or `"GLOBAL"`) — V24.3 vector isolation |
| `timestamp` | TIMESTAMP | When the signal was recorded |

> [!NOTE]
> `sourcing_vector` column was added 2026-07-02 via `bq update` to fix `neg_shield_fetch_failed`. The query in `neg_shield.py` references this column. Existing rows have `NULL` for this field and correctly match `OR sourcing_vector IS NULL` in the query.

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
- Cloud Tasks queue header (`X-CloudTasks-QueueName`) is accepted as a supplementary signal after OIDC passes (V25.6.0 — was previously a bypass that allowed spoofing)

### 5.6 Gemini Model Fallback (V25.6.0)
All Gemini calls now use a 2-tier model chain. If the primary model (`GEMINI_MODEL` env var) returns `NotFound` or `ResourceExhausted`, the call retries once with `gemini-2.0-flash` as fallback. This prevents total pipeline stall on model deprecation or quota exhaustion.

### 5.7 Agents Router Auth (V25.6.0)
The `agents.py` router was the only orchestrator router that bypassed `@require_auth` middleware, using manual `_get_uid()` extraction with no `is_active`, `approval_status`, or `role` checks. V25.6.0 added `@require_auth` to all 5 agent routes and replaced per-request `fs.Client()` with the singleton `get_db()` to prevent gRPC connection leaks.

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
| B2B | Standard — enterprise buyer signals, filetype dorks | `tbs=qdr:y` (past year — V24.6.0) | B2B-scoped |
| B2C | Consumer mandate — forum/review dorks, dialog-cue dorks (`"pm me"`, `"still available"`) | `tbs=qdr:m` (past month) | B2C-scoped |
| D2C | D2C product comparison mandate — `site:reddit.com`, `site:trustpilot.com`, `inurl:compare` | `tbs=qdr:m` | D2C-scoped |
| B2B2C | Dual-ICP mandate — 50% institutional + 50% end-user signals | `tbs=qdr:m` | B2B2C-scoped |

**B2C keyword fallback (V24.3):** When `ctx.intents` is empty and only bio keywords are available, consumer campaigns use intent template queries (`"looking for"`, `"anyone selling"`, `"pm me"`) instead of quoting raw bio words. Raw bio words produce SEO directory results, not buyer signals.

#### 5f. Universal Enriched Context Builder (V24.6.1)
**Location:** `pipeline-main/services/context_builder.py`

`build_enriched_context(campaign)` is the single source of truth for ICP context assembly, called by both `produce.py` and `dispatch.py`.

**Problem it solves:** Not all users are elaborate. Before V24.6.1, a campaign created with only a name and location sent 5 words to Gemini for query generation (`"Product/Service: Brand Narrative Development"`). All other fields — `effective_bio`, `pain_point`, `target_angle_hook`, `unfair_advantage`, `persona_targeting_signals`, `geo_hierarchy` — were silently ignored.

**Context assembly layers (in priority order):**

| Layer | Fields Used | Notes |
|---|---|---|
| 1 — Product/Service | `effective_bio` > `persona_bio` > `bio` > `campaign_focus` | Richest non-junk wins |
| 2 — Market Context | `keywords`, `persona_keywords` | Comma-separated, used as-is |
| 3 — Buyer Pain | `pain_point` | Accumulates real buyer language from approved leads |
| 4 — ICP Identity | `persona_name`, `persona_bio` | Skipped if already used in Layer 1 |
| 5 — Messaging | `target_angle_hook`, `unfair_advantage` | Tells Gemini what buyer language resonates |
| 6 — Targeting | `persona_targeting_signals` (positive only) | Negative signals (`NOT ...`) excluded here |
| 7 — Geography | `location`, `geo_hierarchy.country`, `geo_hierarchy.region` | Skipped if "All" or "Global" |
| 8 — Buyer Type | `sourcing_vector` | B2B / B2C / D2C / B2B2C |

**Graceful degradation:**
- Power user (all fields filled): 8 labeled sections, ~800 chars of context
- Average user (bio + persona linked): 4 sections, ~300 chars
- Lazy user (name + location only): 2 sections — name + geo
- Fallback: always returns at least campaign name — never empty

**Observability:** Logs `context_builder_assembled` with `sections` count and boolean flags per layer. Operators can diagnose thin-context campaigns by filtering for `sections < 3`.

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

Post-flight noise filter (`filter_serper_noise`) removes:
1. **Enterprise/aggregator domains** (`ibm.com`, `amazon.com`, `g2.com`, `capterra.com`, `zoominfo.com`)
   - V26.0.4.1: `linkedin.com` **UNBLOCKED** — snippets contain enough B2B context for Gemini scoring (same reasoning as Quora un-block in V25.2.3)
2. **Noise URL paths** (`/legal`, `/pricing`, `/docs`, `/login`, `/author/`)
3. **Bot/auth page snippets** (`"sign in"`, `"access denied"`, `"forgot password"`)
4. **CDN/asset subdomains** (V24.5.7): `assets.*`, `cdn.*`, `static.*`, `img.*`, `images.*`, `media.*`, `s3.*`, `storage.*`, `files.*`, `dl.*`, `download.*`, `content.*`
   - V26.0.4.1: `research.*` **REMOVED** — too broad, catches legitimate company pages (research.google.com)
5. **Content farm domains** (V25.7.4): 38 news/listicle domains
   - V26.0.4.1: **B2B news exception** — `bloomberg.com`, `businessinsider.com`, `reuters.com`, `cnbc.com`, `livemint.com`, `washingtonpost.com`, `nytimes.com` pass through for event-trigger leads
6. **Reddit news subreddits** (V25.7.4): 36 non-business subreddits blocked
7. **Megathread patterns** (V25.7.4): 15 aggregation title patterns

**Queue backpressure (V24.4):** If `unprocessed_queue` depth > 150, produce skips Serper fetch entirely (`200 skipped_queue_full`). Prevents `[:200]` trimming from discarding fresh signals when the consumer hasn't caught up.

**B2B Forum Dedup (V24.5.5):** Reddit, Quora, StackExchange, HN and other buyer forum platforms are in `shared_platforms` — each thread/post gets URL-path dedup, not domain-level collapse. Without this, all Reddit URLs for a B2B campaign would collapse to one slot.

### Step 7: Gemini Pre-Filter Gate (V26.6.0 domain/strategy-aware)
Deduplicated Serper snippets pass through a Gemini `gemini-2.5-flash` tiering gate before heavy scrape (forum/classified domains may **bypass** to High — see dispatch `_PREFILTER_BYPASS_DOMAINS`).

**Prompt context (always when known):**
- `domain_family`, `profile_confidence`, `liquidity_level`, `strictness_bias`, `thin_campaign`
- `sourcing_vector`, `primary_strategy` (from `intelligence_strategy.primary`)
- Enriched USER BIO from `context_builder` (dispatch)

**Strategy / domain behaviour:**
- **PLATFORM_MINING / COMPETITOR_TOUCHPOINT:** directories, classifieds, review aggregators, and listing/profile pages default to **Medium/High** when ICP+geo match (not auto-Low solely for being a directory). Strategy forces directory softening even on thin profiles.
- **Consumer vectors (B2C/D2C/B2B2C):** STEP 4 dialog-cue / query-context inference is **gated on** (not applied to pure B2B).
- **Domain-family calibration examples:** real estate, marketing/SaaS, manufacturing, healthcare, or general High/Low anchors (replaces marketing-only few-shots).
- Core Low categories remain: SEO listicles, wrong geography, pure competitors selling the same service as USER BIO.
- Deterministic **directory rescue** may promote domain-valuable portal URLs Low→Medium when softening is active.
- Failure/timeout: bounded High-tier degraded pass-through (not unlimited fail-open).

Logs: `pre_filter_context_applied`, `pre_filter_domain_adjustment_applied`, `pre_filter_domain_directory_rescue`, `pre_filter_complete`.

**B2B FAQ Sanitizer (V24.5.7):** After Gemini generates `translated_queries`, a post-generation filter drops any query starting with FAQ openers (`"how do you"`, `"what are good"`, `"what is the best"`, `"tips for"`, etc.) that match SEO agency blogs rather than buyer forums. If all queries are FAQ, one is kept as a last resort.

### Step 7b: Velocity Gate (Medium intake) — V26.7.0
After pre-filter, **High** URLs always proceed. **Medium** URLs are gated:

| Control | Scope | Default | Role |
|---------|--------|---------|------|
| Tenant hard cap | All campaigns for tenant | `VELOCITY_THRESHOLD` (env, often 10) on 24h `new` + `enrichment_pending` | Blocks Medium when tenant is saturated |
| Policy `medium_budget` | Per dispatch cycle | From `adaptive-v3` (e.g. 2–8) | Caps how many Medium URLs enter this batch |
| **Campaign soft quota** | Per campaign / 24h | **`MEDIUM_CAMPAIGN_QUOTA_24H=12`** | Prevents one aggressive campaign from consuming all Medium slots |
| Campaign override | Firestore | `campaign.medium_intake_quota_24h` | Optional per-campaign soft quota |

Effective Medium take = `min(policy_budget, campaign_remaining)` when tenant allows Medium; High never blocked by Medium quotas.

Logs: `velocity_gate_tenant_medium_blocked`, `velocity_gate_campaign_medium_quota`, `TRACE-7` (`medium_throttle_reason`, `campaign_medium_used/quota/remaining`).

### Step 8: Global Exclusivity Lock + Deduplication (V26.7.0 identity)
```python
# resolve_identity_key(): path for social/shared/consumer/multi-entity; domain for normal B2B companies
lock_entity = sha256(path_key) if path_mode else root_domain
lock_ref = db.collection("global_lead_locks").document(lock_entity)
if lock_doc.exists and lock_doc.to_dict().get("locked_until") > now_utc:
    continue  # Locked (3-day window)
lock_ref.set({"locked_until": now_utc + timedelta(days=3), "expire_at": ...})

lead_id = hashlib.sha256(f"{tenant_id}_{identity_key}".encode()).hexdigest()
doc_ref.create({"status": "processing", "matched_campaigns": [campaign_id], ...})
```

Lock-delete failures are logged at `ERROR` (V24.2 — was silent `except: pass`). Multi-entity path application logs `dispatch_multi_entity_path_identity` / `produce_multi_entity_path_identity`.

**Pre-PRISM TLD gate (V24.5.7):** Before running any PRISM scraping, `_process_single_url()` checks the domain TLD against a non-business list (`.org`, `.edu`, `.gov`, `.blog`, `.dev`, `.page`). If matched, the URL returns `skip_non_business_tld` immediately — saving 3–8 Serper credits that would otherwise be spent on PRISM WalledGardenHook queries + enrichment. Previously, this check only fired inside `deep_context_serper_dork()` *after* PRISM had already run.

**Page-type structural score cap (V24.6.0):** After Gemini scoring, before the score gate, a regex-based page-type classifier caps the Gemini score for structurally non-buyer page categories:

| Page Type | URL Pattern | Score Cap |
|---|---|---|
| Conference / event | `/conference/`, `/summit/`, `/program/proposals` | 3 |
| Government portal | `.gov`, `.mil`, `.govt.`, `/ministry/`, `/department/` | 2 |
| Academic repo | `/sol3/`, `/ssrn/`, `/arxiv/`, `/research/paper/` | 2–3 |
| Press release | `/press-release/`, `/newsroom/` | **7** (V26.0.4.1: raised from 4 — B2B event triggers) |
| Job board | `/jobs/`, `/careers/`, `/vacancies/` | **6** (V26.0.4.1: raised from 4 — hiring = buying signal) |

Even if Gemini gives a conference page 10/10 (keyword match), the cap reduces it to 3, which is below the score gate threshold of 6–7. Press releases and job boards are now capped higher because for B2B they represent legitimate buying signals (rebranding announcements, hiring Brand Managers = branding budget).

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

### Step 10: RLHF Pre-Screen + Gemini Scoring (V26.6.0 domain/strategy-aware)

**A. Python Fast-Fail Gate:** Heuristic blocklist check (global + tenant dynamic). Score > 3 → `failed`.

**B. Token Reduction — Density Extraction:** Top 10 most relevant paragraphs by keyword overlap with bio. Reduces Gemini token consumption ~80%.

**C. Multi-Vector Serper Enrichment:** GMB rating, LinkedIn presence, hiring intent signals.

**D. RLHF Python Interceptor:**
```python
fit_score = preferences_weights.get("hiring_intent", 0) * native_hiring_intent
for tech in tech_stack:
    fit_score += preferences_weights.get(f"tech_{tech}", 0)
if fit_score <= -3:
    # Preserve lead as status=rlhf_filtered (not hard delete) for diagnostics
    doc_ref.update({"status": "rlhf_filtered", ...})
```

**E. `final_score_and_dm` (V26.6.0):** Gemini scores the full DOM (or snippet fallback) with structured runtime context:
- Campaign cards include bio, keywords, `pain_point`, `target_angle_hook`, `unfair_advantage`, and enriched ICP context (from `context_builder` / campaign fields).
- **Branched fit rules** (not one generic B2B brochure rule):
  - `PLATFORM_MINING` — agent/listing/directory entities are valid without renter-style pain language.
  - `COMPETITOR_TOUCHPOINT` — reviewers/commenters are primary leads.
  - Consumer vectors — local service/listing fit without requiring B2B hiring intent.
  - B2B default — pure brochures still low; strong ICP company footprint may score mid-band (4–6).
- Lightweight **domain-family scoring guidance** (real_estate, saas, marketing_agency, manufacturing, healthcare, education, ecommerce, finance).
- Returns `scoring_context` for observability; logs `final_score_context_applied`, `final_score_decision`.

**F. Confidence adapter + hybrid promotion (V26.5.1):**  
`adapt_gemini_evaluation_for_confidence()` maps Gemini score / `confidence_level` / `pain_point` / contacts into the harvest-style schema expected by `calculate_lead_confidence()`. Hybrid rule promotes if confidence passes **or** Gemini score clears a policy-aware floor. Logs: `dispatch_confidence_adapter_used`, `dispatch_hybrid_promotion_triggered`, `dispatch_score_gate_eval` / `_drop`.

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

Inbound Radar has **two complementary paths**. Both are domain-aware as of V26.5:

| Path | Endpoint / Job | Role |
|------|----------------|------|
| **Visitor Beacon** | `POST /api/visitor-signals` | Anonymous page-view beacons from `sideio-tracker.js`; firmographic enrichment later |
| **Inbound Sentiment** | Cron → `jobs/inbound_sentiment_job.py` | Serper + Gemini OSINT intent mining per active campaign |

### 8.1 Opt-Out Gate (V24.5)
`visitor_signals_enabled` field on `users` document. If `false` → `204 No Content` immediately on beacon ingest. Inbound sentiment job only runs for tenants with `inbound_radar.enabled == true`.

### 8.2 Visitor Beacon path (`visitor_signals.py`)
- Writes `visitor_signals/{tenant_id}_{visit_hash}` (no cookies / no PII; IP hashed).
- Loads best active-campaign `system_domain_profile` (override preferred; 5‑minute cache).
- When a profile exists, stamps domain metadata + **actionable enrichment fields** (see §8.5). No profile → **identical legacy document shape** (BC).
- Does **not** invent an intent score; domain bias drives `enrichment_priority` and observability deltas only.
- Logs: `visitor_domain_profile_used`, `visitor_enrichment_priority_assigned`, `visitor_domain_adjustment_applied`.

### 8.3 Inbound Sentiment path (`inbound_sentiment_service.py` + job)
- Per active campaign: build Serper queries → Gemini intent classify → filter by write floor.
- Base floors: Gemini garbage filter **0.30**, Firestore write **0.45** (`MIN_INTENT_SCORE`).
- With `system_domain_profile`: floors adjusted via `shared.domain_gate.compute_intent_threshold()` using `strictness_bias` × `profile_confidence` scale (high=1.0, medium=0.6, low=0.3). Thin/low-confidence profiles get milder moves.
- Signals persist `domain_family`, `domain_source`, `profile_confidence`, `thin_campaign`, `strictness_bias`, `intent_threshold_used`, and enrichment priority fields.
- Logs: `inbound_domain_profile_used`, `inbound_domain_adjustment_applied`, `inbound_enrichment_priority_assigned`.

### 8.4 Intent Scoring & Lead Promotion
`intent_score` is a 0.0–1.0 float. Inbound leads (manual convert or auto-path) use:
```python
"normalized_score": round(sig.get("intent_score", 0.5) * 100)
```
- List API (`GET /api/inbound-signals`) respects each signal’s `intent_threshold_used` when present (else floor **0.35**) so domain-lenient writes are not dropped from the UI.
- Convert-to-lead (`PUT /api/inbound-signals/<id>/status`) copies domain + enrichment fields onto the `leads` document.

### 8.5 Enrichment Priority (actionable contract)
Computed by `shared.domain_gate.compute_enrichment_priority()` from domain family, `profile_confidence`, thin flags, optional intent score, and sourcing vector.

| Priority | Queue | Company resolve | Max lookups | Deep graph | Budget-tight |
|----------|-------|-----------------|-------------|------------|--------------|
| `high` | `realtime` | yes | 5 | yes | still run |
| `medium` | `batch` | yes | 2 | no | still run |
| `low` | `deferred` | no | 1 | no | **skip** |

**Helpers for workers:**
- `enrichment_plan_for_priority(priority)` — processing depth dict
- `enrichment_sort_key(doc)` — sort high → medium → low, then by intent
- `should_run_company_resolve(priority, budget_tight=)` — firmographic gate

**Decision highlights:** thin/low confidence → low; high confidence + B2B family → high; high confidence + consumer family (e.g. real_estate) demotes to medium for reverse-IP ROI; medium confidence + manufacturing/SaaS promotes to high; intent ≥ 0.70 can promote medium → high.

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

### 10.2 GET /api/leads (V24.5.6)
Supports `?sort_by=score&min_score=N` query params for CRM-style pipeline views.

**V24.5.6 fixes applied:**
- `min_score` filter now compares against `normalized_score` (0–100) directly. Previously multiplied `min_score × 10` again, making the filter 10× too aggressive (e.g., `min_score=5` filtered `normalized_score >= 50` instead of `>= 5`).
- Query now includes `.where(status == "new")` — previously returned all statuses including zombie `processing` stubs and `enrichment_pending` parking stubs.

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

## 16. DEPENDENCY CONSTRAINTS (V26.0.1)

| Library | Version | Constraints |
|---|---|---|
| `google-cloud-storage` | `2.19.0` | **PIPELINE-MAIN / DIGITAL-TWIN ONLY.** vertexai 1.71.1 requires `storage < 3.0.0`. |
| `google-cloud-storage` | `3.12.0` | All other services (orchestrator, scraper-heavy, etc.) |
| `vertexai` | `1.71.1` | Pinned for stability. Do not upgrade without full RCA on `gemini_service.py` compatibility. |
| `Flask` | `3.0.3` | Standardized baseline across all services. |
| `tenacity` | `9.1.4` | Standardized retry logic. |
| `PyJWT` | `2.10.1` | Orchestrator only. Used by `social_redirect.py` for JWT HS256 token verification. |

> [!NOTE]
> No new pip dependencies were introduced in V26.0.1. All V26 features use existing imports.

---

## 17. KEY DESIGN INVARIANTS (NEVER BREAK)

1. **Tenant isolation:** Every Firestore document a tenant touches must have `tenant_id == user.uid`. Every BQ query must be scoped by `tenant_id`.
2. **Lead dedup ID:** Must be deterministic: `sha256(tenant_id + '_' + identity_key)`. Use **path** identity for social, shared platforms, consumer vectors, **and multi-entity portal hosts** (even under B2B). Use **root domain** only for normal B2B company hosts. SSOT: `shared/multi_entity_hosts.py`.
3. **Score gate:** Promotion is confidence-threshold based (adaptive, bounded) with hybrid Gemini score floor after evaluation adapter; non-promoted leads are retained as `scored_out` for diagnostics and model tuning.
4. **Firestore rules:** `leads` and `campaigns` are the only collections the frontend can read/write directly. All other collections are Admin SDK only.
5. **SW Firebase bypass:** Never let the service worker intercept `googleapis.com` or `google.com` traffic.
6. **Wallet shards:** True balance = `allocated_credits − consumed_credits − SUM(wallet_shards/0-9) − reserved_credits`.
7. **OIDC for cron:** `/api/internal/cron/sweep` validates OIDC tokens with audience = `ORCHESTRATOR_URL` — never Firebase ID tokens.
8. **WhatsApp disabled:** `whatsapp_enabled` Firestore flag must remain `false`. Do not re-enable without explicit approval (AGENTS.md).
9. **Webhook disabled:** Webhook features are out of scope. Do not re-enable without explicit approval.
10. **Consumer archetypes:** Defined once in `pipeline-main/core/constants.py`. Import from there — never duplicate.
11. **No silent failures:** All `except: pass` is prohibited. Every exception must be logged with enough context to debug without reproduction.
12. **PII before BQ:** Always apply `_scrub_pii()` before writing lead text to BigQuery N-gram tables.
13. **Intelligence strategy immutable post-creation:** `intelligence_strategy.primary` is classified at campaign creation by Gemini and must not be changed after creation. The entire pipeline (query_brain, source_router, dispatch entity extraction) branches on this value.
14. **Entity extraction thread safety:** `_ENTITY_DOMAIN_COUNTS` in `dispatch.py` is a module-level dict guarded by `_ENTITY_DOMAIN_LOCK`. All read-modify-write operations MUST hold the lock.
15. **Contact endpoints schema:** Entity-extracted leads must use `list[dict]` format: `[{"type": "email", "value": "..."}]`. Never flat strings.
16. **Strategy-aware blacklist:** PLATFORM_MINING strategy preserves review/directory sites (g2, capterra, yelp) in blacklist — these are intelligence sources, not noise.
17. **Multi-entity portals:** Never domain-lock or domain-dedup hosts in the multi-entity catalogue (Bayut, PropertyFinder, Dubizzle, etc.). Path-level identity is mandatory for lock, lead id, and scraped cache.
18. **Velocity Medium isolation:** Tenant hard cap remains; per-campaign Medium soft quota must not be bypassed without disabling `MEDIUM_CAMPAIGN_QUOTA_ENABLED` deliberately.

---

## 18. KNOWN OPEN ISSUES

These are structural issues identified across V24–V25 audit cycles.

| # | Severity | Component | Issue | Status |
|---|---|---|---|---|
| I-1 | ✅ Resolved | `neg_shield.py` | `Negative_Signals` BQ table missing `sourcing_vector` column | Fixed 2026-07-02 |
| I-2 | 🟠 High | `serper_service.py` | BQ audit telemetry schema mismatch → `serper_audit_broker_non_200` | Open — audit table schema needs investigation |
| I-3 | 🟡 Medium | `dispatch.py` | Velocity gate Firestore composite index missing | Mitigated V26.7 — fail-open campaign quota + degraded Medium sample; index still recommended |
| I-4 | ✅ Resolved | `orchestrator` | `INTERNAL_CRON_SECRET` env var not set | Fixed 2026-07-02 |
| I-5 | ✅ Resolved | `dispatch.py` | `enrichment_pending` leads not counted in velocity gate | Fixed earlier; tenant hard cap includes enrichment_pending |
| I-6 | ✅ Resolved | All campaigns | Pre-filter context starvation | Fixed V24.6.1: `context_builder.py` |
| I-7 | ✅ Resolved | `serper_service.py` | B2B had no temporal filter | Fixed V24.6.0: `tbs=qdr:y` |
| I-8 | ✅ Resolved | `dispatch.py` | No page-type score cap | Fixed V24.6.0: structural regex cap |
| I-9 | ✅ Resolved | `orchestrator` | `--allow-unauthenticated` exposed all internal endpoints | Fixed V25.6.0: `--no-allow-unauthenticated` |
| I-10 | ✅ Resolved | `agents.py` | No auth middleware — suspended users could CRUD agents | Fixed V25.6.0: `@require_auth` |
| I-11 | ✅ Resolved | `internal.py` | Harvest sweep dead — `remaining_credits` field nonexistent | Fixed V25.6.0: correct credit formula |
| I-12 | ✅ Resolved | `leads.py` | Signal-to-lead credit non-transactional race condition | Fixed V25.6.0: `@transactional` wrapper |
| I-13 | ✅ Resolved | `me.py`, `l0_admin.py` | Wallet display ignored `total_consumed` | Fixed V25.6.0: `max()` formula |
| I-14 | ✅ Resolved | `gemini_service.py` | `response.text` crash on empty candidates | Fixed V25.6.0: `_safe_extract()` guard |
| I-15 | ✅ Resolved | `personas.py` | Cache invalidation queried wrong collection | Fixed V25.6.0: tenant-scoped subcollection |
| I-16 | ✅ Resolved | `app.js` | 11 XSS injection points (campaign names, keywords, geo, timeline) | Fixed V25.6.0: `_escapeHTML()` |
| I-17 | ✅ Resolved | `dispatch/produce` | B2B domain-level lock/dedup on multi-entity portals | Fixed V26.7.0: `multi_entity_hosts.py` path identity |
| I-18 | ✅ Resolved | `dispatch.py` | One campaign could starve others of Medium intake | Fixed V26.7.0: per-campaign Medium soft quota |
| I-19 | ✅ Resolved | `gemini_service` / `lead_confidence` | Gemini score ignored by confidence gate (schema mismatch) | Fixed V26.5.1 adapter + hybrid promotion |

**Diagnostic shortcut for operators:** Filter Cloud Run logs for `context_builder_assembled sections=<N>`. Any campaign with `sections < 3` is a thin-context campaign that may produce poor leads — prompt customer to fill in bio, pain_point, or link a persona.

---

## 19. V25.5.x–V25.6.0 QUALITY GATE ARCHITECTURE

V25.5.x introduced a 4-phase lead quality system. V25.6.0 fixed the remaining infrastructure issues.

### 19.1 Noise Gates (V25.5.0)
- **36 blocked subreddits** (news, politics, memes) filtered at Serper level
- **15 megathread regex patterns** prevent deep-thread noise
- **38 blocked content farm domains** (listicles, directories)
- **Topic coherence gate**: Gemini Flash checks campaign–signal relevance before scoring
- **Staleness filter**: B2C=14d, B2B=60d age threshold on signal metadata (V25.6.0: also applied in signal_harvest pathway)

### 19.2 Query Precision (V25.5.0)
- **Subreddit-targeted queries** per archetype
- **Buyer language injection** into search queries
- **Query exhaustion/refresh** when sources return stale content
- **7-day blacklist TTL** (V26.0.4.1: reduced from 30 — prevents month-long starvation from a single noise rejection), 8-domain count cap (V26.0.3: reduced from 15)
- **V26.2.0 query governance**: deterministic query portfolio balancing, blacklist-size control, and platform-mining query injection before Serper calls.
- **V26.2.0 novelty memory**: producer stores query signatures per campaign and suppresses recently repeated queries to protect Serper credits.
- **V26.2.0 exhaustion escalation**: consecutive zero-fresh cycles raise escalation level and inject alternate objective/source packs until novelty resumes.

### 19.3 LQS Scoring (V25.5.1)
Multi-dimensional Lead Quality Score computed per signal:
- Topic coherence, buyer intent, freshness, reachability, DM confidence
- Calibrated Gemini prompt with $ anchoring
- Reddit 1500-char primary-post cap
- Score distribution telemetry
- **Frontend LQS badge** (V25.6.0): green ≥70%, amber 40–70%, red <40%

### 19.4 Adaptive Learning (V25.5.1)
- Per-source accept rate tracking (`source_stats` subcollection)
- Pattern mining from accepted leads (`accepted_patterns`)
- 7-reason rejection granularity (V25.6.0: synced to frontend)
- `force_query_refresh` flag on exhaustion detection
- **V26.3.0 adaptive dispatch policy (`adaptive-v1`)**: campaign-level strict/balanced/recovery modes dynamically tune medium intake and confidence thresholds using queue depth, recent conversion pressure, and exhaustion signals.
- **V26.4.0 adaptive dispatch policy (`adaptive-v2`)**: domain profile awareness (`domain_family`, low-liquidity market hints) and domain-aware prefilter tier pruning.
- **V26.5.0 adaptive dispatch policy (`adaptive-v3`)**: domain contribution = `strictness_bias × 8.0 × confidence_scale`, where `confidence_scale` is **1.0 / 0.6 / 0.3** for `profile_confidence` high/medium/low. Thin/low-confidence domains cannot swing the gate aggressively. Policy returns `domain_threshold_delta`, `domain_strictness_bias`, `profile_confidence`, `thin_campaign` for logs and `scored_out` diagnostics.

---

## 20. V26.0 MULTI-STRATEGY OSINT ENGINE

### 20.1 Strategy Classification (V26.0)
At campaign creation, Gemini classifies the ICP into one of 5 intelligence strategies. The classification is stored as `campaign.intelligence_strategy.primary` and drives the entire pipeline:

| Strategy | Description | Query Generation | Source Priority | Entity Extraction |
|---|---|---|---|---|
| `PLATFORM_MINING` | Extract from competitor directories/aggregators | `site:` queries targeting mining_targets | Serper Discovery, Google Reviews | ✅ Full entity extraction |
| `COLLOQUIAL_DISCOVERY` | Search in buyer's own language | Gemini translates ICP to colloquial queries | Reddit, HackerNews, Consumer Forums | ❌ Standard scoring |
| `COMPETITOR_TOUCHPOINT` | Mine competitor reviews for dissatisfied customers | `site:` queries on review platforms | Google Reviews (6h cooldown), Reddit | ✅ Full entity extraction |
| `PROFESSIONAL_NETWORK` | Target professional networks and events | LinkedIn/conference dork queries | Job Posts, RSS Feeds | ❌ Standard scoring |
| `EVENT_TRIGGER` | Monitor hiring, funding, technology changes | Event/signal-specific dorks | Job Posts, RSS Feeds, HackerNews | ❌ Standard scoring |

### 20.2 Source Router Architecture (V26.0)
**Location:** `pipeline-main/services/source_router.py`

The Source Router is the multi-source OSINT orchestrator. It manages 10 pluggable signal source modules (`signal_sources/`) and routes queries based on intelligence strategy:

```
source_router.execute_multi_source_pipeline(campaign, queries)
  ├── Determine active sources from strategy + sourcing_vector
  ├── Apply source-specific cooldowns (e.g., GoogleReviews: 6h for COMPETITOR_TOUCHPOINT)
  ├── Execute sources in parallel via ThreadPoolExecutor(max_workers=5)
  ├── Merge results via intelligence_mesh.py (cross-source dedup)
  └── Return unified signal list for scoring
```

**Signal Source Plugins** (`signal_sources/`):

| Plugin | Source | Cooldown | Entity Extraction |
|---|---|---|---|
| `serper_discovery.py` | Google Search via Serper API | Standard (drip) | ❌ |
| `reddit.py` | Reddit threads | 30 min | ❌ |
| `hackernews.py` | HN posts/comments | 30 min | ❌ |
| `google_reviews.py` | Google Business Reviews | 6h (COMPETITOR_TOUCHPOINT) / 24h (default) | ✅ |
| `consumer_forum.py` | Consumer forums (Quora, etc.) | 30 min | ❌ |
| `classified_listings.py` | Classified ads (Craigslist, etc.) | 1h | ❌ |
| `job_posts.py` | Job boards (Indeed, etc.) | 2h | ❌ |
| `rss_feed.py` | RSS/Atom feeds | 30 min | ❌ |
| `youtube.py` | YouTube videos/comments | 1h | ❌ |

### 20.3 Entity Extraction Engine (V26.0)
**Location:** `pipeline-main/api/routers/dispatch.py` (lines 1654+)

For `PLATFORM_MINING` and `COMPETITOR_TOUCHPOINT` strategies, a single aggregator page (e.g., a Yelp directory listing) can yield multiple leads. The entity extraction engine:

1. Receives scraped text from an aggregator page
2. Calls `call_gemini_2_5()` with `expect_json=True` and a structured `response_schema`
3. Extracts individual entities (businesses/people) with contact info
4. Applies per-domain rate limiting (max 5 entities per domain per batch) via `_ENTITY_DOMAIN_COUNTS` + `_ENTITY_DOMAIN_LOCK`
5. Writes each entity as a separate lead with `matched_campaigns` and `contact_endpoints` in `list[dict]` format

### 20.4 Colloquial Translation (V26.0)
**Location:** `pipeline-main/services/query_brain.py` (line 884+)

When `vocabulary_notes` is present in the campaign's `intelligence_strategy`, the Gemini prompt includes explicit instructions to translate professional terminology into buyer language. Example:
- Professional: "HVAC maintenance services near Austin TX"
- Colloquial: "my AC keeps leaking Austin" / "air conditioner broken who to call"

This is the core differentiator — no other tool searches in the buyer's own language.

### 20.5 Strategy-Aware Blacklist Filtering (V26.0)
**Location:** `pipeline-main/services/query_brain.py` (line 1084+)

The default blacklist (`_DEFAULT_BLACKLIST`) includes review/directory sites like `g2.com`, `capterra.com`, `yelp.com`. For `PLATFORM_MINING` strategy, these are **intelligence sources**, not noise. V26 conditionally preserves them:
- `PLATFORM_MINING` → keeps g2, capterra, yelp, trustpilot in queries
- `COMPETITOR_TOUCHPOINT` → keeps review sites in queries
- All other strategies → standard blacklist applies

Post-generation, a regex filter strips any `-site:` exclusions that conflict with the strategy's mining targets (V26.0.1 fix).

### 20.6 Smart Pipeline Enhancements (V26.0.4)

**Vocabulary Notes as Query Seeds:** `vocabulary_notes` from `intelligence_strategy` are now injected into the Gemini query generation prompt alongside user keywords. Previously only used for colloquial translation, they now augment thin keyword lists (e.g., campaign name "Oman Realty" → seeds include "property, apartment, villa, buy, rent, Muscat").

**Thin Bio Enrichment:** Detects generic bios like `"Product/Service: Oman Realty"` and augments them with `vocabulary_notes` before passing to Gemini. This enriched bio flows into the Gemini prompt's "Target Pain Point / Bio" field.

**Platform Domain Resolution:** `platform_targets` from `intelligence_strategy` (e.g., "Property Finder Oman") are resolved to searchable domains (e.g., `propertyfinder.com`) via `_PLATFORM_DOMAIN_MAP` (22 brand→domain mappings). Enables proper `site:` queries for platform mining.

**Dynamic Subreddit Selection:** Fallback subreddit lists are now 3-layer: industry-specific (real estate, education, marketing) + geo-specific (Oman, India, UAE) + archetype base. Capped at 8 subreddits.

**Reddit RSS → Serper Fallback:** When Reddit RSS feeds return 0 items (blocked by Cloud Run IP), the system falls back to Serper `site:reddit.com/r/{subreddit}` queries. Budget-controlled at max 6 queries (2 subreddits × 3 terms).

**Colloquial Translation Fix:** Fixed `TypeError` crash caused by unsupported `temperature=0.4` kwarg in `call_gemini_2_5()`. Colloquial translation now fires correctly.

### 20.7 B2B Regression Fixes (V26.0.4.1)

Git history analysis identified 7 regressions introduced between V23 (April, B2B working) and V26 (current):

| # | Regression | Fix |
|---|---|---|
| R1 | LinkedIn blocked at `_ENTERPRISE_DOMAINS` (V24.6.3) | **Unblocked** — snippets sufficient for scoring |
| R2 | Serper sanitizer stripped LinkedIn/Facebook `site:` operators | **Removed** from forbidden list |
| R4 | Content farm filter blocked Bloomberg, Reuters, CNBC | **B2B news exception** added |
| R5 | Page-type score cap: press=4, jobs=4 (threshold=7) | **Raised**: press=7, jobs=6 |
| R6 | CDN prefix `research.*` too broad | **Removed** — academic repos already blocked |
| R7 | RLHF blacklist 30-day TTL too aggressive | **Reduced** to 7 days |

---

## 21. DOMAIN INTELLIGENCE SYSTEM (V26.4–V26.5)

Domain Intelligence classifies each campaign into a **vertical domain family** and emits a runtime profile that conditions query generation, pre-filter strictness, promotion thresholds, and inbound enrichment priority.

### 22.1 Module map

| Module | Location | Role |
|--------|----------|------|
| `domain_constants.py` | `services/shared/` | **SSOT** for `KNOWN_DOMAIN_FAMILIES` (14 families), aliases, `is_valid_domain_family()`, `normalize_domain_family()` |
| `domain_gate.py` | `services/shared/` | Cross-service gates: `compute_intent_threshold()`, `compute_enrichment_priority()`, `enrichment_plan_for_priority()`, `enrichment_sort_key()` |
| `domain_intelligence.py` | `pipeline-main/services/` | `infer_domain_profile()`, override validate/expand, `resolve_campaign_domain_profile()`, `apply_domain_query_profile()`, `build_domain_impact_summary()` |
| `adaptive_policy.py` | `pipeline-main/services/` | `build_dispatch_policy()` — **adaptive-v3** with damped `strictness_bias` |
| Campaigns API | `orchestrator/api/routers/campaigns.py` | Validates/persists `domain_override` using shared constants |

**Supported families:** `real_estate`, `saas`, `manufacturing`, `professional_services`, `healthcare`, `education`, `finance`, `ecommerce`, `hospitality`, `logistics`, `construction`, `hr_recruiting`, `marketing_agency`, `general_services`.

### 22.2 Resolution precedence
```
domain_override (valid)  →  expand to full profile (override_active=true, profile_confidence=high)
         ↓ missing/cleared
system_domain_profile cache (current version, not override-stale)
         ↓ missing/stale
infer_domain_profile(campaign)  →  persist system_domain_profile
```
Invalid overrides fail open: log + auto-infer (pipeline never aborts).

### 22.3 Profile fields (runtime contract)
| Field | Meaning |
|-------|---------|
| `domain_family` | Vertical label |
| `confidence` | Numeric 0–1 match strength |
| `profile_confidence` | **high / medium / low** tier for damping |
| `thin_campaign` | Sparse ICP input (short bio, few keywords, no persona) |
| `input_richness` | high / medium / low field depth |
| `strictness_bias` | ∈ [−0.5, +0.5]; negative = more lenient promotion |
| `soft_domain_adjustments` | True when low-confidence defaults applied |
| `preferred_sources` / `preferred_query_hints` | Domain-biased discovery surfaces |
| `blocked_subreddits` | Noise communities to drop |
| `liquidity_level` / `low_liquidity_market` | Geo/OSINT density |

### 22.4 Thin campaign graceful degradation
Sparse campaigns use softer family pick thresholds, light name/keyword industry hints, and **cannot claim high `profile_confidence`**. Low tier attenuates `strictness_bias` (~35%), caps preferred-hint injection (`max_inject` ≤ 1), and disables aggressive pre-filter directory softening. Well-filled campaigns keep original pick thresholds and full bias (backward compatible).

### 22.5 Pipeline consumers
| Stage | Domain effect |
|-------|----------------|
| **Produce** | `apply_domain_query_profile` after governance: drop blocked subreddits, boost/inject preferred `site:` queries |
| **Query brain** | Seeds platform-mining from preferred platforms; platform mining for domain families even when not pure B2C |
| **Pre-filter (Gemini)** | V26.6: domain/strategy/vector in prompt; PLATFORM_MINING forces directory softening; family calibration examples; STEP 4 consumer-gated |
| **Final scoring (Gemini)** | V26.6: branched fit rules + domain guidance + enriched campaign cards; `scoring_context` on lead docs |
| **Dispatch policy** | `threshold_adjustment` includes domain delta; medium budget mildly lifted in recovery + low liquidity |
| **Velocity Medium** | V26.7: tenant hard cap + per-campaign soft quota (see Step 7b) |
| **Lock / dedup / cache** | V26.7: multi-entity hosts always path-level (vector-independent) |
| **Impact summary** | End-of-cycle structured log + funnel payload; compact copy on `scored_out` |

### 22.6 Inbound consumers
See **§8**. Both Visitor Beacon and Sentiment Radar load `system_domain_profile` when present; absent profile = legacy behaviour.

### 22.7 Multi-entity host identity (V26.7.0)

**Module:** `services/shared/multi_entity_hosts.py` (copied into pipeline container as `./shared`).

**Problem:** B2B domain-level locks/dedup on portals (`bayut.com`, `propertyfinder.*`, …) collapsed thousands of agents/listings into one slot and could exclusivity-lock an entire portal for all tenants for the lock window.

**Rule:** If the host is in the multi-entity catalogue (or matches an env extension), identity is **always path-level** for:
1. `global_lead_locks` document id (hash of path key)
2. Lead document id `sha256(tenant_id + '_' + path_key)`
3. `scraped_cache` document id

Normal single-company domains under B2B remain domain-level. Social/shared/consumer path behaviour is unchanged.

**Config:** `MULTI_ENTITY_HOST_SUFFIXES` — optional comma-separated extra suffixes at runtime.

**Logs:** `dispatch_multi_entity_path_identity`, `produce_multi_entity_path_identity`.

---

## 22. VERSION HISTORY (RECENT)

| Version | Date | Key Changes |
|---|---|---|
| **V26.7.0** | **2026-07-16** | **Multi-entity path identity (`shared/multi_entity_hosts.py`) for portal lock/dedup/cache even under B2B; per-campaign Medium soft quota (`MEDIUM_CAMPAIGN_QUOTA_24H`, optional `campaign.medium_intake_quota_24h`) with tenant hard cap preserved; expanded velocity/identity observability.** |
| **V26.6.0** | **2026-07-16** | **Domain/strategy/vector-aware LLM gates: `pre_filter_gemini` and `final_score_and_dm` receive structured runtime context; branched PLATFORM_MINING / COMPETITOR_TOUCHPOINT / consumer / B2B fit rules; domain-family calibration guidance; scoring_context diagnostics.** |
| **V26.5.1** | **2026-07-16** | **Confidence evaluation adapter + hybrid Gemini score-floor promotion so Serper-path `final_score_and_dm` scores are not ignored by `calculate_lead_confidence`.** |
| **V26.5.0** | **2026-07-16** | **Domain Intelligence GA: thin-campaign `profile_confidence` damping, manual `domain_override`, SSOT `domain_constants.py` + `domain_gate.py`, adaptive-v3 (`strictness_bias` × confidence scale), domain impact summaries, domain-aware Inbound Radar (visitor beacon + sentiment) with actionable `enrichment_priority` contracts (realtime/batch/deferred).** |
| **V26.4.0** | **2026-07-16** | **Domain intelligence layer (`system_domain_profile`) added across produce/dispatch, domain-aware query pruning and tier filtering, adaptive policy upgraded to `adaptive-v2` with domain conditioning, and architecture invariants updated to reflect confidence-based promotion + `scored_out` persistence.** |
| **V26.3.0** | **2026-07-16** | **Adaptive campaign dispatch policy engine (`adaptive-v1`), dynamic confidence threshold adjustments, `scored_out` status persistence instead of hard delete, snippet-cache freshness TTL guard (`SNIPPET_CACHE_TTL_HOURS`), and dedup exclusion of terminal non-promoted/failed statuses to reduce starvation across mixed campaign domains.** |
| **V26.2.0** | **2026-07-15** | **Autonomous campaign enrichment and self-healing backfill, query governance integration in producer, campaign-scoped query novelty memory, exhaustion escalation, dedup recrawl TTL (`DEDUP_RECRAWL_DAYS`), and deployment/runtime hardening for shared module packaging.** |
| **V26.0.4.1** | **2026-07-05** | **B2B regression fixes: unblock LinkedIn from `_ENTERPRISE_DOMAINS`, remove LinkedIn/Facebook from Serper sanitizer `forbidden` list, B2B news exception in content farm filter (Bloomberg, Reuters, CNBC pass through), raise page-type score caps (press: 4→7, jobs: 4→6), remove `research.*` CDN prefix, reduce RLHF blacklist TTL 30→7 days.** |
| **V26.0.4** | **2026-07-05** | **Smart pipeline: vocabulary_notes as Gemini query seeds, thin bio enrichment with vocab, platform domain resolution (22 brand→domain mappings), dynamic industry/geo subreddit selection, Reddit RSS→Serper fallback, fix colloquial translation crash (unsupported temperature kwarg).** |
| V26.0.3 | 2026-07-04 | Hybrid strategy engine: prioritize never exclude, drop -intitle: operators, cap -site: at 8, unblock review sites. |
| V26.0.1 | 2026-07-04 | E2E audit fixes: threading.Lock race condition, call_gemini_2_5 signature fix, entity leads fields, source_router hoisting, post-generation -site: strip. |
| V26.0.0 | 2026-07-04 | Multi-Strategy OSINT Intelligence Engine: 5 intelligence strategies, source_router.py (10 signal source plugins), entity extraction engine, colloquial translation, intelligence_strategy campaign schema. |
| V25.6.0 | 2026-07-04 | 87-issue failure remediation across 23 files. P0: Orchestrator auth, Gemini crash guard, timeline XSS. P1: transactional credits, wallet formula, neg-signal dedup, LQS badge. |
| V25.5.0–V25.5.1 | 2026-07-04 | Lead quality gates + LQS multi-dimensional scoring + adaptive learning. |
| V25.2.1–V25.2.3 | 2026-07-03 | Build failure RCA, Inbound Radar hardening, dependency standardisation, PyJWT, BQ DDL. |
| V24.6.0–V24.6.1 | 2026-07-02 | Universal context builder, B2B temporal filter, page-type score cap, BQ sourcing_vector column. |
| V24.2.0–V24.5.7 | 2026-07-01 | OIDC validation, cron secret, PII scrubbing, consumer archetypes, RLHF yield-weight, CDN filter, queue backpressure, CRM retry. |
