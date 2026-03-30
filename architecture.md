# Lead Sniper – Enterprise Technical Architecture Document Version: 16.0
**(Final Source of Truth – March 2026)**

## 1. Purpose & Business Context

Lead Sniper is a high-margin SaaS tool that helps Indian MSMEs generate ~20 high-quality, contact-ready leads per day per campaign using public search signals, reducing reliance on expensive Meta/Google ads. 

The SME owner (Admin) configures campaigns with product bio, location, and keywords. The system runs a smart multi-keyword funnel and delivers scored, contact-ready leads with ready-to-paste or auto-send WhatsApp/LinkedIn DMs. Team members can be added by the Admin to handle follow-up.

---

## 2. High-Level System Architecture (100% GCP-native Serverless SaaS)

The application has been restructured from a monolithic Python polling service to a highly concurrent, isolated microservice framework to solve concurrency stampedes, memory bloat, and Firestore document constraints.

### Core Services

1. **Authentication:** Firebase Authentication (Google/Email)
2. **Frontend/Dashboard:** Firebase Hosting (mobile-first vanilla JS + Glassmorphism UI)
3. **Database:** Firestore (per-tenant with granular Row-Level Security)
4. **Orchestration:** Cloud Scheduler → Google Cloud Tasks (`max_concurrent_dispatches = 5` staggered drip 6:00-7:00 AM)
5. **Main Pipeline (`lead-pipeline-main`):** Tiny Cloud Run service (256MB)
6. **Heavy Scraper Fallback (`scraper-heavy`):** Independent Cloud Run service (2GB with Playwright; strictly configured to scale to zero immediately idle `min-instances: 0`)
7. **WhatsApp Webhook (`whatsapp-webhook`):** Independent lightweight Cloud Run service (128MB)
8. **Daily Digest (`email-summary`):** Automated delivery of Top Leads via Gmail API.
9. **Secrets:** Google Secret Manager (with 90-day auto-rotation policies)
10. **LLM:** Gemini 2.5 Flash on Vertex AI (No Context Caching)
11. **Search Engine API:** Serper.dev

---

## 3. Microservices & Dependencies Breakdown

### 3.1. Infrastructure as Code (Terraform)
- **Path:** `terraform/main.tf`, `terraform/variables.tf`
- **Purpose:** Automatically provisions Google Cloud Run APIs, Cloud Tasks APIs, Secret Manager entries, and sets the base deployment scaffolding.
- **Queue Limits & Staggering:** `lead-pipeline-queue` enforces `max_dispatches_per_second = 1` and `max_concurrent_dispatches = 5`. With 500 active users, a 06:00:00 AM Cloud Scheduler blast will be artificially staggered by the queue across the entirety of the 6:00 - 7:00 AM hour, absolutely preventing any 06:00:00 API stampede bounds hitting Vertex AI or Serper limits.

### 3.2. Orchestrator Cloud Function (`services/orchestrator`)
- **Type:** HTTP Triggered Cloud Function (Memory: 256MB).
- **Trigger:** Cloud Scheduler (Cron `0 6 * * *` IST).
- **Purpose:** Wakes up every day at 6:00 AM IST, queries Firestore for all campaigns marked `"status": "active"`, and spawns specific requests directly into the Google Cloud Tasks `lead-pipeline-queue`.
- **Dependencies:**
  - `google-cloud-firestore==2.14.0`
  - `google-cloud-tasks==2.15.0`
  - `Flask==3.0.0`

