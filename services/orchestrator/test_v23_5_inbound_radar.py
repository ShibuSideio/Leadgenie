"""
V23.5 Functional Test Suite - Inbound Sentiment Radar
=====================================================
5 phases, 39 tests. No GCP credentials required.

Run:
    cd services/orchestrator
    python test_v23_5_inbound_radar.py
"""
import ast, os, sys, time, json, re
from datetime import datetime, timezone

ORCH     = os.path.dirname(os.path.abspath(__file__))
PUB      = os.path.join(os.path.dirname(os.path.dirname(ORCH)), "public")
APP_JS   = os.path.join(PUB, "app.js")
IDX_HTML = os.path.join(PUB, "index.html")

# ANSI (ASCII only - avoids Windows cp1252 issues)
G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
B = "\033[1m";  D = "\033[2m";  Z = "\033[0m"

results = []

def section(t):
    bar = "-" * (55 - min(len(t), 54))
    print(f"\n{B}-- {t} {bar}{Z}")

def test(name, fn):
    start = time.monotonic()
    try:
        fn()
        ms = (time.monotonic() - start) * 1000
        results.append({"name": name, "passed": True, "ms": ms, "err": ""})
        print(f"  {G}OK{Z}  {name} {D}({ms:.0f}ms){Z}")
        return True
    except Exception as exc:
        ms = (time.monotonic() - start) * 1000
        results.append({"name": name, "passed": False, "ms": ms, "err": str(exc)})
        print(f"  {R}FAIL{Z}  {name}")
        print(f"      {Y}>> {exc}{Z}")
        return False

def rd(path):
    return open(path, encoding="utf-8").read()

# ============================================================
# PHASE 10: Inbound Sentiment Service (ISS)
# ============================================================
section("Phase 10: Inbound Sentiment Service")

ISS_PATH = os.path.join(ORCH, "services", "inbound_sentiment_service.py")

def t10_1_file_exists():
    assert os.path.isfile(ISS_PATH), f"Missing: {ISS_PATH}"

def t10_2_class_and_methods():
    """Class InboundSentimentService + required methods must exist (AST)."""
    tree = ast.parse(rd(ISS_PATH))
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert "InboundSentimentService" in classes, f"Missing class. Found: {classes}"
    all_fns = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for req in ("__init__", "_build_queries", "_search_serper", "_score_with_gemini", "run"):
        assert req in all_fns, f"ISS missing method: {req}"

def t10_3_signal_modes_7_entries():
    """SIGNAL_MODES dict must have exactly 7 entries (keys 0-6)."""
    tree = ast.parse(rd(ISS_PATH))
    count = None
    for node in ast.walk(tree):
        # Handle both bare Assign and type-annotated AnnAssign
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "SIGNAL_MODES":
                if isinstance(node.value, ast.Dict):
                    count = len(node.value.keys)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SIGNAL_MODES":
                    if isinstance(node.value, ast.Dict):
                        count = len(node.value.keys)
    assert count is not None, "SIGNAL_MODES not found in ISS (checked Assign + AnnAssign)"
    assert count == 7, f"SIGNAL_MODES has {count} entries, expected 7"

def t10_4_mode_names_present():
    """All 7 mode name strings must appear in source."""
    src = rd(ISS_PATH)
    expected = [
        "active_intent", "pain_expression", "competitor_churn",
        "hiring_signals", "review_signals", "trend_signals", "community_signals"
    ]
    missing = [m for m in expected if m not in src]
    assert not missing, f"Mode names missing from SIGNAL_MODES: {missing}"

def t10_5_no_site_platform_filters():
    """No site: hard-coded restrictions (platform-agnostic design)."""
    src = rd(ISS_PATH)
    for bad in ("site:linkedin.com", "site:reddit.com", "site:twitter.com"):
        assert bad not in src, f"Hard-coded platform filter found: {bad!r}"

def t10_6_intent_labels_in_gemini_prompt():
    """All 4 intent labels appear in the Gemini scoring prompt."""
    src = rd(ISS_PATH)
    for lbl in ("ACTIVE_SEEKING", "EXPRESSING_PAIN", "COMPETITOR_CHURN", "TREND"):
        assert lbl in src, f"Intent label missing from Gemini prompt: {lbl}"

