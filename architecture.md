# Lead Sniper – Comprehensive Architecture Blueprint (Version 23.0)
**(Master System Definition: Complete End-To-End Enterprise Track)**

## 1. System Vision & Objective
Lead Sniper is an enterprise-grade, high-margin SaaS application engineered to autonomously prospect and deliver strictly high-quality, contact-ready B2B leads for SMEs. The application bypasses traditional Google/Meta ad spending entirely. Instead, it leverages real-time Serper searches driven by predictive AI query logic, scrapes the deep web, analyzes company contexts via Vertex AI Gemini models, and routes the highest-performing targets directly into the MSME owner's WhatsApp feed for single-click CRM approvals.

The defining architectural constraint is its **"Zero-Trust Thin-Client"** model. The frontend contains zero direct database writing privileges.

---

## 2. Global Topological Breakdown
The ecosystem is heavily decoupled into autonomous serverless microservices orchestrated natively within Google Cloud Platform (GCP).

### 2.1 The Progressive Web App Frontend (`public/`)
*   **Hosting:** Firebase Hosting Content Delivery Network (CDN).
*   **Framework:** 100% Vanilla JS Single Page Application (SPA). Operates entirely devoid of React/Vue constraints drastically increasing cold-start execution speeds. 
*   **Design Paradigm:** The UI employs a highly modular "Next-Gen SaaS Glassmorphism" system tracking `backdrop-filter` limits with subtle `cubic-bezier` shadows globally replacing heavy CSS gradients.
*   **PWA Mobility:** Utilizes `manifest.json` and strict `sw.js` (Service Worker) tracking, decoupling mobile operations perfectly, maintaining offline state explicitly.
*   **Zero-Latency Array Filtering:** Immediate `rawLeadsCache.splice()` rendering mapping real-time mutations aggressively bouncing `Ignore` actions instantly masking DB fetch logic locally.
*   **Network Security:** All SDK logic (Firebase Web) natively operates solely for Auth. API fetches explicitly map via generic HTTP payloads targeting `/api/*`.

### 2.2 The Orchestrator Gateway (`services/orchestrator`)
*   **Role:** The central router. Operates as a proxy gateway (256MB RAM) dynamically intercepting `Authorization: Bearer <token>` passing through the frontend.
*   **CORS Hardening:** Operates explicitly without standard Flask extensions forcefully injecting `headers.append()` overrides dropping CORS constraints safely.
*   **Identity Anchors:** Decodes the UID organically mapping the immutable `tenant_id` ensuring a bad actor simply cannot inject records into foreign organizations.

---

## 3. The Backend Microservice Ecosystem

### 3.1 Eventarc Authentication Manager (`services/auth-trigger`)
*   **Function:** An autonomous Cloud Run execution module dynamically bound to Google Cloud Eventarc triggers. When a new SME logs in via Google/Email, it structurally executes tracking operations seamlessly generating an explicitly safe `tenant_id` matrix inside the DB.

### 3.2 Lead Mining Pipeline (`services/pipeline-main`)
*   **Execution Trigger:** Initiated strictly via **Cloud Tasks** (`lead-pipeline-queue`) stagger queues. Rate limits (`max_concurrent_dispatches = 5`) structurally prevent hitting external API bottlenecks rapidly (Stampede Mitigation).
*   **The Smart B.D. Query Engine:** Initiates searches by executing a DB read of local `contacted`/`converted` histories attached strictly to the `tenant_id`. Gemini extracts exact context limits dynamically modifying the search query with `AND (keyword)` arrays. 
*   **Post-Flight Noise Filter:** Executes Python substring match filtering natively scanning the Serper API output explicitly bounding `/legal`, `capterra.com`, or dead snippet responses (`"Sign in"`).
*   **Zero-Contact Score Penalties:** Hardcoded prompt bindings unequivocally coerce Gemini paths evaluating to a flat `0` score if it extracts garbage devoid of explicit human identifiers.

### 3.3 The Heavy Scraper Fallback (`services/scraper-heavy`)
*   **Function:** Independent Cloud Run service (2GB with Playwright). 
*   **Cost Control Constraint:** `min-instances: 0` ensures this expensive headless Chromium instance natively turns entirely off protecting Google Cloud billing bounds seamlessly targeting WAF blocks heavily.

### 3.4 WhatsApp Bidirectional Router (`services/whatsapp-webhook`)
*   **Outbound Trigger (pipeline-main):** If an extracted score hits `>= 8`, execution fetches User BYOT parameters explicitly executing `httpx.post()` targeting Meta Graph API pulling an Interactive JSON array cleanly.
*   **Inbound Autonomy:** Iterates inbound payloads explicitly executing a direct decouple `firestore.update({"status": "approved"})` pulling `action` strings natively generating an automated confirmation back into WhatsApp implicitly executing CRM management outside the UI.

### 3.5 Daily Email Digest (`services/email-summary`)
*   **Trigger Protocol:** Activated by Google **Cloud Scheduler**, executing a daily chron triggering exactly at the designated window seamlessly aggregating lead states into clean Text arrays hitting SendGrid's HTTP API (Bypassing Serverless port blocks securely).

---

## 4. Infrastructure & Automation

### 4.1 Infrastructure as Code (Terraform)
*   **Path:** `terraform/main.tf`
*   **Operations:** Automates GCP Secret Manager, PubSub loops, Cloud Tasks, and dedicated Service Accounts. Uses `roles/datastore.user` to cleanly bypass `firestore.rules`.
*   **Alerting Policy:** Generates a Google Monitoring alert explicitly identifying Cloud Run `5xx Error` limits over 5 minutes globally.

### 4.2 CI/CD Deployment Flow
Administrators manually map 6 native GUI Cloud Build Triggers in the GCP console tracking `Push to Branch: main`.
1. **Lead Pipeline Trigger**: `services/pipeline-main`
2. **Orchestrator Trigger**: `services/orchestrator`
3. **Heavy Scraper Trigger**: `services/scraper-heavy`
4. **Webhook Listener Trigger**: `services/whatsapp-webhook`
5. **Email Worker Trigger**: `services/email-summary`
6. **Firebase Portal Trigger**: Employs an exact `$FIREBASE_SA_KEY` Secret Manager mapping running `firebase deploy --only hosting`.

---

## 5. Test Cases & Verification Matrix

### 5.1. Concurrency Control (Stampede Mitigation) [✅ PASSED]
- **Objective:** Verify Vertex AI/Serper.dev quotas are respected.
- **Expectation Check:** The Cloud Tasks dashboard successfully staggered all queued items effectively limiting execution safely globally.

### 5.2. Tenant Data Cross-Contamination (Identity API Verification) [✅ PASSED]
- **Objective:** Verify MSME Teams are strictly bound to their Tenant ID via restrictions.
- **Test:** Authenticate as User A. Attempt to pass a mutated `POST /api/campaigns` requesting creation inside Tenant B's UUID.
- **Expectation Check:** API safely executed a backend dict `pop()`, forcefully overriding the payload with User A's legitimately queried footprint.

### 5.3. Thin-Client Refusal (Security Hardening) [✅ PASSED]
- **Objective:** Assert Legacy Client-Side Javascript cannot poll the DB.
- **Test:** Execute a raw `db.collection('leads').get()` from DevTools.
- **Expectation Check:** Payload drops mathematically responding `403 Permission Denied` correctly enforcing zero-trust logic perfectly globally.
