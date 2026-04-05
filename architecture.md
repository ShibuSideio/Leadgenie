# Lead Sniper / Sideio Smart Growth (V12.99)
**Master Architecture & Source of Truth Blueprint**

---

## 1. SYSTEM OVERVIEW & INFRASTRUCTURE MAP

Lead Sniper exists as a highly decoupled, event-driven B2B intelligence pipeline. It relies entirely on a microservice architecture built across the Google Cloud ecosystem, prioritizing native LLM (Vertex AI) contextual logic and cost-efficient scaling mechanics.

### The Tech Stack
*   **Frontend UI:** React SPA, integrated with Firebase Client SDKs for real-time listener binding.
*   **Authentication & State:** Firebase Auth coupled natively with Firestore for persistence and RBAC state definition.
*   **The Brain (LLM):** Vertex AI (`gemini-2.5-flash`), centralized natively in the `us-central1` cluster avoiding legacy model execution gaps.
*   **Search Extraction Engine:** The proprietary Serper API.
*   **Background Orchestration:** Google Cloud Tasks.
*   **Execution Infrastructure:** Three distinct Google Cloud Run containers.

### Distributed Container Architecture
1.  **`orchestrator`**: The control plane. Exposes REST endpoints (`/api/leads`, `/api/l0/telemetry`), calculates wallet expenditures natively, registers RLHF feedback scores, and queues executions sequentially into Google Cloud Tasks.
2.  **`pipeline-main`**: The B2B nervous system. Processes asynchronous task loads, hits Serper API for URLs, executes lightweight HTML sweeps, invokes the generative models to classify/enrich prospects contextually, and guarantees deterministic DB extraction.
3.  **`scraper-heavy`**: The Playwright container. Actively invoked via HTTP fallback from `pipeline-main`. Utilizes Chromium to render React/Vue payloads and JS-heavy e-commerce routing with natively blocked media assets to secure operational memory.

---

## 2. FIRESTORE DATABASE SCHEMA

Data normalization enforces single-source-of-truth architectures and minimizes heavy read burns.

### `users` Collection (The Tenant Hub)
*   `email` (string)
*   `role` (string) - Defines `"super_admin"` routing for L0 analytics access.
*   `agreed_to_terms` (timestamp) - Tracks onboarding compliance logic.
*   `crm_webhook_url` (string) - Defines external Push integrations natively.
*   **Wallet Document Base:** `{"allocated_credits": int, "consumed_credits": int}`. 
    *   *Usage Check:* The orchestrator calculates strictly `allocated_credits - consumed_credits` on queuing.
*   **RLHF Matrix:** `preferences_weights: map`
    *   Example: `{"tech_wordpress": -3, "hiring_intent": 1}`. 

### `campaigns` Collection
*   `tenant_id` (string) - Parent logical mapping.
*   `name` (string) - Display value.
*   `target_location` (string) - Geographical anchor restricting search engine leakage.
*   `bio` (string) - Contextual product definition (e.g., *"We offer B2B cleaning"*).
*   `status` (string) - `active` or `paused`.

### `leads` Collection (The Execution Target)
*   `tenant_id` (string)
*   `url` (string)
*   `status` (string, Enum: `new`, `contacted`, `ignored`, `failed`, `processing`, `completed`)
*   `score` (number) - Extracted out of 10 natively via Gemini. 
*   `interactions` (array) - Timestamped UX activity list.
*   `pain_point`, `dm`, `hiring_intent_found`, `tech_stack_found`, `icebreaker_angle`, `email`, `phone` (Derived ML vectors).

---

## 3. THE INTENT & SEARCH ENGINE (Data Flow)

The lead generation mechanism avoids cold generic keyword searches by translating business profiles into natively structured symptom-based intelligence dorks.

### The Symptom Extraction Process
When a sweep executes natively, the initial user `bio` string triggers a prompt payload to `gemini-2.5-flash`:
```text
"The user solves this business problem: '{bio}'. 
Generate 3 highly specific Google Search operators to find targets PUBLICLY EXPERIENCING this problem. 
Rule 1: You MUST include at least one query targeting social/professional networks using 'site:linkedin.com', 'site:facebook.com', or 'site:reddit.com'. 
Rule 2: You MUST append negative keywords to exclude retail/informational sites (e.g., '-shop -cart -amazon -wiki')."
```
*Why:* By forcing strict boolean Dorking logic, the search avoids direct competitors. For example, instead of searching "Cybersecurity Software", the LLM forces queries like `("data breach fine") AND (site:linkedin.com) -shop`.

### Serper Geo-Fencing & Execution
The dynamic Dork string resolves and combines explicitly with the `target_location` bound to the campaign.
```python
search_serper(query=f"{base_query} AND {campaign_location}", location=campaign_location)
```
*Why:* Executing local campaigns forces real-time native location appendages directly inside the API header `gl` schema, destroying globally ranked eCommerce SEO algorithms and locking lead scopes strictly to regional business entities.

---

## 4. THE RLHF & SELF-LEARNING MECHANISM (Zero-Cost Machine Learning)

The core competency of the pipeline is its zero-cost reinforcement mechanism. It requires no background CRON jobs; it calculates exclusively off of existing operational data loops in real-time.