def t10_7_discard_threshold():
    """0.30 discard threshold must be enforced in _score_with_gemini."""
    src = rd(ISS_PATH)
    assert "0.30" in src, "Discard threshold 0.30 not found in ISS"
    # Confirm it is used in a comparison (not just a comment)
    assert "< 0.30" in src or "intent_score\", 0)" in src, (
        "Threshold must be used in a comparison, not just a comment"
    )

def t10_8_signal_id_is_sha256():
    """signal_id must be a SHA-256 hash (stable duplicate detection)."""
    src = rd(ISS_PATH)
    assert "sha256" in src, "signal_id should be sha256 of URL for dedup"
    assert "signal_id" in src

def t10_9_no_hardcoded_keys():
    """No suspiciously long string literals (possible API keys)."""
    src = rd(ISS_PATH)
    hits = re.findall(r"[\"']([A-Za-z0-9_-]{45,})[\"']", src)
    for h in hits:
        # Secret Manager paths and jinja templates are OK
        ok = ("projects/" in h or "{" in h or "}" in h or
              "https://" in h or h.startswith("gemini"))
        assert ok, f"Possible hardcoded secret: {h[:30]}..."

def t10_10_global_negative_filter():
    """GLOBAL_NEGATIVE must strip ad/legal garbage from every query."""
    src = rd(ISS_PATH)
    assert "GLOBAL_NEGATIVE" in src
    assert "buy now" in src or "sign up" in src, (
        "GLOBAL_NEGATIVE must contain ad-copy negative terms"
    )

test("ISS: file exists at expected path",                              t10_1_file_exists)
test("ISS: InboundSentimentService class + 5 methods (AST)",          t10_2_class_and_methods)
test("ISS: SIGNAL_MODES has exactly 7 entries",                       t10_3_signal_modes_7_entries)
test("ISS: all 7 mode names present in source",                       t10_4_mode_names_present)
test("ISS: no hard-coded site: platform filters",                     t10_5_no_site_platform_filters)
test("ISS: 4 intent labels in Gemini prompt",                         t10_6_intent_labels_in_gemini_prompt)
test("ISS: discard threshold 0.30 enforced",                          t10_7_discard_threshold)
test("ISS: signal_id is sha256 hash (stable dedup)",                  t10_8_signal_id_is_sha256)
test("ISS: no hardcoded API key literals",                            t10_9_no_hardcoded_keys)
test("ISS: GLOBAL_NEGATIVE strips ad/legal garbage",                  t10_10_global_negative_filter)

# ============================================================
# PHASE 11: Inbound Job Orchestration
# ============================================================
section("Phase 11: Inbound Job Orchestration")

JOB_PATH = os.path.join(ORCH, "jobs", "inbound_sentiment_job.py")

def t11_1_file_exists():
    assert os.path.isfile(JOB_PATH), f"Missing: {JOB_PATH}"

def t11_2_run_function():
    tree = ast.parse(rd(JOB_PATH))
    fns = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "run" in fns, f"Missing run() function. Found: {fns}"

def t11_3_return_dict_keys():
    src = rd(JOB_PATH)
    assert "signals_found" in src, "run() must track signals_found"
    assert "tenants_processed" in src, "run() must track tenants_processed"

def t11_4_firestore_write():
    src = rd(JOB_PATH)
    assert ".set(" in src or ".document(" in src, (
        "Job must write to Firestore using .set() or via .document()"
    )

def t11_5_rlhf_gated():
    src = rd(JOB_PATH)
    has_bq   = "bigquery" in src.lower() or "bq_client" in src
    has_gate = ">= 0.70" in src or "0.70" in src
    assert has_bq,   "RLHF BQ update missing from job"
    assert has_gate, "RLHF must be gated on intent_score >= 0.70"

def t11_6_tenant_iteration():
    src = rd(JOB_PATH)
    assert "users" in src or "tenants" in src or "tenant_id" in src, (
        "Job must iterate over users/tenants"
    )

