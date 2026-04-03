# Lead Sniper – Enterprise Technical Architecture (Version 21.0)
**(Final Source of Truth – V6 WhatsApp Interactive Edition)**

## 1. Purpose & Business Context

Lead Sniper is a high-margin SaaS tool that helps SMEs generate ~20 high-quality, contact-ready leads per day per campaign using public search signals, reducing reliance on expensive Meta/Google ads. 

The SME owner (Admin) configures campaigns with product bio, location, and keywords. The system runs a smart multi-keyword funnel, powered by predictive historical AI mining, and delivers scored, contact-ready leads. **With Version 6, the system now features autonomous bidirectional WhatsApp routing, delivering real-time interactive JSON prompts to SME Admins, allowing them to instantly execute CRM states without opening the dashboard.**

---

## 2. High-Level System Architecture (Thin-Client Serverless SaaS)

The application operates fundamentally on a pure, decoupled **Thin-Client API Gateway** structure. All direct frontend-to-database real-time polling has been fully eradicated mapping true Zero-Trust enterprise constraints.

### Core Services

1. **Authentication:** Firebase Authentication (Google/Email).
2. **Frontend/Dashboard:** Firebase Hosting running a high-fidelity **Vanilla JS SPA**. Operates as a Progressive Web App (PWA) with native mobile caching.
3. **Database:** Firestore (Absolute Lockdown via `match /{document=**} { allow read, write: if false; }`). All I/O occurs natively via the Python Admin SDK natively authorized by the backend.
4. **Orchestrator / API Gateway:** Cloud Run service (256MB) natively exposing REST endpoints (`/api/campaigns`, `/api/leads`) with hardcoded pre-flight CORS origin protections.
5. **Main Pipeline (`lead-pipeline-main`):** Primary Cloud Run service extracting context via Serper and Vertex AI. Extrapolates out bounds dynamically pushing high scores to Graph API.
6. **Heavy Scraper Fallback (`scraper-heavy`):** Independent Cloud Run service (2GB with Playwright; min-instances: 0).
7. **WhatsApp Webhook (`whatsapp-webhook`):** Cloud Run service (128MB) operating an autonomous listener decoding Graph API Webhooks explicitly tracking JSON `interactive.button_replies`.
8. **Daily Digest (`email-summary`):** Automated delivery of Top Leads via Gmail API.
9. **Secrets:** Google Secret Manager.
10. **LLM:** Gemini 2.5 Flash on Vertex AI.
11. **Search Engine API:** Serper.dev.

---

## 3. Microservices & Intelligence Breakdown

### 3.1. Infrastructure as Code (Terraform)
- **Path:** `terraform/main.tf`
- **Purpose:** Automatically provisions Google Cloud Run APIs, Cloud Tasks APIs, Secret Manager entries, and dedicated IAM microservice boundaries. Custom Service Accounts natively use `roles/datastore.user`.

### 3.2. Orchestrator API Gateway (`services/orchestrator`)
- **Type:** Proxy Gateway mapping frontend API fetches correctly filtering tokens natively discarding standard Flask extensions directly forcing `headers.append()` overrides. Returns `O(1)` JSON representations tracking zero-trust policies cleanly.

### 3.3. Pipeline Core & Smart B.D. Engine (`services/pipeline-main`)
- **Pre-Flight Query Generator:** Initiates operations dynamically tracking historical successful lead matrices attached strictly to the `tenant_id`. Maps exact extracted context words aggressively alongside generic Google Dorks (`-wiki -careers -amazon.com`) eliminating raw noise securely before execution.
- **Post-Flight Noise Filter:** Executes Python substring match filtering natively scanning the Serper API output JSON explicitly bounding `/legal`, `capterra.com`, or dead snippet responses (`"Sign in"`).
- **Outbound WhatsApp Trigger:** If an extracted score hits `>= 8`, execution halts and fetches User BYOT parameters explicitly executing `httpx.post()` targeting Meta Graph API endpoints natively pushing a custom Interactive Button template securely.

### 3.4. WhatsApp Bidirectional Router (`services/whatsapp-webhook`)
- **Parser Loop:** The system iterates deep over inbound Meta arrays safely verifying `messages[0]` payloads identifying Interactive `button_reply` boundaries cleanly tracking strings safely.
- **Autonomous CRM Mutation:** Successfully parses action strings natively running decoupled `firestore.update({"status": "approved"})` explicitly mutating target strings globally ensuring Zero Backend interactions.
- **Confirmation Loop:** Triggers an atomic fallback reply acknowledging targets safely explicitly utilizing tracking algorithms natively.

### 3.5. Next-Gen Progressive Web App (Frontend `public/`)
- **SaaS Glassmorphism UX:** Overhauled with dynamic `backdrop-filter` limits tracking absolute predictive hover shadows matching enterprise SaaS aesthetic matrices natively globally replacing heavy CSS gradients natively.
- **Service Worker Mobility:** Installed strict Offline Caching logic decoupling mobile responsiveness dynamically.
- **Zero-Latency Array Filtering:** Immediate `rawLeadsCache.splice()` rendering mapping real-time mutations aggressively bouncing `Ignore` actions instantly masking DB fetch logic locally.

---

## 4. Security, Monitoring & Backups
- **No Thick-Client I/O:** `firestore.rules` formally blocks all operations. The database is invisible to the internet.
- **Data Perimeter Isolation:** The Orchestrator's internal Identity logic intercepts all payload operations forcefully mapping `users` matrices organically securing tenant logic natively.
- **BYOT Strict Architecture:** Webhooks inherently block outbound structures parsing missing explicit Admin keys seamlessly enforcing execution bounds efficiently globally securely.

---

## 5. Deployment Map (Native CI/CD)

The application utilizes a strict enterprise security boundary where GitHub acts **exclusively** as a Source Control Management (SCM) repository cleanly pushing logic against Cloud Run constraints natively.

### 5.1. Google Cloud Build Native Triggers Configured
Administrators manually map 6 native GUI Cloud Build Triggers in the GCP console tracking `Push to Branch: main`.
1. **Lead Pipeline Trigger**: `services/pipeline-main`
2. **Orchestrator Trigger**: `services/orchestrator`
3. **Heavy Scraper Trigger**: `services/scraper-heavy` (2Gi max, 0 min-instance)
4. **Webhook Listener Trigger**: `services/whatsapp-webhook`
5. **Email Worker Trigger**: `services/email-summary`
6. **Firebase Portal Trigger**: Employs an exact `$FIREBASE_SA_KEY` Secret Manager mapping running `firebase deploy --only hosting` tracking purely frontend execution flawlessly tracking CDN bindings dynamically.
