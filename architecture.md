# Lead Sniper – Enterprise Technical Architecture (Version 20.0)
**(Final Source of Truth – V4 Smart BD Agent Edition)**

## 1. Purpose & Business Context

Lead Sniper is a high-margin SaaS tool that helps SMEs generate ~20 high-quality, contact-ready leads per day per campaign using public search signals, reducing reliance on expensive Meta/Google ads. 

The SME owner (Admin) configures campaigns with product bio, location, and keywords. The system runs a smart multi-keyword funnel, powered by predictive historical AI mining, and delivers scored, contact-ready leads with ready-to-paste or auto-send WhatsApp/LinkedIn DMs.

---

## 2. High-Level System Architecture (Thin-Client Serverless SaaS)

The application operates fundamentally on a pure, decoupled **Thin-Client API Gateway** structure. All direct frontend-to-database real-time polling has been fully eradicated mapping true Zero-Trust enterprise constraints.

### Core Services

1. **Authentication:** Firebase Authentication (Google/Email).
2. **Frontend/Dashboard:** Firebase Hosting running a high-fidelity **Vanilla JS SPA**. Operates as a Progressive Web App (PWA) with native mobile caching.
3. **Database:** Firestore (Absolute Lockdown via `match /{document=**} { allow read, write: if false; }`). All I/O occurs natively via the Python Admin SDK natively authorized by the backend.
4. **Orchestrator / API Gateway:** Cloud Run service (256MB) natively exposing REST endpoints (`/api/campaigns`, `/api/leads`) with hardcoded pre-flight CORS origin protections.
5. **Main Pipeline (`lead-pipeline-main`):** Primary Cloud Run service extracting context via Serper and Vertex AI. **Now augmented with the Smart B.D. Query Engine.**
6. **Heavy Scraper Fallback (`scraper-heavy`):** Independent Cloud Run service (2GB with Playwright; min-instances: 0).
7. **WhatsApp Webhook (`whatsapp-webhook`):** Independent lightweight Cloud Run service (128MB).
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
- **Zero-Contact Score Penalties:** Hardcoded prompt bindings unequivocally coerce Gemini execution paths evaluating to a flat `0` score if it physically extracts marketing garbage devoid of explicit human nomenclature identifiers/contact fields.

### 3.4. Next-Gen Progressive Web App (Frontend `public/`)
- **SaaS Glassmorphism UX:** Overhauled with dynamic `backdrop-filter` limits tracking absolute predictive hover shadows matching enterprise SaaS aesthetic matrices natively globally replacing heavy CSS gradients natively.
- **Service Worker Mobility:** Installed strict Offline Caching logic decoupling mobile responsiveness dynamically.
- **Zero-Latency Array Filtering:** Immediate `rawLeadsCache.splice()` rendering mapping real-time mutations aggressively bouncing `Ignore` actions instantly masking DB fetch logic locally.
- **Geo-Location IP Targeting:** Executes a generic fetch grabbing location parameters intelligently wrapping Country limits natively simulating dropdown arrays dynamically gracefully ensuring accurate query parameters locally natively.

---

## 4. Security, Monitoring & Backups
- **No Thick-Client I/O:** `firestore.rules` formally blocks all operations. The database is invisible to the internet.
- **Data Perimeter Isolation:** The Orchestrator's internal Identity logic intercepts all payload operations forcefully mapping `users` matrices organically securing tenant logic natively.

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