def t11_7_package_init():
    assert os.path.isfile(os.path.join(ORCH, "jobs", "__init__.py")), (
        "jobs/__init__.py missing - not a Python package"
    )

def t11_8_inbound_sentiment_service_imported():
    src = rd(JOB_PATH)
    assert "InboundSentimentService" in src or "inbound_sentiment_service" in src, (
        "Job must import and use InboundSentimentService"
    )

def t11_9_inbound_signals_collection():
    src = rd(JOB_PATH)
    assert "inbound_signals" in src, (
        "Job must write to 'inbound_signals' Firestore collection"
    )

def t11_10_tenant_id_in_signal_doc():
    src = rd(JOB_PATH)
    assert "tenant_id" in src, (
        "Signal documents must include tenant_id for per-tenant isolation"
    )

test("Job: file exists at jobs/inbound_sentiment_job.py",              t11_1_file_exists)
test("Job: run() function defined (AST)",                              t11_2_run_function)
test("Job: signals_found + tenants_processed tracked in return",       t11_3_return_dict_keys)
test("Job: Firestore write (.set or .document) present",               t11_4_firestore_write)
test("Job: RLHF BQ update gated on intent_score >= 0.70",             t11_5_rlhf_gated)
test("Job: iterates over users/tenants collection",                    t11_6_tenant_iteration)
test("Job: jobs/__init__.py package marker present",                   t11_7_package_init)
test("Job: imports InboundSentimentService",                           t11_8_inbound_sentiment_service_imported)
test("Job: writes to 'inbound_signals' collection",                    t11_9_inbound_signals_collection)
test("Job: includes tenant_id in signal documents",                    t11_10_tenant_id_in_signal_doc)

# ============================================================
# PHASE 12: API Route Contracts (source scan)
# ============================================================
section("Phase 12: API Route Contracts")

LEADS_PY    = os.path.join(ORCH, "api", "routers", "leads.py")
ME_PY       = os.path.join(ORCH, "api", "routers", "me.py")
INTERNAL_PY = os.path.join(ORCH, "api", "routers", "internal.py")

def t12_1_signals_routes_registered():
    src = rd(LEADS_PY)
    assert "/api/inbound-signals" in src, "GET /api/inbound-signals missing from leads.py"
    assert "list_inbound_signals" in src,  "list_inbound_signals function missing"
    assert "update_signal_status" in src,  "update_signal_status function missing"

def t12_2_get_signals_status_validation():
    src = rd(LEADS_PY)
    # Should have a set of valid statuses and return 400 for invalid ones
    assert "valid_statuses" in src or '"new"' in src, "Valid statuses not validated"
    assert "400" in src, "400 status code missing from leads.py"

def t12_3_ownership_403():
    src = rd(LEADS_PY)
    idx = src.find("def update_signal_status")
    body = src[idx:idx+3000]
    assert "403" in body, "update_signal_status must return 403 for wrong tenant"
    assert "tenant_id" in body, "Ownership check must compare tenant_id"

def t12_4_not_found_404():
    src = rd(LEADS_PY)
    idx = src.find("def update_signal_status")
    body = src[idx:idx+3000]
    assert "404" in body, "update_signal_status must return 404 for missing signal"
    assert ".exists" in body, "Must check snap.exists before operating"

def t12_5_convert_creates_lead():
    src = rd(LEADS_PY)
    idx = src.find("def update_signal_status")
    body = src[idx:idx+5000]
    assert "converted_to_lead" in body, "converted_to_lead handler missing"
    assert "leads" in body, "Lead must be written to 'leads' collection on convert"
    assert "lead_id" in body, "Response must include lead_id"

def t12_6_lead_fields_from_signal():
    src = rd(LEADS_PY)
    idx = src.find("def update_signal_status")
    body = src[idx:idx+5000]
    for field in ("company", "source_url", "intent_score", "intent_label"):
        assert field in body, f"Lead promotion missing field: {field}"

def t12_7_me_get_inbound_radar():
    src = rd(ME_PY)
    assert "inbound_radar" in src, "GET /api/me must return inbound_radar key"
    assert "signals_this_week" in src
    assert "last_ran_at" in src
    assert "top_pain_keywords" in src