### The UI Trigger Loop
When an end-user examines the generated leads in React, they actively label them via **Ignore** or **Converted** buttons.
*   This Native REST `PUT` updates `status` and flags the `/api/leads` HTTP endpoint.
*   **The Orchestrator natively evaluates this state:** It identifies the extracted variables (`tech_wordpress`, `tech_hubspot`, `hiring_intent`). 
*   If "Ignored", it issues a `-1` decrement via `firestore.Increment(-1)` into the user parent document's `preferences_weights` mapping. If "Converted", it triggers `+1`.

### Integration A: Generative Historical Mining
When a new search payload triggers sequentially, `pipeline-main` executes native analytical sweeps.
It fetches the past `20` leads explicitly flagged as `contacted` or `converted`. It concatenates their extracted `pain_point` justifications natively into a JSON blob and requests Gemini to "Extract 3 conceptual B2B phrase trends".
These successful keywords are intelligently appended (via `AND ()`) automatically to the next Search Engine Serper lookup pipeline.

### Integration B: The Interceptor Drop
When Serper acquires a URL, and it is scraped, the codebase natively intercepts its extracted data vectors *before* spending Vertex AI billing credits.
```python
fit_score = 0
for tech in tech_stack:
    fit_score += preferences_weights.get(f"tech_{tech}", 0)
if fit_score <= -3:
    doc_ref.delete()
    continue # Lead vaporized. Saves compute sequence. 
```
*Why:* If the tenant has a track record of continuously marking Shopify or WordPress businesses as "Ignored," the system natively deletes the document immediately parsing the initial HTML payload if it calculates a fatal score threshold.

---

## 5. SCRAPING & AI ENRICHMENT (State Management)

### The Container Isolation (`scraper-heavy`)
Because modern Web 2.0 frameworks require DOM instantiation, the Playwright implementation relies on active event loops natively modified for maximum scale constraints.
*   **Asset Blocking:** Native configuration enforces `page.route("**/*")` to instantly `.abort()` requests fetching an `image`, `media`, `font`, or `stylesheet`. This exclusively guarantees the `Node` process retains minimal vRAM payload logic without catastrophic Kubernetes OOM crashing scenarios.
*   **Timeout Handling:** Native execution loops leverage `domcontentloaded` triggers bounding out natively at `< 15000ms`, failing gracefully out of Javascript runtime stalls.

### The Value-Chain Intent Gate (`filter_b2b_urls`)
URL lists generated natively from the Serper sequence must pass a strict Semantic check sequence before being approved for complete scraping logic. 
*Why:* Gemini reads the title and snippets verifying the value logic natively via instruction:
```text
"CRITICAL INTENT CHECK: Is the website EXPERIENCING the problem the user solves, or are they SELLING a solution to it? You MUST reject any URL that is an SEO blog, a competitor, or a direct-to-consumer (D2C) retail catalog. Only approve targets that match the user's intended value chain."
```

### State Orchestration Defense (The Crash Vector Guard)
During the V12 transition, major infrastructure upgrades fixed orphan-state corruption. 
All Playwright fallback sweeps and AI enrichment payload operations are structurally wrapped inside rigid parent `try...except Exception:` blocks native to `main.py`.
If the `scraper-heavy` container crashes sequentially out of memory, or Gemini goes completely unresponsive natively, the error gracefully triggers returning `db.collection("leads").update({"status": "failed"})`. No lead document stays isolated in `"processing"`. 

---

## 6. FRONTEND UX & TELEMETRY (React)

The Client UX focuses exclusively on native interaction rendering, converting machine telemetry directly into actionable execution funnels.

### The Actionable Pipeline
*   **3-Part Analytics Funnel:** A high-level visual representation maps globally evaluated system states evaluating *Discovered Today*, *Actionable* (`status == 'new'`), and *Ignored* flows. 
*   **Competitor & Technology Visibility:** The React client identifies specific technology stack arrays parsing natively returned data payloads and overlays structured metadata Badges dynamically atop the HTML Card sequence, avoiding bloated descriptions.
*   **Single-Click Execution:** Replacing deprecated "Auto-Send" scripts, the "Copy Message" invocation actively writes the Generative AI drafted contextual greeting natively into the system Clipboard array and tags the document `"status": "contacted"`. 
*   **CRM Interoperability:** Implements an external `no-cors` cross-site post mapping JSON leads natively into external infrastructure endpoints like Zapier/Make.

### The L0 Super Admin Matrix
The system avoids catastrophic multi-tenant nested reads utilizing optimized Firestore logic.
*   **Macro Analytics:** Using structural `count()` operations over individual `.get()` execution scripts, the platform efficiently generates multi-dimensional platform aggregations isolating health pulses. 
*   **In-Memory UI Sorting:** The L0 Governance view utilizes active React DOM states wrapping an HTML injection loop tied to Javascript `sortL0Table('wallet')` controls, dynamically manipulating arrays client-side without incurring any execution load physically inside the Database endpoints. Usage of mapping `.is_active`, `.approval_status`, and array states dynamically surfaces the `MINT`, `SUSPEND`, and `APPROVE` operational capabilities rendering strictly via mapped JSON representations without querying directly.
