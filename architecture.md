# Lead Sniper – Enterprise Technical Architecture Document Version: 17.0
**(Final Source of Truth – REST API Gateway Edition)**

## 1. Purpose & Business Context

Lead Sniper is a high-margin SaaS tool that helps SMEs generate ~20 high-quality, contact-ready leads per day per campaign using public search signals, reducing reliance on expensive Meta/Google ads. 

The SME owner (Admin) configures campaigns with product bio, location, and keywords. The system runs a smart multi-keyword funnel and delivers scored, contact-ready leads with ready-to-paste or auto-send WhatsApp/LinkedIn DMs.

---

## 2. High-Level System Architecture (Thin-Client Serverless SaaS)

The application has been radically hardened from a Firebase Thick-Client model to a pure, decoupled **Thin-Client API Gateway** structure. All direct frontend-to-database real-time polling has been physically annihilated to enforce true Zero-Trust enterprise constraints.

### Core Services

1. **Authentication:** Firebase Authentication (Google/Email).
2. **Frontend/Dashboard:** Firebase Hosting (mobile-first vanilla JS). Configured via `firebase.json` with a rewrite to proxy `/api/**` explicitly to the `orchestrator` service.
3. **Database:** Firestore (Absolute Lockdown via `match /{document=**} { allow read, write: if false; }`). All I/O occurs natively via the Python Admin SDK natively authorized by the backend.
4. **Orchestration / API Gateway (`orchestrator`):** Cloud Run service (256MB) natively exposing REST endpoints (`/api/campaigns`, `/api/leads`) and dispatching background Cloud Tasks syncs.
5. **Main Pipeline (`lead-pipeline-main`):** Tiny Cloud Run service (256MB) parsing SERP LLM evaluation.
6. **Heavy Scraper Fallback (`scraper-heavy`):** Independent Cloud Run service (2GB with Playwright; strictly configured to scale to zero immediately idle `min-instances: 0`).
7. **WhatsApp Webhook (`whatsapp-webhook`):** Independent lightweight Cloud Run service (128MB).
8. **Daily Digest (`email-summary`):** Automated delivery of Top Leads via Gmail API.
9. **Secrets:** Google Secret Manager.
10. **LLM:** Gemini 2.5 Flash on Vertex AI.
11. **Search Engine API:** Serper.dev.

---

## 3. Microservices & Dependencies Breakdown

### 3.1. Infrastructure as Code (Terraform)
- **Path:** `terraform/main.tf`, `terraform/variables.tf` (Requires HashiCorp google provider `~> 4.0`)
- **Purpose:** Automatically provisions Google Cloud Run APIs, Cloud Tasks APIs, Secret Manager entries, and dedicated IAM microservice boundaries. Custom Service Accounts natively use `roles/datastore.user` to bypass the `firestore.rules` lockdown.

### 3.2. Orchestrator API Gateway (`services/orchestrator`)
- **Type:** Cloud Run App wrapper (Memory: 256MB) exposed to `allUsers` to proxy frontend rewrites.
- **REST Paradigm:** Routes `GET`, `POST`, and `PUT` traffic passing through the Firebase Hosting frontend mapping.
- **Backend Identity Engine:** Intercepts `Authorization: Bearer <token>`, decodes the UID, and dynamically executes an anchor query against the `users` collection to mathematically extract or map the `tenant_id`. Brand new registrations seamlessly default to `role: admin`.
- **Legacy Trigger:** Wakes up via Cloud Scheduler at 6:00 AM IST to stagger async queue loops into `lead-pipeline-queue`.
- **Dependencies:**
  - `firebase-admin>=6.5.0`
  - `google-cloud-firestore==2.14.0`
  - `google-cloud-tasks==2.15.0`
  - `Flask==3.0.0`

### 3.3. Pipeline Core (`services/pipeline-main`)
- **Type:** Cloud Run App (Memory: 256MB).
- **Workflow:** Extrapolates SERP arrays -> Scrapes DOM -> Evaluates intent (Gemini) -> Synthesizes DM Hooks -> Logs payload strictly using the backend-affixed `tenant_id` context.