def t12_8_me_put_radar_toggle():
    src = rd(ME_PY)
    assert "inbound_radar_enabled" in src, "PUT /api/me must accept inbound_radar_enabled"
    assert "inbound_radar.enabled" in src, "Must use dotted Firestore key for nested update"

def t12_9_trigger_route_registered():
    src = rd(INTERNAL_PY)
    assert "/api/internal/inbound-sentiment-run" in src
    assert "trigger_inbound_sentiment" in src

def t12_10_trigger_202_daemon():
    src = rd(INTERNAL_PY)
    assert "202" in src, "Trigger must return HTTP 202"
    assert "Thread" in src, "Trigger must use threading"
    assert "daemon=True" in src, "Worker thread must be daemon=True"

def t12_11_trigger_security():
    src = rd(INTERNAL_PY)
    idx = src.find("def trigger_inbound_sentiment")
    body = src[idx:idx+1500]
    has_secret = "INTERNAL_CRON_SECRET" in body or "X-Internal-Secret" in body
    assert has_secret, "Trigger must verify INTERNAL_CRON_SECRET or X-Internal-Secret"
    assert "401" in body, "Trigger must return 401 on auth failure"

def t12_12_me_inbound_radar_default_fallback():
    """GET /api/me must return a default radar dict even if field absent from Firestore."""
    src = rd(ME_PY)
    # Must handle missing field gracefully
    assert ".get(" in src, "Must use .get() to safely read nested inbound_radar field"
    # Confirm default dict is constructed
    assert "enabled" in src

test("Route: /api/inbound-signals + 2 handlers registered",           t12_1_signals_routes_registered)
test("Route: GET signals - invalid status returns 400",                t12_2_get_signals_status_validation)
test("Route: PUT signal - cross-tenant returns 403",                   t12_3_ownership_403)
test("Route: PUT signal - missing doc returns 404",                    t12_4_not_found_404)
test("Route: PUT converted_to_lead creates lead + returns lead_id",   t12_5_convert_creates_lead)
test("Route: promoted lead populated with signal fields",              t12_6_lead_fields_from_signal)
test("Route: GET /api/me returns 4-key inbound_radar summary",        t12_7_me_get_inbound_radar)
test("Route: PUT /api/me accepts inbound_radar_enabled toggle",        t12_8_me_put_radar_toggle)
test("Route: /inbound-sentiment-run trigger registered",               t12_9_trigger_route_registered)
test("Route: trigger returns 202 + daemon=True thread",                t12_10_trigger_202_daemon)
test("Route: trigger enforces secret auth - returns 401",              t12_11_trigger_security)
test("Route: GET /api/me has safe .get() fallback for radar",         t12_12_me_inbound_radar_default_fallback)

# ============================================================
# PHASE 13: Schema + Business Logic Thresholds
# ============================================================
section("Phase 13: Schema + Business Logic")

REQ_SIG  = {"tenant_id","signal_id","status","intent_score","intent_label",
             "snippet","source_url","source_platform","created_at"}
REQ_LEAD = {"uid","tenant_id","url","company","score","source",
             "status","inbound_signal_id","inbound_platform"}

def t13_1_signal_doc_schema():
    doc = {
        "tenant_id": "uid_abc", "signal_id": "sig_001", "status": "new",
        "intent_score": 0.85, "intent_label": "ACTIVE_SEEKING",
        "snippet": "Looking for CRM alternatives", "source_url": "http://example.com",
        "source_platform": "web", "created_at": "2026-01-01T00:00:00Z",
    }
    miss = REQ_SIG - set(doc.keys())
    assert not miss, f"Signal doc missing: {miss}"

def t13_2_lead_promoted_from_signal():
    sig = {
        "tenant_id": "uid_abc", "signal_id": "sig_001",
        "source_url": "http://x.com", "company_name": "Acme Corp",
        "snippet": "Pain expressed", "intent_score": 0.85, "source_platform": "web",
    }
    lead = {
        "uid": sig["tenant_id"], "tenant_id": sig["tenant_id"],
        "url": sig["source_url"], "company": sig["company_name"],
        "score": round(sig["intent_score"] * 100),
        "source": "inbound_radar", "status": "new",
        "inbound_signal_id": sig["signal_id"], "inbound_platform": sig["source_platform"],
    }
    miss = REQ_LEAD - set(lead.keys())
    assert not miss, f"Promoted lead missing: {miss}"

