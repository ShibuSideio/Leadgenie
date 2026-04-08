import re

with open('architecture.md', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Update users schema to add dynamic_blocklist
text = text.replace(
    '"tech_react": 1\n  },',
    '"tech_react": 1\n  },\n  "dynamic_blocklist": ["checkout", "add to cart"], // Auto-populated by RLHF Ignored leads'
)

# 2. Update leads schema
text = text.replace(
    '"tech_stack_found": ["react", "hubspot"],\n  "email": "hr@techcorp.com",',
    '"tech_stack_found": ["react", "hubspot"],\n  "decision_maker_name": "John Doe",\n  "decision_maker_title": "VP of Operations",\n  "company_size_tier": "Mid-Market",\n  "primary_objection_hypothesis": "They might lack budget for external enterprise tooling.",\n  "email": "hr@techcorp.com",'
)

# 3. Update Step 6 with Proxy
proxy_text = """- **Callback Loop:** Scraping executes in total isolation with explicit `--disable-dev-shm-usage` resource aborts. Upon success, `scraper-heavy` queues a Cloud Task back to `pipeline-main/finalize` delivering the DOM payloads to Vertex.
- **Proxy & Secret Vault Execution:** Chromium execution binds routing matrix topologies directly from `google-cloud-secret-manager` (Decodo Standard/Premium networks), preventing WAF blocks and bypassing basic Auth environment exposure."""
text = text.replace(
    '- **Callback Loop:** Scraping executes in total isolation with explicit `--disable-dev-shm-usage` resource aborts. Upon success, `scraper-heavy` queues a Cloud Task back to `pipeline-main/finalize` delivering the DOM payloads to Vertex.',
    proxy_text
)


# 4. Insert Step 7 and shift old Step 7 to 8
old_step7 = "### Step 7: Final Enrichment & DM Drafting"

new_step7 = """### Step 7: Python Fast-Fail Gate & NLP Density Extraction
**Location:** `services/pipeline-main/main.py::finalize`
- **Native TTL Caches:** The payload first hits `scraped_cache` where Firestores native TTL purges the document seamlessly 30 days after `expireAt` without crons.
- **Python Fast-Fail Guard:** Prior to LLM allocation, Python natively scans the blob against the user's `dynamic_blocklist` and a global b2b blacklist. Bouncing storefronts drops execution saving Vertex budget.
- **Density Extraction:** `extract_dense_payload()` evaluates the massive DOM blob ranking standard paragraphs directly against keyword arrays natively lifting the top 10 most relevant bounds, effectively shrinking context windows.

### Step 8: Final Enterprise Schema Extraction & DM Drafting"""

text = text.replace(old_step7, new_step7)
text = text.replace('THE 7-STEP PIPELINE', 'THE 8-STEP PIPELINE')


# 5. Update RLHF Section (Line 213ish)
old_rlhf = """When the UI triggers `Ignore` or `Converted`, the orchestrator natively executes mathematical backpropagation:
```python
delta = 1 if status == "converted" else -1"""

new_rlhf = """When the UI triggers `Ignore` or `Converted`, the orchestrator natively executes mathematical backpropagation:
- **Dynamic Array Blocklists:** If a lead is Ignored, standard B2B taxonomy is injected into the user's `dynamic_blocklist` array using `firestore.ArrayUnion`, creating a self-teaching heuristic wall.
- **Preference Scaling:**
```python
delta = 1 if status == "converted" else -1"""
text = text.replace(old_rlhf, new_rlhf)

# 6. Add Function Map C
func_c = """

### Function Map C: Few-Shot Conversion Context Injection
**Location:** `services/pipeline-main/main.py` inside `final_score_and_dm`.
Just before building the icebreaker, the pipeline fetches the tenant's last 3 leads explicitly marked `status == "converted"`. It injects their DMs directly into the prompt instructing Vertex AI to strictly mimic proven phrasing.
"""
text = text.replace(
    'continue\n```',
    'continue\n```' + func_c
)

# 7. Update Frontend React -> Vanilla JS and add optimizations
old_frontend = "## 6. FRONTEND UX & TELEMETRY (React)"
new_frontend = "## 7. FRONTEND UX & TELEMETRY (Vanilla JS)"
text = text.replace(old_frontend, new_frontend)
text = text.replace("The React client", "The Javascript core")
text = text.replace("The React feed strictly", "The Vanilla pipeline strictly")

old_opt = "*   **Single-Click Execution:** Deprecating the legacy Autonomous WhatsApp Auto-Send, the UI now features a `Copy Message` invocation. It actively copies the Generative AI drafted `dm` text to the user's native system Clipboard and immediately triggers a PUT mapping the lead to `\"status\": \"contacted\"`."

new_opt = """*   **Single-Click Optimistic Drops:** The `Copy Message` invocation mimics React state behavior natively. When fired, it detaches the targeted Lead ID from the intersection virtualizer, drops it from local caching, and physically removes the DOM card instantly (Optimistic UI), letting the backend asynchronous fire complete invisibly.
*   **Virtual DOM Offload:** Rather than looping native lists, the application wraps all objects inside an `IntersectionObserver`. Only the 10-15 viewport targets are actively hydrated with heavy DOM templates, while obscured elements revert to height-bound skeletons, protecting structural FPS rates on heavy 500-lead arrays."""

text = text.replace(old_opt, new_opt + '\n*   **Single-Click Execution:** Deprecating the legacy Autonomous WhatsApp Auto-Send, the UI now features a `Copy Message` invocation. It actively copies the Generative AI drafted `dm` text to the user\'s native system Clipboard and immediately triggers a PUT mapping the lead to `"status": "contacted"`.')

l0_telemetry = """*   **Macro Analytics:** By querying `db.collection("leads").where(...).count()`, the `/api/l0/telemetry` endpoint calculates total global scale without ever executing thousands of document reads natively.
*   **In-Memory UI Sorting:** The L0 Governance telemetry table utilizes `window.sortL0Table('email' | 'wallet' | 'leads')` within Javascript. This intercepts the DOM table representation and inherently sorts the JSON arrays directly in the client's vRAM, entirely bypassing backend query execution costs."""

l0_telemetry_new = l0_telemetry + """\n*   **Debounced Telemetry Lock:** The L0 dashboard utilizes a native memory debounce lock. A 30-second epoch validation prevents admin spammers from infinitely querying the massive document tables via the UI Refresh button."""

text = text.replace(l0_telemetry, l0_telemetry_new)

with open('architecture.md', 'w', encoding='utf-8') as f:
    f.write(text)