### 3.3. Pipeline Core (`services/pipeline-main`)
- **Type:** Cloud Run App (Memory: 256MB).
- **Purpose:** Executes the multi-keyword intelligence sweep.
- **Workflow:**
  1. *Sweep:* Queries Serper.dev for top 20 organic results per keyword. Deduplicates by URL.
  2. *Pre-Filter:* Sends the JSON snippt array + user Product Bio to Gemini 2.5 Flash to prune out low-signal domains and returns a strict array of the top 30 URLs.
  3. *Primary Scrape:* Attempts text extraction using `httpx + BeautifulSoup4`. If text volume indicates a blank React/Vue/JS-App DOM, it triggers a fallback sequence calling `scraper-heavy`.
  4. *Cache Control:* Applies a hard truncation (`text[:100000]`) ensuring no website pushes Firestore beyond its 1MB Document limit. Saves mapping to `/scraped_cache/{url}`.
  5. *LLM Final Scoring & DM Synthesis:* Reads the resulting extracted text. Generates a conversational 2-sentence DM specific to WhatsApp/LinkedIn parameters alongside an intent score.
  6. *Storage:* Writes passing leads to `/tenants/{tenant_id}/leads`.
- **Dependencies:**
  - `google-cloud-firestore==2.14.0`
  - `google-cloud-secret-manager==2.16.2`
  - `google-cloud-aiplatform==1.38.1`
  - `Flask==3.0.0`, `gunicorn==21.2.0`, `httpx==0.26.0`, `beautifulsoup4==4.12.3`, `grpcio`

### 3.4. Heavy Scraper (`services/scraper-heavy`)
- **Type:** Independent Cloud Run App (Memory: 2GB, `min-instances: 0`).
- **Purpose:** Acts entirely as a failover network for the primary pipeline. Instantiates a headless Chromium instance to allow fully executed DOM states to render before grabbing static HTML and returning it to the pipeline. Scales efficiently to zero immediately after payload delivery returning memory capacity pool cleanly.
- **Base Image:** `mcr.microsoft.com/playwright/python:v1.42.0-jammy`
- **Dependencies:**
  - `playwright==1.42.0`
  - `Flask==3.0.0`, `gunicorn==21.2.0`

### 3.5. WhatsApp Webhook (`services/whatsapp-webhook`)
- **Type:** Cloud Run App (Memory: 128MB).
- **Purpose:** Listens strictly for Meta API GET (verification) and POST (status update) event webhooks. Connects directly to Firestore to update UI tracked states (`delivered`, `read`, `replied`). Applies strict `X-Hub-Signature-256` HMAC validation via Secret Manager against payload signatures to prevent bad actors from spoofing read receipts.
- **Dependencies:**
  - `google-cloud-firestore==2.14.0`
  - `google-cloud-secret-manager==2.16.2`
  - `Flask==3.0.0`, `gunicorn==21.2.0`, `grpcio`

### 3.6. Daily Email Summary (`services/email-summary`)
- **Type:** Cloud Run App Triggered via Scheduler (Memory: 128MB).
- **Purpose:** Compiles a digest of top scored leads for each Tenant and blasts daily HTML emails using standard Gmail SMTP / App Passwords via Secret Manager mapping.

### 3.7. Frontend Application (`public/`)
- **Deployment:** Firebase Hosting.
- **Rules:** Strict Row-Level Security ensuring Admins/Team Members can only query their own Firebase Auth `{tenantId}` datasets.
- **Core Functionality:**
  - UI 5-Button Event Hooks: Directly mutating the Outcome Trackers (`[Complete]`, `[Ignore]`, `[Converted]`, `[No Response]`, `[Follow-up Later]`).
  - New Campaigns: Enables tracking through explicitly triggering the Smart Competitor Monitor keywords.
  - Return On Investment: Emits active UI metrics indicating monthly saved dollars vs Ad Cost and dynamically generates PDF reports.
- **Theme:** Vibrant, dynamic dark-mode glassmorphism utilizing `Outfit` fonts.

---

## 4. Security, Monitoring & Backups (IaC Enforcement)
- **Log Metrics Policy:** Cloud Run metrics trigger native Cloud Alerts if `5xx` rate boundaries are breached across a 5 minute rolling window ensuring developers know about scraping failures instantly.
- **Secret Rotations:** `rotations { }` enforces 90 day auto-reset cycling of API keys.
- **Recovery Strategy:** Firestore natively dumps data to standard multi-regional Cloud Storage Buckets utilizing 30-day lifecycle policies.