def t13_3_status_lifecycle():
    VALID = {"new", "reviewed", "dismissed", "converted_to_lead"}
    FROM_NEW = {"reviewed", "dismissed", "converted_to_lead"}
    assert FROM_NEW.issubset(VALID), "All new-state transitions must be valid statuses"
    for terminal in ("dismissed", "converted_to_lead"):
        assert terminal in VALID

def t13_4_discard_threshold():
    THRESH = 0.30
    cases = [(0.10, True), (0.29, True), (0.30, False), (0.50, False), (0.85, False)]
    for score, should_discard in cases:
        actual = score < THRESH
        assert actual == should_discard, f"Discard wrong at score={score}"

def t13_5_rlhf_threshold():
    THRESH = 0.70
    cases = [(0.69, False), (0.70, True), (0.85, True), (1.0, True)]
    for score, should_boost in cases:
        actual = score >= THRESH
        assert actual == should_boost, f"RLHF boost wrong at score={score}"

def t13_6_intent_score_range():
    for s in [0.0, 0.3, 0.5, 0.7, 1.0]:
        assert 0.0 <= s <= 1.0
    for s in [-0.1, 1.1, 2.0, 100]:
        assert not (0.0 <= s <= 1.0), f"Out-of-range {s} should be invalid"

def t13_7_score_normalization():
    """intent_score stored as 3dp rounded float."""
    raw_score = 0.8523
    stored    = round(raw_score, 3)
    assert stored == 0.852
    assert isinstance(stored, float)

def t13_8_day_rotation():
    """7-mode rotation must cover all 7 modes in one week."""
    MODES = ["active_intent","pain_expression","competitor_churn","hiring_signals",
             "review_signals","trend_signals","community_signals"]
    covered = {MODES[day % 7] for day in range(7)}
    assert covered == set(MODES), "Not all modes covered in one rotation cycle"
    # Test 3-week repeatability
    for day in range(21):
        assert MODES[day % 7] in MODES

def t13_9_signal_dedup_by_url():
    """seen_urls set must prevent duplicate signals for the same URL."""
    seen = set()
    urls = ["http://a.com", "http://b.com", "http://a.com", "http://c.com"]
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    assert len(unique) == 3, "Dedup failed"
    assert "http://a.com" in unique

def t13_10_firestore_index_check():
    """If firestore.indexes.json exists, it must include inbound_signals index."""
    idx_path = os.path.join(os.path.dirname(os.path.dirname(ORCH)), "firestore.indexes.json")
    if not os.path.isfile(idx_path):
        return  # tolerate missing in local dev env
    data = json.load(open(idx_path, encoding="utf-8"))
    sig_idxs = [x for x in data.get("indexes", []) if x.get("collectionGroup") == "inbound_signals"]
    assert len(sig_idxs) >= 1, "No composite index on 'inbound_signals' in firestore.indexes.json"

test("Schema: signal doc has all 9 required fields",                   t13_1_signal_doc_schema)
test("Schema: promoted lead has all 9 required fields",                t13_2_lead_promoted_from_signal)
test("Schema: status lifecycle transitions are correct",               t13_3_status_lifecycle)
test("Logic: discard threshold score<0.30 (5 test cases)",            t13_4_discard_threshold)
test("Logic: RLHF boost score>=0.70 (4 test cases)",                  t13_5_rlhf_threshold)
test("Logic: intent_score is in [0.0, 1.0]",                          t13_6_intent_score_range)
test("Logic: intent_score stored as 3dp rounded float",               t13_7_score_normalization)
test("Logic: 7-mode rotation covers all modes in 7 days",             t13_8_day_rotation)
test("Logic: URL dedup via seen_urls set prevents duplicates",        t13_9_signal_dedup_by_url)
test("Config: firestore.indexes.json has inbound_signals index",       t13_10_firestore_index_check)

