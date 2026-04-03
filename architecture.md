# Sideio Leads V11 - Master System Architecture & Developer Handbook

## 1. Executive Summary
Sideio Leads is an enterprise-grade, multi-tenant B2B lead generation platform. It automatically scans the internet based on natural language product descriptions ("bios"), identifies high-value decision-makers, scores their lead quality using Large Language Models (LLMs), and drafts personalized, anti-spam WhatsApp/LinkedIn DMs. The platform operates on a "Bring Your Own Token" (BYOT) architecture for enterprise outbound messaging and utilizes a strict internal credit economy.

The system is rigorously decoupled into three core domains:
1. **Frontend PWA:** A vanilla JavaScript, strictly cached, static asset application served securely via Firebase Hosting.
2. **Orchestrator Gateway (Cloud Run):** The master REST API, auth gateway, and batched Task Dispatcher.
3. **Pipeline Worker (Cloud Run):** An asynchronous, scaled processing node handling heavy I/O (scraping), Serper integrations, and Vertex AI (Gemini) inferencing.

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
2. The PWA sends a REST request to the **Orchestrator Gateway**.
3. The Orchestrator authenticates the token, performs RBAC (Super Admin vs Admin), and mutating Firestore.
4. The Orchestrator routinely sweeps for `active` campaigns and packages them into JSON payloads.
5. It injects these payloads into **Cloud Tasks**.
6. Cloud Tasks securely POSTs the payload to the **Pipeline Worker** using a verified service account OIDC identity.
7. The Pipeline Worker processes the lead (Serper API -> Gemini Pre-Filter -> Scrape -> Gemini Extraction).
8. The Pipeline natively pushes the extracted lead directly into Firestore and optionally pushes a notification via the Meta WhatsApp API.

---

## 3. Database Schema (Firestore)

The application relies strictly on Google Firestore. Data integrity is enforced via application-tier gateways rather than raw database rules, as all requests route through the Orchestrator.

### `users` (or `tenants`)
Manages Identity, Governance, and Telemetry Economy.
- `tenant_id` [String] (Maps to Firebase UID).
- `email` [String]
- `role` [String] (`admin` or `super_admin`).
- `approval_status` [String] (`pending` or `approved`).
- `beta_expiry` [Timestamp] (Absolute closure of system access).
- `is_active` [Boolean] (L0 Governance kill-switch).
- `wallet.allocated_credits` [Int] (Max allowed leads/actions).
- `wallet.consumed_credits` [Int] (Current burn).
- `wa_token` [String] (Symmetrically Encrypted Fernet CIPHER TEXT of the Meta WhatsApp API token).
- `wa_phone_id` [String]
- `admin_phone` [String]

### `campaigns`
The core matrices for defining target audiences natively.
- `id` [String] (Auto-generated).
- `tenant_id` [String]
- `status` [String] (`active`, `paused`).
- `bio` [String] (Natural language description of the B2B SaaS).
- `keywords` [String] (Comma-separated seed queries).
- `location` [String] (City / State).
- `gl` [String] (Country code, e.g., 'us', 'in').

### `leads`
The AI-extracted targets.
- `id` [String] (Deterministic hash: SHA256(tenant_id + campaign_id + target_url) to prevent duplication).
- `tenant_id` [String]
- `campaign_id` [String]
- `url` [String]
- `status` [String] (`processing`, `new`, `contacted`, `converted`, `ignored`).
- `score` [Int] (1-10 Vertex AI confidence index).
- `pain_point` [String] (The specific business problem extracted).
- `dm` [String] (The pre-drafted 2-sentence conversational outreach).
- `email` [String]
- `phone` [String]
- `linkedin` [String]

### `scraped_cache`
Cost-reduction mechanism preventing duplicate HTTP scraping across campaigns.
- `id` [String] (URL sanitized: slashes replaced by underscores).
- `url` [String]
- `text` [String] (Truncated text DOM strictly capped at 100KB).

### `usage_metrics`
Analytical tracking mapping specifically to `tenant_id`.
- `id` [String] (Maps to `tenant_id`).
- `gemini_calls` [Int] (Incremented dynamically).
- `serper_searches` [Int]

---

## 4. The Orchestrator Gateway (API & Core Node)

The Orchestrator (`services/orchestrator/main.py`) is written in Python/Flask and serves as the primary gateway.

### Authentication Engine (`authenticate_request`)
All inbound REST API calls must carry an `Authorization: Bearer <token>` header. The Orchestrator intercepts this, runs `auth.verify_id_token()`, and cross-references the Firebase `users` database. 
- If the document does not exist, it automatically creates a JIT (Just-In-Time) registration profile marking the user as `pending`.
- If the user's `role` is not `super_admin` and `is_active` is `False`, the system violently rejects the query.

### REST Endpoints
*   `GET /api/me`: Returns the user document and deserializes the wallet.
*   `GET /api/campaigns` & `GET /api/leads`: Strictly sandboxed data retrieval querying `tenant_id`.
*   `POST/PUT /api/campaigns` & `PUT /api/leads`: Mutation endpoints. Lead updates securely append data to an `interactions` array natively.
*   `POST /api/settings`: Accepts BYOT tokens from the user. It natively passes the raw token through a symmetric `cryptography.fernet` encryption suite using a global `.env` rotation key before saving to Firestore.