---

## 4. Test Cases & Verification Matrix

### 4.1. Concurrency Control (Stampede Mitigation) [✅ PASSED]
- **Objective:** Verify Vertex AI/Serper.dev quotas are respected.
- **Test:** Generated 500 active mock campaigns in Firestore. Triggered the `Orchestrator` manually.
- **Expectation Check:** The Cloud Tasks dashboard successfully staggered all 500 queued items over ~45 minutes. The dispatch rate never exceeded `max_concurrent_dispatches = 5`, eliminating any timestamp stampedes against Gemini APIs.

### 4.2. Scraper Fallback Architecture (Playwright Memory Trap Isolation) [✅ PASSED]
- **Objective:** Ensure single-page apps (SPAs) don't crash the fast-pipeline, and Playwright triggers successfully, then scales to zero.
- **Test:** Passed a heavy React SPA bundle to `/dispatch`.
- **Expectation Check:** The `lead-pipeline-main` executed a `Timeout/EmptyDOM` capture and smoothly re-routed to `scraper-heavy`. Chromium isolated execution accurately rendered content.

### 4.3. Firestore 1MB Write Limitation (Cache Fix) [✅ PASSED]
- **Objective:** Prevent backend crashes when caching large corporate domains.
- **Test:** Pointed pipeline at a massive Wiki page (> 2MB HTML DOM).
- **Expectation Check:** `scraped_cache` string extraction successfully hit `truncate_text()` limiter capping at `100,000` bytes inside Firestore native UI.

### 4.4. Tenant Data Cross-Contamination (RLS Verification) [✅ PASSED]
- **Objective:** Verify MSME Teams are strictly bound to their Tenant ID.
- **Test:** Authenticate as User A (Tenant A). Attempt to query `/tenants/{Tenant_B}/leads`.
- **Expectation Check:** API safely failed out producing `FirebaseError: Missing or insufficient permissions.`.

### 4.5. WhatsApp Signature Exchange [✅ PASSED]
- **Objective:** Assert Meta Webhooks can't be spoofed.
- **Test:** Sent a cURL POST to `/webhook` attempting a message `Status=Read` impersonation.
- **Expectation Check:** The pipeline executed a `HTTP 403 Forbidden` exit safely dropping the forged hash via the strict HMAC digest matching.

---

## 5. Deployment Map (Native GCP CI/CD)

The application utilizes a strict enterprise security boundary where GitHub acts **exclusively** as a Source Control Management (SCM) repository. All compilation, secret binding, and deployment are orchestrated identically to enterprise blueprints natively within **Google Cloud Build Triggers**. No deployment tokens or long-lived authentication keys are housed in GitHub.

### 5.1. Google Cloud Build Native Triggers Configured
Administrators manually map 6 native GUI Cloud Build Triggers in the GCP console tracking `Push to Branch: main`.

1. **Lead Pipeline Trigger**: Uses `services/pipeline-main/cloudbuild.yaml` (Filter match: `services/pipeline-main/**`).
2. **Orchestrator Trigger**: Uses `services/orchestrator/cloudbuild.yaml` (Filter match: `services/orchestrator/**`).
3. **Heavy Scraper Trigger**: Uses `services/scraper-heavy/cloudbuild.yaml` (Filter match: `services/scraper-heavy/**`).
4. **Webhook Listener Trigger**: Uses `services/whatsapp-webhook/cloudbuild.yaml` (Filter match: `services/whatsapp-webhook/**`).
5. **Email Worker Trigger**: Uses `services/email-summary/cloudbuild.yaml` (Filter match: `services/email-summary/**`).
6. **Firebase Portal Trigger**: Uses the root `./cloudbuild.yaml` (Filter match: `public/**` & `cloudbuild.yaml` & `firestore.rules`), passing `_FIREBASE_TOKEN` explicitly via Cloud Secrets natively integrated into the GCP Trigger UI.