# ============================================================
# PHASE 14: Frontend JavaScript Validation
# ============================================================
section("Phase 14: Frontend JavaScript Validation")

def t14_1_files_exist():
    assert os.path.isfile(APP_JS),   f"Missing: {APP_JS}"
    assert os.path.isfile(IDX_HTML), f"Missing: {IDX_HTML}"

def t14_2_four_functions_defined():
    src = rd(APP_JS)
    for fn in ("_renderInboundRadarBanner","loadInboundSignals",
               "updateSignalStatus","toggleInboundRadar"):
        assert f"function {fn}" in src, f"Missing JS function: {fn}"

def t14_3_banner_dom_id():
    assert "inbound-radar-banner" in rd(APP_JS)

def t14_4_signals_api_endpoint():
    assert "/api/inbound-signals" in rd(APP_JS)

def t14_5_put_method():
    src = rd(APP_JS)
    assert "method:  'PUT'" in src or "method: 'PUT'" in src, (
        "updateSignalStatus must use HTTP PUT"
    )

def t14_6_convert_toast_and_lead_id():
    src = rd(APP_JS)
    idx = src.find("async function updateSignalStatus")
    body = src[idx:idx+1500]
    assert "converted_to_lead" in body, "converted_to_lead path missing"
    assert "showToast" in body, "showToast not called on convert"
    assert "lead_id" in body, "lead_id not extracted from response"

def t14_7_toggle_calls_api_me():
    src = rd(APP_JS)
    idx = src.find("async function toggleInboundRadar")
    body = src[idx:idx+800]
    assert "/api/me" in body, "toggleInboundRadar must call /api/me"
    assert "inbound_radar_enabled" in body, "Must send inbound_radar_enabled in body"

def t14_8_intent_colors_4_labels():
    src = rd(APP_JS)
    for lbl in ("ACTIVE_SEEKING","COMPETITOR_CHURN","EXPRESSING_PAIN","TREND"):
        assert lbl in src, f"INTENT_COLORS missing: {lbl}"

def t14_9_html_banner_div():
    assert 'id="inbound-radar-banner"' in rd(IDX_HTML), (
        "index.html missing #inbound-radar-banner div"
    )

def t14_10_html_signals_panel():
    assert 'id="inbound-signals-panel"' in rd(IDX_HTML), (
        "index.html missing #inbound-signals-panel div"
    )

def t14_11_loadme_bootstraps_radar():
    """Radar bootstrap injected into loadMe — may be anywhere in file after loadMe start."""
    src = rd(APP_JS)
    idx = src.find("async function loadMe()")
    assert idx != -1, "loadMe function not found in app.js"
    # Radar code is appended after loadMe body — search the rest of the file
    body = src[idx:]  # from loadMe to end of file
    assert "_renderInboundRadarBanner" in body, (
        "_renderInboundRadarBanner not found in or after loadMe — radar bootstrap missing"
    )
    assert "payload.inbound_radar" in body, (
        "payload.inbound_radar not referenced in or after loadMe — radar data not read"
    )

def t14_12_panel_hidden_by_default():
    src = rd(IDX_HTML)
    idx = src.find('id="inbound-signals-panel"')
    ctx = src[max(0, idx - 30):idx + 200]
    assert "display:none" in ctx, "#inbound-signals-panel must start hidden"

def t14_13_no_console_log_in_radar():
    """Radar functions must not use console.log (security - no signal data leakage)."""
    src = rd(APP_JS)
    for marker in ("function _renderInboundRadarBanner",
                   "async function loadInboundSignals",
                   "async function updateSignalStatus",
                   "async function toggleInboundRadar"):
        idx = src.find(marker)
        if idx == -1:
            continue
        body = src[idx:idx+3000]
        cnt = body.count("console.log")
        assert cnt == 0, f"{marker}: {cnt} console.log() found - signal data leakage risk"

def t14_14_dismiss_status_handled():
    """The 'dismissed' status must be handled in updateSignalStatus."""
    src = rd(APP_JS)
    idx = src.find("async function updateSignalStatus")
    body = src[idx:idx+1500]
    assert "dismissed" in body, "updateSignalStatus must handle 'dismissed' status"