### Background Task Dispatcher (`trigger_daily_sweep`)
This routine acts as the heartbeat of the entire app. It can be triggered manually or via Cloud Scheduler.
1. It loops through all `active` campaigns.
2. It explicitly calculates the `wallet` budget for that specific `tenant_id`.
3. If valid, it constructs an HTTP POST payload containing `{campaign_id, tenant_id}`.
4. It attaches the Orchestrator's internal Service Account email as an OIDC identity token for Zero-Trust internal authorization.
5. It pushes the payload to **Google Cloud Tasks**, handing off execution safely.

### L0 Governance Layer (`/api/l0/...`)
Accessible strictly to `super_admin`. Allows total infrastructural manipulation:
- `POST /approve`: Upgrades user access, sets an absolute beta expiration date, and mints standard credits cleanly.
- `POST /mint`: Incremental dot-notation injection using `firestore.Increment()` securely to avoid document overrides.
- `POST /suspend`: Instantly detaches user capabilities globally.
- `GET /trends`: Real-time map-reduce aggregations grouping campaigns by geometric location and intelligent "Domains" (Medical, Finance, Software, etc.) based on substring mapping.

---

## 5. The Intelligence Pipeline Worker

The Pipeline (`services/pipeline-main/main.py`) executes the core lead generation algorithm. It is triggered by Cloud Tasks and operates independently.

### The Algorithmic Flow
1. **Dynamic Smart Query Generation (`generate_smart_query`)**:
   - The worker dynamically fetches previously `converted` or `contacted` leads belonging to the tenant.
   - It sends the previously successful `pain_points` to Gemini to extrapolate structural business trends.
   - It appends these trends via boolean logic (`AND ("trend" OR "trend")`) to the user's base keywords, whilst attaching a strict exclusion `-blacklist` (e.g. Wikipedia, Jobs, Amazon).

2. **Google Serper Execution**:
   - Queries `google.serper.dev/search`. Increments `usage_metrics.serper_searches`.

3. **Ruthless Post-Flight Noise Filter (`filter_serper_noise`)**:
   - Strips enterprise directories (g2, capterra, zoominfo).
   - Strips utility pages (`/login`, `/legal`, `forgot password`).

4. **LLM Pre-Filter (`pre_filter_gemini`)**:
   - Takes the raw Google Snippets and compares them against the user's `bio` using Vertex AI.
   - Discards irrelevant businesses *before* loading full pages. Returns a clean array of raw HTTP strings.

5. **Atomic Lead Lock**:
   - The system utilizes a SHA256 string `tenant_id_campaign_id_url` as the absolute Firestore ID.
   - Uses `doc_ref.create()`. If this throws `AlreadyExists`, the loop strictly caught a duplicate run and gracefully safely skips the rest of the execution.

6. **Web Scraping & WAF Evasion (`scrape_url`)**:
   - Natively checks for Web Application Firewall (WAF) fingerprints (Cloudflare, Incapsula).
   - Checks if the extracted string length is suspiciously low (JS frameworks).
   - If trapped, it gracefully reroutes the scrape to `SCRAPER_HEAVY_URL` (a secondary isolated container operating headless Chromium).
   - The output is ruthlessly sliced to 100,000 characters (`safe_truncate`) to ensure no payload will ever crash Firestore's 1MB hardware limit. Cache hit mapping is verified prior.

7. **Final AI Extraction (`final_score_and_dm`)**:
   - Gemini receives the truncated DOM and strictly extracts JSON identifying: Name, Pain Point, Email, Phone, LinkedIn, and assigns a Score (0-10).
   - It uniquely constructs a highly personalized two-sentence WhatsApp DM using psychological B2B hooks based strictly on the user's `bio`.

8. **Meta Push Notification (The V6 Hook)**:
   - If `score >= 8` (High Value Pipeline Lead), the system decodes the `wa_token` via Fernet interpolation.
   - It automatically transmits a Meta API push directly to the admin's phone natively injecting Interactive UI Buttons ("✅ Approve & Send", "🚫 Ignore").

---

## 6. Frontend Architecture (PWA)

Located in `public/`. Follows raw Vanilla JS standards for ultimate performance and zero-dependency bloat.

### Caching and Desync Safety
The Application strictly leverages a Service Worker (`sw.js`) utilizing a **Network-First** strategy.
To counteract browsers caching internal memory states (specifically for the `/api/me` auth route), `fetch()` calls inject aggressive timestamp rotation payloads (`?rt=17...`) into the target URLs. This mathematically invalidates browser-level HTTP caching while averting Flask CORS-Preflight rejections (which occur if `Cache-Control` is pushed).

### Layout Management
The Dashboard initializes via `loadDashboard()`.
- Uses generic hash routing (`#`) natively hooked to `switchTab()` methods to toggle CSS `.hidden` classes.
- Validates the active user's permissions, dynamically unlocking the L0 global tab specifically if `data.role === super_admin`.

### State Protection
A global `activeWallet` object holds synchronous memory variables correlating to the user's credit state. This guarantees real-time DOM hydration preventing users from interacting with UI modules if `(allocated - consumed) <= 0`.

---

## 7. Security Context & Future Extensions

*   **Zero-Trust:** Pipeline execution rejects any request strictly without verified Service Account origin identity validation.
*   **Cost Control:** A `limit(30)` boundary actively governs LLM executions inside the Pipeline. The Pipeline explicitly consumes tokens natively back to the Orchestrator via `firestore.Increment()` preventing over-execution.
*   **DPDP Erasure:** `/purge` completely and entirely eradicates all tracing variables natively attached to a specific user for data compliance.

**End of Document.**
