# Sideio Leads V12 - Enterprise Master System Architecture & Developer Handbook

## 1. Executive Summary
Sideio Leads is an enterprise-grade, multi-tenant B2B lead generation platform. It automatically scans the internet based on natural language product descriptions ("bios"), identifies high-value decision-makers, scores their lead quality using Large Language Models (LLMs), and drafts personalized, anti-spam WhatsApp/LinkedIn DMs. The V12 platform operates on an advanced "Bring Your Own Token" (BYOT) architecture, governed by a strict internal credit economy, and powered by an autonomous, zero-cost Python Reinforcement Learning from Human Feedback (RLHF) Loop.

The system is rigorously decoupled into three core domains:
1. **Frontend PWA:** A vanilla JavaScript, strictly cached, static asset application served securely via Firebase Hosting featuring Premium Intelligence Badging.
2. **Orchestrator Gateway (Cloud Run):** The master REST API, auth gateway, telemetry aggregation layer, and RLHF mutation handler.
3. **Pipeline Worker (Cloud Run):** An asynchronous, globally locked processing node utilizing Multi-Vector OSINT (Symptom Dorks, GMB, Social, Hiring), structural Tech-Stack regexes, and Vertex AI (Gemini) inferencing.

---

## 2. Infrastructure Deployment Topology

The entire state is defined programmatically via Terraform and deployed into Google Cloud Platform (GCP).

- **Hosting:** Firebase Hosting (for `index.html`, `app.js`, `styles.css`).
- **Compute:** Google Cloud Run (Fully managed, stateless containers scaling to zero).
- **Asynchronous Queuing:** Google Cloud Tasks.
- **Database:** Firebase Firestore (NoSQL Document Store).
- **Authentication:** Google Identity Platform (Firebase Auth).
- **Secrets Management:** Google Cloud Secret Manager.

### High-Level Data Flow
1. User interacts with the **Frontend PWA**, which mints a Firebase OIDC JWT token.
2. The Orchestrator Gateway authenticates the token, performs RBAC, and records telemetry.
3. The Orchestrator packages `active` campaigns and injects them into **Cloud Tasks**.
4. Cloud Tasks pushes the JSON payload to the **Pipeline Worker** using a verified OIDC identity.
5. The Pipeline extracts target accounts using Symptom Dorks, Multi-Vector Serper sweeping, and zero-cost RLHF cutoffs.
6. Target URLs are locked using Unified Account Resolution (UAR) to prevent duplicated effort cross-campaign.
7. Leads cleanly pass Vertex AI extraction and inherently populate Firestore while optionally firing Meta WhatsApp API pushes.

---

## 3. Database Schema (Firestore)

### `users` (or `tenants`)
Manages Identity, Governance, Economy, and RLHF Weightings.
- `tenant_id` [String] (Maps to Firebase UID).
- `email` [String]
- `role` [String] (`admin` or `super_admin`).
- `approval_status` [String] (`pending` or `approved`).
- `is_active` [Boolean] (L0 Governance kill-switch).
- `wallet.allocated_credits` [Int] & `wallet.consumed_credits` [Int]
- `preferences_weights` [Map] (Tracks boolean RLHF values dynamically like `tech_wordpress: 5`, `hiring_intent: 12`).

### `campaigns`
The matrices for defining target audiences natively.
- `id` [String] / `tenant_id` [String] / `status` [String] 
- `bio` [String] / `keywords` [String] / `gl` [String] / `location` [String]

### `leads`
The deeply enriched, universally locked targets.
- `id` [String] (Deterministic UAR Hash: `tenant_id` + `target_domain`).
- `tenant_id` [String]
- `matched_campaigns` [Array] (Tracks cross-campaign matches via `firestore.ArrayUnion`).
- `url` [String] (The specific lead entry point).
- `status` [String] (`processing`, `new`, `contacted`, `converted`, `ignored`).
- `score` [Int] (1-10 Vertex AI confidence index).
- `pain_point` [String], `dm` [String], `email` [String], `linkedin` [String], `phone` [String]
- `tech_stack_found` [Array] (e.g., `["shopify", "stripe"]`).
- `hiring_intent_found` [String] (Extracted dynamically).
- `icebreaker_angle` [String]

### `global_lead_locks`
Cross-tenant exclusivity tracking to prevent spamming the same company.
- `id` [String] (Root level domain like `acme.com`).
- `locked_until` [Timestamp] (Absolute expiration date of the 14-day lock limit).

