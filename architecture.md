# Lead Sniper – Comprehensive Architecture Blueprint (Version 22.0)
**(Master System Definition: V6 Whatsapp + Zero-Trust Engine Protocol)**

## 1. System Vision & Objective
Lead Sniper is an enterprise-grade, high-margin SaaS application engineered to autonomously prospect and deliver strictly high-quality, contact-ready B2B leads for SMEs. The application bypasses traditional Google/Meta ad spending entirely. Instead, it leverages real-time Serper searches driven by predictive AI query logic, scrapes the deep web, analyzes company contexts via Vertex AI Gemini models, and routes the highest-performing targets directly into the MSME owner's WhatsApp feed for single-click CRM approvals.

The defining architectural constraint is its **"Zero-Trust Thin-Client"** model. The frontend contains zero direct database writing privileges; all structural reads and writes pass through secured Python API Gateways validating JWT signatures and parsing strict `tenant_id` boundaries. 

---

## 2. Global Topological Breakdown

The ecosystem is heavily decoupled into autonomous serverless microservices orchestrated natively within Google Cloud Platform (GCP).

### 2.1 The Progressive Web App Frontend (`public/`)
*   **Hosting:** Firebase Hosting Content Delivery Network (CDN).
*   **Framework:** 100% Vanilla JS Single Page Application (SPA). Operates entirely devoid of React/Vue constraints drastically increasing cold-start execution speeds. 
*   **Design Paradigm:** The UI employs a highly modular "Next-Gen SaaS Glassmorphism" system. Heavy use of native `backdrop-filter` limits with subtle `cubic-bezier` box-shadows replace outdated Bootstrap flat designs. 
*   **PWA Mobility:** Utilizes `manifest.json` and strict `sw.js` (Service Worker) tracking, decoupling mobile operations perfectly, maintaining offline state caches logically.
*   **Real-Time State Mapping:** DOM execution bounds replicate React `useEffect` and `useState` natively via O(1) Javascript array modifications (e.g., executing `splice()` natively to dismiss "Ignored" targets globally without initiating network loading screens).
*   **Network Security:** All SDK logic (Firebase Web) natively operates solely for Auth. API fetches explicitly map via generic HTTP payloads targeting `/api/*`.

### 2.2 The Orchestrator Gateway (`services/orchestrator`)
*   **Role:** The central router. Operates as a proxy gateway (256MB RAM) for all frontend communications dynamically intercepting `authorization` strings tracking JWT endpoints.
*   **CORS Hardening:** Exposes explicit, strict Python `headers.append()` overrides dropping Flask-CORS dependencies to violently enforce allowed origins explicitly matching the Firebase Hosting domains globally.
*   **Identity Anchors:** Intercepts UID blocks matching standard Google login logic natively rewriting the `users` array fetching the immutable `tenant_id` ensuring a bad actor simply cannot inject records into foreign organizations.

---

## 3. The Lead Mining Pipeline Workflow

The core heavy lifting occurs within asynchronous task limits triggered by the gateway cleanly mapping a multi-service extraction architecture.

### Phase A: The Smart B.D. Query Engine (`pipeline-main`)
*   **Execution Trigger:** Initiated strictly via Cloud Tasks stagger queues structurally to prevent hitting external API bottlenecks rapidly.
*   **Historical Query Extraction:** Before executing searches, the engine maps a local `firestore` database execution dynamically sniffing out prior high-performing leads (`status in ['contacted', 'converted']`).
*   **LLM Dork Engineering:** Extracts prior `pain_point` extractions securely routing them cleanly into Gemini to spit back 3 pure B2B search keyword constructs natively bound into generic query parameters mapping Google OR syntax `(A OR B OR C)`.
*   **Zero-Noise Filtering:** Appends universal Dork rules explicitly tracking blacklists bounding elements dynamically (`-wiki -careers -"login"`).

### Phase B: Search & Ruthless Scrape Filtering
*   **Execute Serper:** Runs the payload via Serper API natively extracting raw JSON outputs globally.
*   **Post-Flight Triage:** Triggers `filter_serper_noise()` explicitly. Discards massive aggregators intelligently mapping string matches aggressively bounding paths `/legal` or `.g2.com` keeping scraped volumes tiny.
*   **Scraping Cascade:** The survived array is hit lightly utilizing fast Python `httpx`/`BeautifulSoup`. If dynamic Javascript WAF models are detected (Cloudflare blocks natively dropping `"Just a moment"` payloads), it cleanly catches exceptions dynamically routing the URL globally into `services/scraper-heavy` (a 2GB Playwright Chromium instance mapping min-instances: 0 to conserve cash natively isolating execution constraints explicitly).

### Phase C: Intelligence Scoring (Zero-Trust Prompting)
*   The raw string is truncated to a precise 100KB boundary cleanly evading 1MB Firestore limitations preventing explicit pipeline stack overflows securely.
*   The array routes explicitly to Google's Vertex AI (`gemini-2.5-flash`).
*   **The Specific Human Target Rule:** The architecture runs an aggressive prompt logic tracking specific target identifiers safely generating a flat `0` penalty string explicitly denying entry natively to marketing landing pages organically. 
*   **Lock Deduplication:** Uses an atomic `hashlib.sha256` matching the URL + Campaign globally forcing Firestore to evaluate an `AlreadyExists` failure structurally bypassing race-condition loops natively.

---

## 4. Webhook CRM Routing & Interactivity

The system features autonomous bidirectional CRM workflows mapping pure logic without dashboard interventions.

### 4.1. Meta WhatsApp Webhooks (`services/whatsapp-webhook`)
*   **Outbound Trigger (`pipeline-main`):** The instant an LLM emits a score `>= 8`, the main pipeline formally queries the `users` collection to adopt a strictly isolated "Bring Your Own Token" (BYOT) `wa_token` limit securely. It executes a Graph API Interactive Button array routing the Company snippet natively into WhatsApp dynamically.
*   **Inbound Interception:** `whatsapp-webhook` natively processes Meta payloads iterating deep arrays strictly looking into `messages[0].interactive.button_reply.id`. 
*   **Atomic Syncing:** The button explicitly triggers an array `payload_str.split("_")` cleanly dropping the targeted `lead_id` seamlessly executing a deep `update({'status': 'approved'})` executing cleanly against the document boundary securely tracking responses back natively alerting the administrator safely locally cleanly.

---

## 5. Deployment, Automation, and CI/CD

Because the system handles BYOT targets securely executing external Webhooks logically mapping explicit Cloud limits organically, GitHub formally controls deployments cleanly tracking branches organically.

### 5.1 Project Infrastructure Map
1.  **Auth Webhook (`services/auth-trigger`):** Generates `tenant_id` blocks structurally triggering on new Google Login registrations natively dynamically.
2.  **Email Digest (`services/email-summary`):** Bounding SMTP protocols securely explicitly executing daily Cloud Scheduler sweeps intelligently tracking top target records correctly globally.
3.  **Firebase Settings:** Utilizing standard `firebase.json` constraints dropping the `hosting` CDN securely mapped explicitly with `source: "/api/**"` bounds explicitly tracking Cloud Run `orchestrator` pointers cleanly dynamically handling edge logic intelligently safely cleanly.
4.  **Cloud Build Triggers:** CI boundaries physically mapped internally safely natively dynamically replacing `local` deployments targeting automated `push` boundaries efficiently cleanly dynamically cleanly.