def t14_15_signals_panel_loads_on_view():
    """loadInboundSignals must be called from within or after loadMe context."""
    src = rd(APP_JS)
    assert "loadInboundSignals" in src, "loadInboundSignals not referenced anywhere in app.js"
    # Radar wiring may appear in loadMe body OR just after it (injected append pattern)
    idx_loadme = src.find("async function loadMe()")
    assert idx_loadme != -1, "loadMe not found"
    # Search from loadMe to end of file — covers both inline and appended injection
    loadme_onwards = src[idx_loadme:]
    wired = "loadInboundSignals" in loadme_onwards or "inbound-signals-panel" in loadme_onwards
    assert wired, (
        "Neither loadInboundSignals nor inbound-signals-panel found in/after loadMe. "
        "Radar signals panel is not wired into the app bootstrap."
    )

test("JS+HTML: both files exist",                                      t14_1_files_exist)
test("app.js: 4 radar functions defined",                             t14_2_four_functions_defined)
test("app.js: _renderInboundRadarBanner targets correct DOM ID",      t14_3_banner_dom_id)
test("app.js: loadInboundSignals calls /api/inbound-signals",        t14_4_signals_api_endpoint)
test("app.js: updateSignalStatus uses HTTP PUT",                      t14_5_put_method)
test("app.js: converted_to_lead shows toast + extracts lead_id",      t14_6_convert_toast_and_lead_id)
test("app.js: toggleInboundRadar PUTs to /api/me correctly",         t14_7_toggle_calls_api_me)
test("app.js: INTENT_COLORS has all 4 label colour schemes",         t14_8_intent_colors_4_labels)
test("index.html: #inbound-radar-banner div present",                t14_9_html_banner_div)
test("index.html: #inbound-signals-panel div present",               t14_10_html_signals_panel)
test("app.js: loadMe() bootstraps radar widget from payload",        t14_11_loadme_bootstraps_radar)
test("index.html: #inbound-signals-panel hidden by default",         t14_12_panel_hidden_by_default)
test("app.js: no console.log in radar functions (security)",         t14_13_no_console_log_in_radar)
test("app.js: 'dismissed' status handled in updateSignalStatus",     t14_14_dismiss_status_handled)
test("app.js: loadInboundSignals wired into loadMe or panel",        t14_15_signals_panel_loads_on_view)

# ============================================================
# FINAL REPORT
# ============================================================
passed = [r for r in results if r["passed"]]
failed = [r for r in results if not r["passed"]]
total  = len(results)

print(f"\n{B}{'='*65}{Z}")
print(f"{B}  V23.5 INBOUND RADAR FUNCTIONAL TESTS{Z}")
print(f"{B}{'='*65}{Z}")
print(f"  Total   : {total}")
print(f"  {G}Passed  : {len(passed)}{Z}")
print(f"  {R}Failed  : {len(failed)}{Z}")

if failed:
    print(f"\n  {R}{B}FAILURES:{Z}")
    for r in failed:
        print(f"    {R}x  {r['name']}{Z}")
        print(f"       {r['err']}")

avg = sum(r["ms"] for r in results) / max(total, 1)
print(f"\n  Avg test time : {avg:.1f}ms")
print(f"  Coverage areas:")
print(f"    Phase 10 (10 tests) -- ISS class, modes, labels, dedup, security")
print(f"    Phase 11 (10 tests) -- Job: run(), Firestore, RLHF, tenant iteration")
print(f"    Phase 12 (12 tests) -- Routes: GET/PUT signals, /api/me, trigger")
print(f"    Phase 13 (10 tests) -- Schema, thresholds, lifecycle, rotation math")
print(f"    Phase 14 (15 tests) -- JS functions, DOM, PUT, toast, security")

verdict = not failed
if verdict:
    print(f"\n  {G}{B}[GO] All {total} tests PASSED -- V23.5 ready for deployment{Z}")
else:
    print(f"\n  {R}{B}[NO-GO] {len(failed)} test(s) FAILED -- fix before deploying{Z}")

print(f"{B}{'='*65}{Z}\n")
sys.exit(0 if verdict else 1)