---

## 4. The Orchestrator Gateway (API & Core Node)

The Orchestrator (`services/orchestrator/main.py`) controls strictly gated gateways.

### REST Endpoints
*   `GET /api/me`, `/api/campaigns`, `/api/leads`: Strict user boundaries querying on `tenant_id` logic. 
*   **The RLHF Backpropagation** (`PUT /api/leads/...`):
    When a UI button is pushed to set a lead context dynamically to `converted` or `ignored`, the Orchestrator instantly identifies the target's `tech_stack_found` strings and `hiring_intent_found`. It applies native `firestore.Increment(1)` or `-1` to the underlying `preferences_weights` user document.

### L0 Governance Layer (`/api/l0/...`)
Exclusivity for `super_admin`: Upgrade, mint credits, or globally suspend instances. Also drives Macro Intelligence data `GET /api/l0/trends` computing global Maps + Dimensions on current active campaigns.

---

## 5. The Intelligence Pipeline Worker (The Quality Moat)

The Pipeline (`services/pipeline-main/main.py`) leverages bleeding-edge operations logic.

### 1. Vector 0: The Symptom Discovery Funnel
Before blindly guessing URLs, the script feeds the user's `bio` to `gemini-1.5-flash` natively asking for extremely specific Google Search dorks regarding companies displaying public symptoms of a problem. These results are merged with user keywords and passed to Serper.

### 2. Unified Account Resolution (UAR) & Exclusivity Locking
- When targeting a URL, the script strips domain to `acme.com`. 
- **Global Lock:** Validates against `global_lead_locks`. Drops execution entirely if another user touched `acme.com` within 14 days.
- **UAR Hashing:** Attempts atomic Firestore `.create()` using ID `tenant_id_domain`. If an `AlreadyExists` exception throws, it immediately merges the current `campaign_id` into the document's array via `ArrayUnion` and bails out, saving raw API credits.

### 3. Multi-Vector Serper Context Dorking
Retrieval Augmented Generation structure pulling dynamically:
- **Vector A (GMB):** Grabs Star Ratings and location address.
- **Vector B (Social):** LinkedIn/Facebook metadata.
- **Vector C (Hiring Pulse & Expansion):** Cross-references the domain against `linkedin.com/jobs`, `indeed.com`, `naukri.com`, and `instahyre.com`.

### 4. Zero-Cost Python Heuristics (The Predictor Cutoff)
- **Tech Stack X-Ray:** Natively parses scraped HTML locally looking for explicit SaaS scripts (`cdn.shopify`, `wp-content`, `intercom`) instantly.
- **Intent Scan:** Reads Vector C snippets natively in Python extracting "lpa", "lakh", "apply today", returning a fast boolean.
- **RLHF Gate:** Mathematical comparison (`Fit Score`) is applied querying the calculated parameters against `preferences_weights`. If `score <= -3`, the Pipeline structurally `.delete()`s the UAR stub and moves on *without deploying Vertex AI*.

### 5. Final AI Extraction & Communication Push
- If it passes the filter, Gemini pre-reads all 3 Search Vectors, the raw Tech-Stack variables, and the webpage DOM to formulate a hyper-personalized `"Trojan Horse"` DM angle using exact JSON schema mapping.
- Automatically connects to Meta WhatsApp and delivers the lead and drafted response strictly if the AI `score >= 8`.

---

## 6. Frontend Architecture (PWA)

Located in `public/`. Follows raw Vanilla JS standards for ultimate performance and zero-dependency bloat.

### Caching and Desync Safety
The Application strictly leverages a Service Worker utilizing a Network-First strategy. Updates use dynamic timestamp parameters on API gateways (`?rt=...`) and iteration bumps in `index.html` (`?v=16`) to physically bypass persistent caches dynamically.

### Premium DOM Rendering (Enterprise UI Facelift)
Data structures returned by the Orchestrator mapping `tech_stack_found` or `hiring_intent_found` are translated seamlessly into visual `<span class="badge">` components, generating `🔒 Exclusive Lead`, `🟢 Hiring: True` and cyan `⚡ shopify` badges.

### Analytics Cartography
Global data loops natively instantiate dynamic `Chart.js` integrations displaying funnel visualizations (`New`, `Contacted`, `Converted`). Macro maps inherently scan geographic dimensions rendering local B2B trend data structurally to the super admins.