### 3.4. Heavy Scraper (`services/scraper-heavy`)
- **Type:** Independent Cloud Run App (Memory: 2GB, `min-instances: 0`). Headless Chromium instance executing heavy React/Vue DOM string parsing natively capping cache limits below 100kb payload constraints.

### 3.5. WhatsApp Webhook (`services/whatsapp-webhook`)
- **Type:** Cloud Run App (Memory: 128MB). Listens strictly for Meta API GET and POST event webhooks securely bypassing Firebase UI scopes.

### 3.6. Daily Email Summary (`services/email-summary`)
- **Type:** Cloud Run App Triggered via Scheduler (Memory: 128MB) natively firing Python SMTP templates to SME owners.

### 3.7. Frontend Application (`public/`)
- **Deployment:** Firebase Hosting. 
- **Networking:** Stripped of all `firebase-firestore` CDN SDKs. The frontend operates solely via `fetch()` API operations transmitting standard JWT structures.
- **Routing:** Configured through `firebase.json` triggering a rewrite of `/api/**` mappings to strictly proxy to the `orchestrator` service.
- **Core Functionality:** Decoupled HTML DOM mutating dynamically purely off backend-sanitized JSON arrays yielding secure multi-tenant execution matrices.

---

## 4. Security, Monitoring & Backups
- **No Thick-Client I/O:** `firestore.rules` formally blocks all operations. The database is invisible to the internet.
- **Data Perimeter Isolation:** The Orchestrator's internal Identity logic intercepts all `POST` payloads manually deleting and affixing securely generated `tenant_id` traits masking against potential structural UI payload injection attacks.
- **Log Metrics Policy:** Terraform enforces a Cloud Monitor alert catching any rate boundaries inside a 5-minute rolling window.

---

## 5. Test Cases & Verification Matrix

### 5.1. Concurrency Control (Stampede Mitigation) [✅ PASSED]
- **Objective:** Verify Vertex AI/Serper.dev quotas are respected.
- **Expectation Check:** The Cloud Tasks dashboard successfully staggered all 500 queued items effectively limiting `max_concurrent_dispatches = 5`.

### 5.2. Tenant Data Cross-Contamination (Identity API Verification) [✅ PASSED]
- **Objective:** Verify MSME Teams are strictly bound to their Tenant ID via REST logic.
- **Test:** Authenticate as User A. Attempt to pass a mutated `POST /api/campaigns` requesting creation inside Tenant B's UUID.
- **Expectation Check:** API safely executed a backend dict `pop()`, forcefully overriding the payload with User A's legitimately queried `users` tenant footprint.

### 5.3. Thin-Client Refusal (Security Hardening) [✅ PASSED]
- **Objective:** Assert Legacy Client-Side Javascript cannot poll the DB.
- **Test:** Execute a raw `db.collection('leads').get()` from DevTools.
- **Expectation Check:** Payload drops mathematically responding `403 Permission Denied` correctly enforcing lockdown boundaries.

---

## 6. Deployment Map (Native CI/CD)

The application utilizes a strict enterprise security boundary where GitHub acts **exclusively** as a Source Control Management (SCM) repository. 

### 6.1. Google Cloud Build Native Triggers Configured
Administrators manually map 6 native GUI Cloud Build Triggers in the GCP console tracking `Push to Branch: main`.

1. **Lead Pipeline Trigger**: Uses `services/pipeline-main` tracking 256Mi RAM limits.
2. **Orchestrator Trigger**: Uses `services/orchestrator` tracking 256Mi and `--allow-unauthenticated` for API Proxying.
3. **Heavy Scraper Trigger**: Uses `services/scraper-heavy` explicit 2Gi limits and `min-instances: 0` fast-shutoff configurations.
4. **Webhook Listener Trigger**: Uses `services/whatsapp-webhook` mapping explicit `--allow-unauthenticated` execution against 128Mi.
5. **Email Worker Trigger**: Uses `services/email-summary` deployed mapping to 128Mi.
6. **Firebase Portal Trigger**: Employs an exact `$FIREBASE_SA_KEY` Secret Manager mapping natively executing `npm install -g firebase-tools` inside `node:20` to trigger strict `hosting,firestore` execution updates flawlessly avoiding missing `functions` triggers.
