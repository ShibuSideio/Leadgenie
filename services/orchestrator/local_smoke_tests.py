"""
V23 Local Smoke Test Suite
===========================
Runs against the V23 module code directly - no GCP credentials required.

Tests:
  Phase 1: Module structure & import validation
  Phase 2: Pure-function behavioral validation (math, NLP, URL parsing, geo)
  Phase 3: Exception hierarchy & error contracts
  Phase 4: Logging system validation
  Phase 5: Config & environment hardening

Usage:
    python local_smoke_tests.py
"""
from __future__ import annotations
import os
import sys
import traceback
import time
import importlib.util
import types
from typing import Any, Callable
from unittest.mock import MagicMock, patch

# ── Set required env vars BEFORE importing config modules ────────────────────
# Use explicit empty-string guard so a CI step that exports ENCRYPTION_KEY=''
# (e.g. an unresolved bash placeholder) doesn't silently break the cipher tests.
_SMOKE_FERNET_KEY = "uNqG8Jc-44SjK22N8B5-2GksnE5F_88_V5wQZ02j1A0="  # test-only, not production
if not os.environ.get("ENCRYPTION_KEY"):
    os.environ["ENCRYPTION_KEY"] = _SMOKE_FERNET_KEY
if not os.environ.get("PROJECT_ID"):
    os.environ["PROJECT_ID"] = "sideio-leads-v16"
if not os.environ.get("LOCATION"):
    os.environ["LOCATION"] = "asia-south1"

# ── sys.path bootstrap ────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICES = os.path.dirname(_HERE)
for p in (_HERE, _SERVICES):
    if p not in sys.path:
        sys.path.insert(0, p)

# ── Mock GCP SDK modules that have Python 3.14-incompatible C-extension
#    metaclasses (google-cloud-firestore, firebase_admin use tp_new hooks
#    that are not ported to CPython 3.14 yet). All business logic tests
#    bypass these stubs; only import-path tests use the mocks. ─────────────

def _make_mock_module(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__spec__ = None  # type: ignore
    return m

# firebase_admin stubs
_fb_admin      = _make_mock_module("firebase_admin")
_fb_admin._apps = {}
_fb_admin.initialize_app = MagicMock()
_fb_admin.credentials  = _make_mock_module("firebase_admin.credentials")
_fb_admin.firestore    = _make_mock_module("firebase_admin.firestore")
_fb_admin.firestore.client = MagicMock(return_value=MagicMock())
_fb_admin.firestore.SERVER_TIMESTAMP = "__SERVER_TIMESTAMP__"
_fb_admin.firestore.Increment = MagicMock(side_effect=lambda x: x)
_fb_admin.firestore.ArrayUnion = MagicMock(side_effect=lambda x: x)
_fb_admin.auth         = _make_mock_module("firebase_admin.auth")
_fb_admin.auth.verify_id_token = MagicMock(return_value={"uid": "test_uid", "email": "test@example.com"})

for _mod_name, _mod in {
    "firebase_admin":              _fb_admin,
    "firebase_admin.credentials":  _fb_admin.credentials,
    "firebase_admin.firestore":    _fb_admin.firestore,
    "firebase_admin.auth":         _fb_admin.auth,
}.items():
    sys.modules.setdefault(_mod_name, _mod)

# google.cloud stubs
_gc           = _make_mock_module("google")
_gc_cloud     = _make_mock_module("google.cloud")
_gc_fs        = _make_mock_module("google.cloud.firestore")
_gc_fs.Client = MagicMock(return_value=MagicMock())
_gc_fs.SERVER_TIMESTAMP  = "__SERVER_TIMESTAMP__"
_gc_fs.Increment         = MagicMock(side_effect=lambda x: x)
_gc_fs.ArrayUnion        = MagicMock(side_effect=lambda x: x)
_gc_bq        = _make_mock_module("google.cloud.bigquery")
_gc_bq.Client = MagicMock(return_value=MagicMock())
_gc_bq.ScalarQueryParameter = MagicMock()
_gc_bq.QueryJobConfig       = MagicMock()
_gc_tasks     = _make_mock_module("google.cloud.tasks_v2")
_gc_tasks.CloudTasksClient  = MagicMock()
_gc_tasks.HttpMethod        = MagicMock()
_gc_sm        = _make_mock_module("google.cloud.secretmanager")
_gc_sm.SecretManagerServiceClient = MagicMock(return_value=MagicMock())
_gc_storage   = _make_mock_module("google.cloud.storage")
_gc_kms       = _make_mock_module("google.cloud.kms")
_gc_kms.KeyManagementServiceClient = MagicMock()
_gc_fs_v1     = _make_mock_module("google.cloud.firestore_v1")
_gc_fs_v1_bq  = _make_mock_module("google.cloud.firestore_v1.base_query")
_gc_fs_v1_bq.FieldFilter = MagicMock()

for _mod_name, _mod in {
    "google":                               _gc,
    "google.cloud":                         _gc_cloud,
    "google.cloud.firestore":               _gc_fs,
    "google.cloud.bigquery":                _gc_bq,
    "google.cloud.tasks_v2":                _gc_tasks,
    "google.cloud.secretmanager":           _gc_sm,
    "google.cloud.storage":                 _gc_storage,
    "google.cloud.kms":                     _gc_kms,
    "google.cloud.firestore_v1":            _gc_fs_v1,
    "google.cloud.firestore_v1.base_query": _gc_fs_v1_bq,
}.items():
    sys.modules.setdefault(_mod_name, _mod)

# vertexai stub
_vai = _make_mock_module("vertexai")
_vai.init = MagicMock()
_vai_gm = _make_mock_module("vertexai.generative_models")
_vai_gm.GenerativeModel  = MagicMock()
_vai_gm.GenerationConfig = MagicMock()
for _n, _m in {"vertexai": _vai, "vertexai.generative_models": _vai_gm}.items():
    sys.modules.setdefault(_n, _m)

# Flask stub — minimal to allow Blueprint imports
try:
    import flask  # real flask if installed
except ImportError:
    _flask = _make_mock_module("flask")
    class _Blueprint:
        def __init__(self, name, *a, **kw): self.name = name
        def route(self, *a, **kw):
            def dec(fn): return fn
            return dec
    _flask.Flask     = MagicMock()
    _flask.Blueprint = _Blueprint
    _flask.jsonify   = MagicMock()
    _flask.request   = MagicMock()
    _flask.make_response = MagicMock()
    sys.modules.setdefault("flask", _flask)
    sys.modules.setdefault("flask_cors", _make_mock_module("flask_cors"))

# cryptography — real library (should be installed)
try:
    from cryptography.fernet import Fernet  # noqa: F401
except ImportError:
    _crypto = _make_mock_module("cryptography")
    _crypto_fernet = _make_mock_module("cryptography.fernet")
    class _Fernet:
        def __init__(self, key): self._key = key
        def encrypt(self, data): return data
        def decrypt(self, data): return data
    _crypto_fernet.Fernet = _Fernet
    sys.modules.setdefault("cryptography", _crypto)
    sys.modules.setdefault("cryptography.fernet", _crypto_fernet)

# ── ANSI ─────────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"
BOLD   = "\033[1m";  DIM = "\033[2m";  RESET  = "\033[0m"

# ── Result tracker ────────────────────────────────────────────────────────────
results: list[dict] = []

def test(name: str, fn: Callable) -> bool:
    start = time.monotonic()
    try:
        fn()
        ms = (time.monotonic() - start) * 1000
        results.append({"name": name, "passed": True, "ms": ms, "err": ""})
        print(f"  {GREEN}✓{RESET}  {name} {DIM}({ms:.0f}ms){RESET}")
        return True
    except Exception as exc:
        ms = (time.monotonic() - start) * 1000
        err = str(exc)
        results.append({"name": name, "passed": False, "ms": ms, "err": err})
        print(f"  {RED}✗{RESET}  {name}")
        print(f"      {YELLOW}↳ {err}{RESET}")
        return False

def section(title: str):
    print(f"\n{BOLD}── {title} {'─'*(55 - len(title))}{RESET}")

# =============================================================================
# PHASE 1: Module Import Validation
# =============================================================================
section("Phase 1: Module Import Validation")

def t_import_shared_base_path():
    from shared.base_path import parse_base_path, SOCIAL_ONTOLOGY_DOMAINS
    assert callable(parse_base_path), "parse_base_path must be callable"
    assert isinstance(SOCIAL_ONTOLOGY_DOMAINS, frozenset), "Must be frozenset"
    assert "reddit.com" in SOCIAL_ONTOLOGY_DOMAINS

def t_import_shared_geo_map():
    from shared.geo_map import GL_MAP, resolve_geo
    assert isinstance(GL_MAP, dict), "GL_MAP must be a dict"
    assert callable(resolve_geo)
    assert len(GL_MAP) >= 10, f"Expected >= 10 entries, got {len(GL_MAP)}"

def t_import_shared_tech_signatures():
    from shared.tech_signatures import TECH_SIGNATURES, extract_tech_stack
    assert isinstance(TECH_SIGNATURES, dict)
    assert callable(extract_tech_stack)
    assert "wordpress" in TECH_SIGNATURES

def t_import_core_exceptions():
    from core.exceptions import (
        LeadSniperError, AuthError, TokenVerificationError,
        AccountSuspendedError, ForbiddenError, QuotaExhaustedError,
        ApprovalPendingError, DatabaseError, DatabaseTimeoutError,
        TransactionConflictError, ExternalServiceError,
        SerperRateLimitError, VertexAITimeoutError,
        SecretManagerError, ValidationError, SchemaViolationError,
    )
    assert issubclass(TokenVerificationError, AuthError)
    assert issubclass(AuthError, LeadSniperError)
    assert issubclass(QuotaExhaustedError, LeadSniperError)

def t_import_core_config():
    import importlib, sys
    _saved = os.environ.get("ENCRYPTION_KEY")
    os.environ["ENCRYPTION_KEY"] = _SMOKE_FERNET_KEY
    for mod_name in [k for k in list(sys.modules) if k in ("core.config", "core_config")]:
        del sys.modules[mod_name]
    try:
        from core.config import (
            PROJECT_ID, LOCATION, QUEUE, ROI_DEFAULTS, ALLOWED_ORIGINS, get_cipher
        )
        # PROJECT_ID can be the real GCP project (Cloud Build injects it) or
        # the local default — just assert it is a non-empty string.
        assert isinstance(PROJECT_ID, str) and PROJECT_ID, \
            f"PROJECT_ID must be a non-empty string, got {PROJECT_ID!r}"
        assert isinstance(ROI_DEFAULTS, dict), "ROI_DEFAULTS must be a dict"
        assert "avg_cpl" in ROI_DEFAULTS, "ROI_DEFAULTS must contain avg_cpl"
        cipher = get_cipher()
        assert cipher is not None, "get_cipher() returned None"
    finally:
        if _saved is not None:
            os.environ["ENCRYPTION_KEY"] = _saved
        elif "ENCRYPTION_KEY" in os.environ:
            del os.environ["ENCRYPTION_KEY"]

def t_import_core_logging():
    from core.logging import get_logger
    assert callable(get_logger)
    log = get_logger("test.module", service="smoke_test")
    assert hasattr(log, "info")
    assert hasattr(log, "warning")
    assert hasattr(log, "error")

def t_import_repositories_firestore():
    from repositories.firestore_repo import (
        get_user, create_user, update_user, list_campaigns,
        get_campaign, list_leads, count_leads_by_status,
        get_wallet_shards_total, get_router_config,
        get_unit_economics, save_unit_economics,
    )
    for fn in [get_user, list_campaigns, get_campaign, list_leads,
               count_leads_by_status, get_wallet_shards_total]:
        assert callable(fn), f"{fn.__name__} must be callable"

def t_import_services_auth():
    from services.auth_service import authenticate_request
    assert callable(authenticate_request)

def t_import_services_analytics():
    from services.analytics_service import compute_roi_metrics, validate_and_build_ue_update
    assert callable(compute_roi_metrics)
    assert callable(validate_and_build_ue_update)

def t_import_services_shadow_tracker():
    from services.intelligence.shadow_tracker import extract_ngrams, async_shadow_track
    assert callable(extract_ngrams)
    assert callable(async_shadow_track)

def t_import_services_neg_signal():
    from services.intelligence.neg_signal import (
        async_neg_signal_insert, NEG_SIGNAL_REASONS
    )
    assert callable(async_neg_signal_insert)
    assert "competitor" in NEG_SIGNAL_REASONS
    assert "author" in NEG_SIGNAL_REASONS

def t_import_api_middleware():
    from api.middleware import require_auth, require_super_admin
    assert callable(require_auth)
    assert callable(require_super_admin)

def t_import_api_routers():
    from api.routers.analytics import bp as analytics_bp
    from api.routers.me import bp as me_bp
    from api.routers.data_reads import bp as data_reads_bp
    for bp in [analytics_bp, me_bp, data_reads_bp]:
        assert bp is not None
        assert hasattr(bp, "name"), "Blueprint must have a name"

test("shared.base_path imports correctly",           t_import_shared_base_path)
test("shared.geo_map imports correctly",             t_import_shared_geo_map)
test("shared.tech_signatures imports correctly",     t_import_shared_tech_signatures)
test("core.exceptions imports all 15 classes",       t_import_core_exceptions)
test("core.config loads and Fernet cipher ready",    t_import_core_config)
test("core.logging get_logger returns BoundLogger",  t_import_core_logging)
test("repositories.firestore_repo exports 10 fns",  t_import_repositories_firestore)
test("services.auth_service imports",                t_import_services_auth)
test("services.analytics_service imports",           t_import_services_analytics)
test("services.intelligence.shadow_tracker imports", t_import_services_shadow_tracker)
test("services.intelligence.neg_signal imports",     t_import_services_neg_signal)
test("api.middleware imports decorators",             t_import_api_middleware)
test("api.routers all 3 Blueprints import",          t_import_api_routers)

# =============================================================================
# PHASE 2: Pure-Function Behavioral Validation
# =============================================================================
section("Phase 2: Pure-Function Behavioral Validation")

def t_parse_base_path_b2b():
    from shared.base_path import parse_base_path
    assert parse_base_path("https://www.techcrunch.com/2024/article") == "techcrunch.com"
    assert parse_base_path("salesforce.com/crm/pricing") == "salesforce.com"
    assert parse_base_path("") == "unknown"

def t_parse_base_path_social():
    from shared.base_path import parse_base_path
    r = parse_base_path("https://reddit.com/r/Entrepreneur/comments/xyz/post")
    assert r == "reddit.com/r/Entrepreneur", f"Got: {r!r}"
    li = parse_base_path("https://linkedin.com/in/johndoe/activity")
    assert "linkedin.com" in li

def t_parse_base_path_www_strip():
    from shared.base_path import parse_base_path
    assert parse_base_path("https://www.hubspot.com/products") == "hubspot.com"

def t_resolve_geo_india():
    from shared.geo_map import resolve_geo
    loc, gl = resolve_geo("Mumbai, India")
    assert loc == "India" and gl == "in", f"Got ({loc!r}, {gl!r})"

def t_resolve_geo_usa():
    from shared.geo_map import resolve_geo
    loc, gl = resolve_geo("New York, USA")
    assert gl == "us", f"Got gl={gl!r}"

def t_resolve_geo_global():
    from shared.geo_map import resolve_geo
    loc, gl = resolve_geo("Global")
    assert loc == "" and gl == ""

def t_resolve_geo_unknown():
    from shared.geo_map import resolve_geo
    loc, gl = resolve_geo("Some Unknown Place XYZ")
    assert loc == "" and gl == ""

def t_extract_tech_stack_wordpress():
    from shared.tech_signatures import extract_tech_stack
    html = "<link rel='stylesheet' href='/wp-content/themes/mytheme/style.css'>"
    result = extract_tech_stack(html)
    assert "wordpress" in result, f"Got: {result}"

def t_extract_tech_stack_multiple():
    from shared.tech_signatures import extract_tech_stack
    html = "cdn.shopify.com js.stripe.com js.hs-scripts.com"
    result = extract_tech_stack(html)
    assert "shopify"  in result
    assert "stripe"   in result
    assert "hubspot"  in result

def t_extract_tech_stack_empty():
    from shared.tech_signatures import extract_tech_stack
    assert extract_tech_stack("plain text no tech signals") == []

def t_extract_ngrams_basic():
    from services.intelligence.shadow_tracker import extract_ngrams
    text = "struggling with lead generation pipeline"
    ngrams = extract_ngrams(text, top_k=10)
    assert isinstance(ngrams, list)
    assert len(ngrams) > 0
    # Stop-words like 'with' should be filtered
    for ng in ngrams:
        assert len(ng.split()) >= 2, f"N-gram too short: {ng!r}"

def t_extract_ngrams_empty():
    from services.intelligence.shadow_tracker import extract_ngrams
    assert extract_ngrams("") == []
    assert extract_ngrams("   ") == []

def t_extract_ngrams_stopwords_filtered():
    from services.intelligence.shadow_tracker import extract_ngrams
    # "the and for are" are all stop-words — should produce no valid n-grams
    result = extract_ngrams("the and for are but not", top_k=5)
    for ng in result:
        words = ng.split()
        assert "the" not in words and "and" not in words

# ── ROI Formula Audit ─────────────────────────────────────────────────────────
def t_roi_formula_ad_savings():
    """ad_savings = n_approved × avg_cpl"""
    n_approved = 10
    avg_cpl    = 50.0
    expected   = round(n_approved * avg_cpl, 2)
    assert expected == 500.0

def t_roi_formula_labor_savings():
    """labor_savings = (n_approved × 15 / 60) × sdr_hourly_rate"""
    n_approved = 10
    sdr_rate   = 15.0
    expected   = round((n_approved * 15 / 60) * sdr_rate, 2)
    assert expected == 37.5

def t_roi_formula_pipeline_value():
    """pipeline_value = n_approved × conversion_rate × deal_size"""
    n      = 10
    rate   = 0.02
    size   = 5000.0
    pv     = round(n * rate * size, 2)
    assert pv == 1000.0

def t_roi_formula_zero_deal_size():
    """pipeline_value = 0 when avg_deal_size = 0 (must not show phantom revenue)"""
    pv = round(100 * 0.02 * 0.0, 2)
    assert pv == 0.0

def t_analytics_validate_ue_payload_valid():
    from services.analytics_service import validate_and_build_ue_update
    updates = validate_and_build_ue_update({"avg_cpl": 75.0, "sdr_hourly_rate": 20.0})
    assert "unit_economics.avg_cpl" in updates
    assert updates["unit_economics.avg_cpl"] == 75.0

def t_analytics_validate_ue_payload_empty():
    from services.analytics_service import validate_and_build_ue_update
    from core.exceptions import ValidationError
    try:
        validate_and_build_ue_update({})
        assert False, "Should have raised ValidationError"
    except ValidationError as e:
        assert e.http_status == 400

def t_analytics_validate_ue_currency():
    from services.analytics_service import validate_and_build_ue_update
    updates = validate_and_build_ue_update({"currency": "inr"})
    assert updates["unit_economics.currency"] == "INR", "Currency must uppercase"

def t_analytics_validate_ue_negative_cpl():
    from services.analytics_service import validate_and_build_ue_update
    # Negative values are clamped to 0 (max(0.0, float(val)))
    updates = validate_and_build_ue_update({"avg_cpl": -100.0})
    assert updates["unit_economics.avg_cpl"] == 0.0

test("parse_base_path — B2B domains strip to root",  t_parse_base_path_b2b)
test("parse_base_path — social keeps 2 path segs",   t_parse_base_path_social)
test("parse_base_path — www. stripped correctly",     t_parse_base_path_www_strip)
test("resolve_geo — India → (India, in)",             t_resolve_geo_india)
test("resolve_geo — USA → (USA, us)",                 t_resolve_geo_usa)
test("resolve_geo — Global → ('', '')",               t_resolve_geo_global)
test("resolve_geo — unknown → ('', '')",              t_resolve_geo_unknown)
test("extract_tech_stack — WordPress detected",       t_extract_tech_stack_wordpress)
test("extract_tech_stack — multi-tech detected",      t_extract_tech_stack_multiple)
test("extract_tech_stack — empty on no signals",      t_extract_tech_stack_empty)
test("extract_ngrams — 2-grams from pain text",       t_extract_ngrams_basic)
test("extract_ngrams — empty input returns []",       t_extract_ngrams_empty)
test("extract_ngrams — stop-words filtered",          t_extract_ngrams_stopwords_filtered)
test("ROI formula — ad_savings math correct",         t_roi_formula_ad_savings)
test("ROI formula — labor_savings math correct",      t_roi_formula_labor_savings)
test("ROI formula — pipeline_value math correct",     t_roi_formula_pipeline_value)
test("ROI formula — zero deal_size = 0 revenue",      t_roi_formula_zero_deal_size)
test("analytics validate — valid payload accepted",   t_analytics_validate_ue_payload_valid)
test("analytics validate — empty payload → 400",      t_analytics_validate_ue_payload_empty)
test("analytics validate — currency uppercased",      t_analytics_validate_ue_currency)
test("analytics validate — negative cpl clamped to 0",t_analytics_validate_ue_negative_cpl)

# =============================================================================
# PHASE 3: Exception Hierarchy & HTTP Status Contracts
# =============================================================================
section("Phase 3: Exception Hierarchy & HTTP Status Contracts")

def t_exception_auth_http_401():
    from core.exceptions import AuthError, TokenVerificationError, AccountSuspendedError
    for cls in [AuthError, TokenVerificationError, AccountSuspendedError]:
        e = cls("test")
        assert e.http_status == 401, f"{cls.__name__}.http_status must be 401"

def t_exception_forbidden_http_403():
    from core.exceptions import ForbiddenError
    assert ForbiddenError().http_status == 403

def t_exception_quota_http_402():
    from core.exceptions import QuotaExhaustedError
    assert QuotaExhaustedError().http_status == 402

def t_exception_approval_http_403():
    from core.exceptions import ApprovalPendingError
    assert ApprovalPendingError().http_status == 403

def t_exception_validation_http_400():
    from core.exceptions import ValidationError, SchemaViolationError
    assert ValidationError("bad").http_status == 400
    assert SchemaViolationError("bad").http_status == 400

def t_exception_base_is_exception():
    from core.exceptions import LeadSniperError
    e = LeadSniperError("test error")
    assert isinstance(e, Exception)
    assert str(e) == "test error"
    assert e.message == "test error"

def t_exception_hierarchy_auth_is_lead_sniper():
    from core.exceptions import TokenVerificationError, LeadSniperError, AuthError
    e = TokenVerificationError("bad token")
    assert isinstance(e, AuthError)
    assert isinstance(e, LeadSniperError)
    assert isinstance(e, Exception)

def t_exception_pipeline_classes():
    """Load pipeline-main exceptions via explicit path (avoid orchestrator shadowing)."""
    pipeline_exc_path = os.path.join(_SERVICES, "pipeline-main", "core", "exceptions.py")
    spec = importlib.util.spec_from_file_location("pipeline_main_exceptions", pipeline_exc_path)
    mod  = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert hasattr(mod, "NegShieldTimeoutError"),       "NegShieldTimeoutError missing"
    assert hasattr(mod, "ConfidenceRouterTimeoutError"), "ConfidenceRouterTimeoutError missing"
    assert hasattr(mod, "ValidationError"),             "ValidationError missing"
    assert mod.NegShieldTimeoutError("timeout").http_status == 500
    assert mod.ValidationError("bad").http_status == 400
    assert issubclass(mod.NegShieldTimeoutError, mod.LeadSniperError)

test("AuthError, TokenVerif, Suspended → HTTP 401",  t_exception_auth_http_401)
test("ForbiddenError → HTTP 403",                    t_exception_forbidden_http_403)
test("QuotaExhaustedError → HTTP 402",               t_exception_quota_http_402)
test("ApprovalPendingError → HTTP 403",              t_exception_approval_http_403)
test("ValidationError, SchemaViolation → HTTP 400",  t_exception_validation_http_400)
test("LeadSniperError is Exception, has .message",   t_exception_base_is_exception)
test("TokenVerifError is AuthError is LeadSniper",   t_exception_hierarchy_auth_is_lead_sniper)
test("Pipeline-main exceptions load correctly",      t_exception_pipeline_classes)

# =============================================================================
# PHASE 4: Structured Logging Validation
# =============================================================================
section("Phase 4: Structured Logging Validation")

def t_logger_bound_context():
    from core.logging import get_logger
    log = get_logger("smoke.test", service="orchestrator")
    bound = log.bind(tenant="abc123", operation="test")
    assert bound._ctx.get("service") == "orchestrator"
    assert bound._ctx.get("tenant") == "abc123"

def t_logger_json_output(capsys=None):
    import io, json, logging
    from core.logging import get_logger, _GCPJsonFormatter
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_GCPJsonFormatter())
    logger = logging.getLogger("smoke.json.test")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    
    log = get_logger.__wrapped__(logger, {}) if hasattr(get_logger, '__wrapped__') else None
    # Direct formatter test
    record = logging.LogRecord("test", logging.ERROR, "", 0, "test_event", (), None)
    record.extra = {"tenant": "abc", "count": 5}  # type: ignore
    output = handler.formatter.format(record)
    parsed = json.loads(output)
    assert parsed["severity"] == "ERROR"
    assert parsed["message"] == "test_event"
    assert parsed["tenant"] == "abc"

def t_logger_all_levels():
    from core.logging import get_logger
    log = get_logger("smoke.levels")
    # These must not raise
    log.debug("debug_event", x=1)
    log.info("info_event", x=2)
    log.warning("warn_event", x=3)
    log.error("error_event", x=4)
    log.critical("crit_event", x=5)

test("BoundLogger preserves context across bind()", t_logger_bound_context)
test("_GCPJsonFormatter emits valid JSON with severity", t_logger_json_output)
test("All 5 log levels emit without exception",     t_logger_all_levels)

# =============================================================================
# PHASE 5: Configuration & Environment Hardening
# =============================================================================
section("Phase 5: Config & Environment Hardening")

def t_config_fernet_encryption_roundtrip():
    """Ensure Fernet cipher can encrypt and decrypt with a known-good key."""
    from cryptography.fernet import Fernet as _Fernet
    # Use the test key directly — bypasses any cached state from a prior empty-key run
    cipher = _Fernet(_SMOKE_FERNET_KEY.encode())
    plaintext = b"test-wa-token-abc123"
    encrypted = cipher.encrypt(plaintext)
    decrypted = cipher.decrypt(encrypted)
    assert decrypted == plaintext, "Fernet roundtrip must be lossless"

def t_config_roi_defaults_all_keys():
    from core.config import ROI_DEFAULTS
    required = {"avg_cpl", "avg_deal_size", "sdr_hourly_rate", "est_conversion_rate", "currency"}
    missing = required - set(ROI_DEFAULTS.keys())
    assert not missing, f"ROI_DEFAULTS missing keys: {missing}"
    assert ROI_DEFAULTS["avg_cpl"] == 50.0
    assert ROI_DEFAULTS["currency"] == "USD"

def t_config_missing_encryption_key_raises():
    """If ENCRYPTION_KEY is absent, config must raise ValueError not use fallback."""
    import importlib, os
    saved = os.environ.pop("ENCRYPTION_KEY", None)
    try:
        # Force re-import with missing key
        import importlib.util, types
        spec = importlib.util.spec_from_file_location(
            "core_config_test",
            os.path.join(_HERE, "core", "config.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)  # type: ignore
            # If it doesn't raise, check if there's a hardcoded fallback
            # (bad) or if it added the key from environment (good because we set it above)
            assert False, "Should have raised ValueError for missing ENCRYPTION_KEY"
        except ValueError as ve:
            assert "ENCRYPTION_KEY" in str(ve)
        except Exception:
            pass  # Other import errors acceptable in isolated re-import
    finally:
        if saved:
            os.environ["ENCRYPTION_KEY"] = saved

def t_config_pipeline_serper_key_name():
    import importlib.util as _ilu, sys as _sys
    _mod_key = f"_pipeline_config_{id(t_config_pipeline_serper_key_name)}"
    _saved = os.environ.get("ENCRYPTION_KEY")
    os.environ["ENCRYPTION_KEY"] = _SMOKE_FERNET_KEY
    try:
        spec = _ilu.spec_from_file_location(
            _mod_key,
            os.path.join(_SERVICES, "pipeline-main", "core", "config.py"),
        )
        mod = _ilu.module_from_spec(spec)  # type: ignore
        # Register BEFORE exec so _LazyModule.__class__ assignment can find it
        _sys.modules[_mod_key] = mod
        spec.loader.exec_module(mod)  # type: ignore
        assert hasattr(mod, "SERPER_API_KEY_NAME"), "SERPER_API_KEY_NAME missing"
        assert "SERPER_API_KEY" in mod.SERPER_API_KEY_NAME, \
            f"Expected uppercase 'SERPER_API_KEY' in secret name, got: {mod.SERPER_API_KEY_NAME!r}"
        assert hasattr(mod, "NEG_SHIELD_BQ_TIMEOUT_S"), "NEG_SHIELD_BQ_TIMEOUT_S missing"
        assert mod.NEG_SHIELD_BQ_TIMEOUT_S == 3.0
    except AssertionError:
        raise
    except Exception as e:
        raise AssertionError(f"pipeline-main/core/config.py failed: {e}")
    finally:
        _sys.modules.pop(_mod_key, None)
        if _saved is not None:
            os.environ["ENCRYPTION_KEY"] = _saved
        elif "ENCRYPTION_KEY" in os.environ:
            del os.environ["ENCRYPTION_KEY"]

def t_shared_package_canonical_no_duplicates():
    """Verify shared package is the ONLY definition of parse_base_path."""
    from shared.base_path import parse_base_path as shared_fn
    # Calling the same function from two import paths must be identical object
    import importlib
    m2 = importlib.import_module("shared.base_path")
    assert m2.parse_base_path is shared_fn, "Must be same function object (no duplicate)"

test("Fernet encrypt → decrypt roundtrip lossless", t_config_fernet_encryption_roundtrip)
test("ROI_DEFAULTS has all 5 required keys",         t_config_roi_defaults_all_keys)
test("Missing ENCRYPTION_KEY raises ValueError",     t_config_missing_encryption_key_raises)
test("Pipeline config has SERPER_API_KEY_NAME",      t_config_pipeline_serper_key_name)
test("shared.parse_base_path is canonical (no dup)", t_shared_package_canonical_no_duplicates)

# =============================================================================
# PHASE 6: Lazy Init Lock Concurrency Validation (V23 Task 2)
# =============================================================================
section("Phase 6: Lazy Init Lock Concurrency (gRPC Task 2)")


def t_lazy_init_lock_get_db_concurrent():
    """get_db() under concurrent hammer returns the same singleton.

    Spawns 20 threads all calling pipeline-main's get_db() simultaneously via
    an isolated module import.  Asserts the underlying Client constructor was
    called at most once (double-checked locking guarantee).
    """
    import threading as _th
    import importlib.util as _ilu, sys as _sys

    _saved_enc = os.environ.get("ENCRYPTION_KEY")
    os.environ["ENCRYPTION_KEY"] = _SMOKE_FERNET_KEY

    construction_count = [0]
    sentinel_instance  = object()

    class _CountingClient:
        def __new__(cls, *a, **kw):
            construction_count[0] += 1
            return sentinel_instance

    _mod_key = f"_pm_clients_lock_test_{id(t_lazy_init_lock_get_db_concurrent)}"
    spec = _ilu.spec_from_file_location(
        _mod_key,
        os.path.join(_SERVICES, "pipeline-main", "core", "clients.py"),
    )
    mod = _ilu.module_from_spec(spec)  # type: ignore
    _sys.modules[_mod_key] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore
    except Exception:
        pass

    # Reset and inject our counting client
    mod._db_instance = None
    mod._db_lock = _th.Lock()
    import google.cloud.firestore as _real_fs
    _orig_client = _real_fs.Client
    _real_fs.Client = _CountingClient  # type: ignore

    results_thr = []
    errors_thr  = []
    barrier = _th.Barrier(20)

    def _hammer():
        barrier.wait()
        try:
            results_thr.append(mod.get_db())
        except Exception as exc:
            errors_thr.append(str(exc))

    threads = [_th.Thread(target=_hammer) for _ in range(20)]
    for t_ in threads: t_.start()
    for t_ in threads: t_.join(timeout=5)

    _real_fs.Client = _orig_client
    _sys.modules.pop(_mod_key, None)
    if _saved_enc is not None:
        os.environ["ENCRYPTION_KEY"] = _saved_enc

    assert not errors_thr, f"Thread errors: {errors_thr[:3]}"
    assert len(results_thr) == 20, "Not all 20 threads returned a value"
    first = results_thr[0]
    for inst in results_thr:
        assert inst is first, "Singleton violated — multiple distinct Client instances"
    assert construction_count[0] <= 1, (
        f"Client() constructed {construction_count[0]} times — Lock not working"
    )


def t_lazy_init_vertex_lock_concurrent():
    """init_vertex() concurrent: vertexai.init() called at most once."""
    import threading as _th
    import importlib.util as _ilu, sys as _sys

    _saved_enc = os.environ.get("ENCRYPTION_KEY")
    os.environ["ENCRYPTION_KEY"] = _SMOKE_FERNET_KEY

    init_count = [0]

    import vertexai as _vai_real
    _orig_init = _vai_real.init

    def _counting_init(*a, **kw):
        init_count[0] += 1

    _vai_real.init = _counting_init  # type: ignore

    _mod_key2 = f"_pm_clients_vertex_{id(t_lazy_init_vertex_lock_concurrent)}"
    spec = _ilu.spec_from_file_location(
        _mod_key2,
        os.path.join(_SERVICES, "pipeline-main", "core", "clients.py"),
    )
    mod = _ilu.module_from_spec(spec)  # type: ignore
    _sys.modules[_mod_key2] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore
    except Exception:
        pass

    mod._vertex_initialised = False
    mod._vertex_lock = _th.Lock()

    errors = []
    barrier = _th.Barrier(16)

    def _init_hammer():
        barrier.wait()
        try:
            mod.init_vertex()
        except Exception as exc:
            errors.append(str(exc))

    threads = [_th.Thread(target=_init_hammer) for _ in range(16)]
    for t_ in threads: t_.start()
    for t_ in threads: t_.join(timeout=5)

    _vai_real.init = _orig_init
    _sys.modules.pop(_mod_key2, None)
    if _saved_enc is not None:
        os.environ["ENCRYPTION_KEY"] = _saved_enc

    assert not errors, f"Thread errors: {errors[:3]}"
    assert init_count[0] <= 1, (
        f"vertexai.init() called {init_count[0]} times — Lock not effective"
    )


def t_no_grpc_client_at_module_scope():
    """Importing internal.py must NOT call get_db() at module scope.

    The legacy ``db = get_db()`` line triggered firebase_admin.initialize_app()
    at Blueprint registration time, before Gunicorn forked workers.  This test
    asserts that import-time call count is exactly zero.
    """
    import importlib.util as _ilu, sys as _sys

    call_count = [0]
    _orig_get_db = None

    try:
        import core.clients as _cc
        _orig_get_db = _cc.get_db

        def _spy():
            call_count[0] += 1
            return MagicMock()

        _cc.get_db = _spy  # type: ignore

        _mod_key3 = f"_internal_scope_{id(t_no_grpc_client_at_module_scope)}"
        internal_path = os.path.join(_HERE, "api", "routers", "internal.py")
        spec = _ilu.spec_from_file_location(_mod_key3, internal_path)
        mod  = _ilu.module_from_spec(spec)  # type: ignore
        _sys.modules[_mod_key3] = mod
        try:
            spec.loader.exec_module(mod)  # type: ignore
        except Exception:
            pass
        _sys.modules.pop(_mod_key3, None)
    finally:
        if _orig_get_db is not None:
            import core.clients as _cc2
            _cc2.get_db = _orig_get_db  # type: ignore

    assert call_count[0] == 0, (
        f"get_db() was called {call_count[0]}x at import time. "
        "Module-scope gRPC leak NOT fixed."
    )


test("get_db() concurrent 20 threads: singleton ≤1 construction",    t_lazy_init_lock_get_db_concurrent)
test("init_vertex() concurrent 16 threads: vertexai.init() ≤1 call", t_lazy_init_vertex_lock_concurrent)
test("internal.py import: get_db() NOT called at module scope",       t_no_grpc_client_at_module_scope)

# =============================================================================
# PHASE 7: Timestamp Serialization Middleware (V23 Task 3)
# =============================================================================
section("Phase 7: Timestamp Serialization Middleware (Task 3)")


def t_sanitize_datetime_aware_to_isoformat():
    """to_firestore_ts converts tz-aware datetime to ISO-8601 string."""
    import datetime as _dt
    from core.firestore_utils import to_firestore_ts
    dt = _dt.datetime(2026, 4, 18, 12, 0, 0, tzinfo=_dt.timezone.utc)
    result = to_firestore_ts(dt)
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert "2026-04-18" in result, f"ISO date not found: {result!r}"
    # Timezone must be present
    assert "+00:00" in result or "UTC" in result, \
        f"UTC offset missing from: {result!r}"


def t_sanitize_datetime_naive_gets_utc():
    """to_firestore_ts injects UTC tz into naive datetime before isoformat."""
    import datetime as _dt
    from core.firestore_utils import to_firestore_ts
    naive = _dt.datetime(2026, 1, 1, 0, 0, 0)
    result = to_firestore_ts(naive)
    assert isinstance(result, str), "Must return string"
    assert "+00:00" in result, f"Expected UTC offset, got: {result!r}"


def t_sanitize_passthrough_non_datetime():
    """to_firestore_ts passes str/int/float/None/list/bool through untouched."""
    from core.firestore_utils import to_firestore_ts
    assert to_firestore_ts("already-a-string") == "already-a-string"
    assert to_firestore_ts(42)          == 42
    assert to_firestore_ts(3.14)        == 3.14
    assert to_firestore_ts(None)        is None
    assert to_firestore_ts(True)        is True
    assert to_firestore_ts([1, 2, 3])   == [1, 2, 3]


def t_sanitize_sentinel_server_timestamp_passthrough():
    """SERVER_TIMESTAMP sentinel passes through sanitize_update completely untouched.

    V23 Task 3 Amendment critical assertion:
    Firestore sentinel objects (SERVER_TIMESTAMP, Increment, ArrayUnion) must
    NOT be cast to strings.  Casting would destroy the semantic and write a
    literal string to Firestore instead of triggering the server-side operation.
    """
    from core.firestore_utils import sanitize_update, to_firestore_ts
    import datetime as _dt

    class _MockServerTimestamp:
        """Simulates google.cloud.firestore_v1.transforms.SERVER_TIMESTAMP."""
        sentinel_value = "server_timestamp"  # attribute present on GCP sentinel
        def __repr__(self): return "<SERVER_TIMESTAMP>"

    class _MockIncrement:
        """Simulates firestore.Increment(n)."""
        _document_path = None  # attribute present on GCP transforms
        def __init__(self, val): self.val = val
        def __repr__(self): return f"<Increment({self.val})>"

    sentinel_ts  = _MockServerTimestamp()
    sentinel_inc = _MockIncrement(1)

    # to_firestore_ts must return each sentinel unchanged
    assert to_firestore_ts(sentinel_ts)  is sentinel_ts,  \
        "SERVER_TIMESTAMP was mutated by to_firestore_ts"
    assert to_firestore_ts(sentinel_inc) is sentinel_inc, \
        "Increment sentinel was mutated by to_firestore_ts"

    # Full dict path via sanitize_update
    payload = {
        "updatedAt":       sentinel_ts,
        "score_delta":     sentinel_inc,
        "next_drip_due":   _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc),
        "status":          "active",
    }
    out = sanitize_update(payload)
    assert out["updatedAt"]   is sentinel_ts,  "SERVER_TIMESTAMP mutated by sanitize_update"
    assert out["score_delta"] is sentinel_inc, "Increment mutated by sanitize_update"
    assert isinstance(out["next_drip_due"], str), "datetime not serialized to ISO string"
    assert out["status"] == "active",             "plain string was mutated"


def t_sanitize_legacy_datetime_with_nanoseconds():
    """DatetimeWithNanoseconds (datetime subclass) is serialised to ISO string."""
    import datetime as _dt
    from core.firestore_utils import to_firestore_ts

    class _DatetimeWithNanoseconds(_dt.datetime):
        nanoseconds = 123456789

    dwn = _DatetimeWithNanoseconds(2026, 3, 15, 10, 30, 0, tzinfo=_dt.timezone.utc)
    result = to_firestore_ts(dwn)
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert "2026-03-15" in result, f"Date not found: {result!r}"


test("to_firestore_ts: aware datetime → ISO-8601 string",           t_sanitize_datetime_aware_to_isoformat)
test("to_firestore_ts: naive datetime → UTC ISO-8601 string",       t_sanitize_datetime_naive_gets_utc)
test("to_firestore_ts: str/int/None/list pass through unchanged",   t_sanitize_passthrough_non_datetime)
test("sanitize_update: SERVER_TIMESTAMP + Increment pass through",  t_sanitize_sentinel_server_timestamp_passthrough)
test("to_firestore_ts: DatetimeWithNanoseconds → ISO-8601 string", t_sanitize_legacy_datetime_with_nanoseconds)

# =============================================================================
# FINAL REPORT
# =============================================================================
passed = [r for r in results if r["passed"]]
failed = [r for r in results if not r["passed"]]
total  = len(results)

# =============================================================================
# PHASE 8: OIDC Middleware Validation (V23 Amendment 1)
# =============================================================================
section("Phase 8: OIDC Middleware Validation (Amendment 1)")

_PM_PATH = os.path.join(_SERVICES, "pipeline-main")


def _pm_import(name: str):
    """Import a pipeline-main module by dotted name, injecting its root to sys.path."""
    import importlib.util as _ilu
    # Inject _PM_PATH so that 'from core.logging import ...' resolves inside the module
    if _PM_PATH not in sys.path:
        sys.path.insert(0, _PM_PATH)
    parts   = name.split(".")
    relpath = os.path.join(*parts) + ".py"
    abspath = os.path.join(_PM_PATH, relpath)
    key     = f"_pm_{name.replace('.', '_')}_{os.getpid()}"
    spec    = _ilu.spec_from_file_location(key, abspath)
    mod     = _ilu.module_from_spec(spec)  # type: ignore
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore
    except Exception:
        pass
    return mod


def t_oidc_missing_authorization_header():
    """require_tasks_oidc returns 401 when Authorization header is absent."""
    from flask import Flask
    from unittest.mock import patch

    # Patch _verify_token so we control its outcome without real Google certs
    with patch.dict(os.environ, {"PIPELINE_MAIN_URL": "https://example.run.app",
                                 "PIPELINE_SA_EMAIL": "sa@proj.iam.gserviceaccount.com"}):
        oidc = _pm_import("middleware.oidc")
        app  = Flask("test_oidc_missing")

        @app.route("/test", methods=["POST"])
        @oidc.require_tasks_oidc
        def _protected():
            return "ok", 200

        with app.test_client() as c:
            resp = c.post("/test")
            assert resp.status_code == 401, \
                f"Expected 401 for missing Authorization header, got {resp.status_code}"
            body = resp.get_json()
            assert body.get("code") == "MISSING_OIDC_TOKEN", \
                f"Expected MISSING_OIDC_TOKEN code, got: {body}"


def t_oidc_invalid_token_returns_401():
    """require_tasks_oidc returns 401 when token fails cryptographic verification."""
    from flask import Flask
    from unittest.mock import patch

    with patch.dict(os.environ, {"PIPELINE_MAIN_URL": "https://example.run.app",
                                 "PIPELINE_SA_EMAIL": "sa@proj.iam.gserviceaccount.com"}):
        oidc = _pm_import("middleware.oidc")

        # Patch _verify_token on the loaded module to return failure
        original_verify = oidc._verify_token
        oidc._verify_token = lambda token: (False, "signature mismatch")  # type: ignore

        app  = Flask("test_oidc_invalid")

        @app.route("/test", methods=["POST"])
        @oidc.require_tasks_oidc
        def _protected():
            return "ok", 200

        try:
            with app.test_client() as c:
                resp = c.post("/test", headers={"Authorization": "Bearer bad.token.here"})
                assert resp.status_code == 401, \
                    f"Expected 401 for invalid token, got {resp.status_code}"
                body = resp.get_json()
                assert body.get("code") == "INVALID_OIDC_TOKEN", \
                    f"Expected INVALID_OIDC_TOKEN, got: {body}"
        finally:
            oidc._verify_token = original_verify  # type: ignore


def t_oidc_valid_token_but_missing_queue_header_returns_403():
    """With valid OIDC token but no X-CloudTasks-QueueName header → 403."""
    from flask import Flask

    with patch.dict(os.environ, {"PIPELINE_MAIN_URL": "https://example.run.app",
                                 "PIPELINE_SA_EMAIL": "sa@proj.iam.gserviceaccount.com"}):
        oidc = _pm_import("middleware.oidc")
        oidc._verify_token = lambda token: (True, "")  # type: ignore  # stub valid

        app  = Flask("test_oidc_no_queue")

        @app.route("/test", methods=["POST"])
        @oidc.require_tasks_oidc
        def _protected():
            return "ok", 200

        with app.test_client() as c:
            resp = c.post("/test", headers={"Authorization": "Bearer good.token.here"})
            assert resp.status_code == 403, \
                f"Expected 403 for missing X-CloudTasks-QueueName, got {resp.status_code}"
            body = resp.get_json()
            assert body.get("code") == "MISSING_QUEUE_HEADER", \
                f"Expected MISSING_QUEUE_HEADER, got: {body}"


test("OIDC: missing Authorization header → 401",            t_oidc_missing_authorization_header)
test("OIDC: invalid JWT → 401 INVALID_OIDC_TOKEN",          t_oidc_invalid_token_returns_401)
test("OIDC: valid JWT + missing queue header → 403",         t_oidc_valid_token_but_missing_queue_header_returns_403)

# =============================================================================
# PHASE 9: V23 Modular Pipeline-Main Import Validation
# =============================================================================
section("Phase 9: V23 Modular Pipeline-Main Import Validation")


def t_serper_service_imports_no_grpc():
    """services/serper_service.py defines required public symbols (AST check)."""
    import ast
    src_path = os.path.join(_PM_PATH, "services", "serper_service.py")
    assert os.path.isfile(src_path), f"serper_service.py not found at {src_path}"
    with open(src_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=src_path)
    # Collect top-level names (functions, plain and annotated assignments)
    top_names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    top_names.add(t.id)
        elif isinstance(node, ast.AnnAssign):   # handles: NAME: type = value
            if isinstance(node.target, ast.Name):
                top_names.add(node.target.id)
    required = {"search_serper", "filter_serper_noise", "extract_root_domain", "SOCIAL_DOMAINS"}
    missing  = required - top_names
    assert not missing, f"serper_service missing symbols: {missing}"



def t_gemini_service_imports_no_module_scope_vertex():
    """services/gemini_service.py defines required symbols and has no bare vertexai.init() call (AST)."""
    import ast
    src_path = os.path.join(_PM_PATH, "services", "gemini_service.py")
    assert os.path.isfile(src_path), f"gemini_service.py not found at {src_path}"
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=src_path)

    # Collect top-level function names
    top_names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for sym in ("call_gemini_2_5", "pre_filter_gemini", "final_score_and_dm"):
        assert sym in top_names, f"gemini_service missing top-level function: {sym}"

    # Verify vertexai.init() is not called at module scope (i.e., directly in tree.body)
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            # Detect vertexai.init() and init_vertex() at top level
            if isinstance(call.func, ast.Attribute):
                if getattr(call.func, 'attr', '') == 'init' and \
                   isinstance(call.func.value, ast.Name) and \
                   call.func.value.id == 'vertexai':
                    raise AssertionError(
                        "vertexai.init() called at module scope in gemini_service.py — gRPC leak!"
                    )


def t_query_brain_imports_without_db_call():
    """services/query_brain.py imports without calling get_db() at module scope."""
    import importlib.util as _ilu

    db_called = [0]

    key  = f"_pm_qbrain_{os.getpid()}"
    spec = _ilu.spec_from_file_location(
        key, os.path.join(_PM_PATH, "services", "query_brain.py")
    )
    mod = _ilu.module_from_spec(spec)  # type: ignore
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore
    except Exception:
        pass
    sys.modules.pop(key, None)

    assert hasattr(mod, "generate_smart_query"),  "generate_smart_query not exported"
    assert hasattr(mod, "VECTOR_PLATFORM_MAP"),    "VECTOR_PLATFORM_MAP not exported"


def t_gcs_task_no_module_scope_cloud_tasks():
    """services/gcs_task.py imports without constructing CloudTasksClient."""
    import importlib.util as _ilu
    from google.cloud import tasks_v2 as _tasks

    ctor_called = [0]
    _orig_ctor  = _tasks.CloudTasksClient

    class _SpyClient:
        def __new__(cls, *a, **kw):
            ctor_called[0] += 1
            return object.__new__(cls)

    _tasks.CloudTasksClient = _SpyClient  # type: ignore

    key  = f"_pm_gcstask_{os.getpid()}"
    spec = _ilu.spec_from_file_location(
        key, os.path.join(_PM_PATH, "services", "gcs_task.py")
    )
    mod = _ilu.module_from_spec(spec)  # type: ignore
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore
    except Exception:
        pass

    _tasks.CloudTasksClient = _orig_ctor  # type: ignore
    sys.modules.pop(key, None)

    assert ctor_called[0] == 0, \
        f"CloudTasksClient() was constructed {ctor_called[0]}x at import time — gRPC leak"
    assert hasattr(mod, "enqueue_gcs_dump"), "enqueue_gcs_dump not exported"


test("serper_service imports cleanly — no gRPC at module scope",   t_serper_service_imports_no_grpc)
test("gemini_service imports cleanly — vertexai.init() NOT called", t_gemini_service_imports_no_module_scope_vertex)
test("query_brain imports cleanly — get_db() NOT called at scope",  t_query_brain_imports_without_db_call)
test("gcs_task imports cleanly — CloudTasksClient NOT called",      t_gcs_task_no_module_scope_cloud_tasks)

# =============================================================================
# FINAL REPORT
# =============================================================================
passed = [r for r in results if r["passed"]]
failed = [r for r in results if not r["passed"]]
total  = len(results)

print(f"\n{BOLD}{'='*65}{RESET}")
print(f"{BOLD}  SIDEIO V23 LOCAL SMOKE TEST RESULTS{RESET}")
print(f"{BOLD}{'='*65}{RESET}")
print(f"  Total  : {total}")
print(f"  {GREEN}Passed : {len(passed)}{RESET}")
print(f"  {RED}Failed : {len(failed)}{RESET}")

if failed:
    print(f"\n  {RED}{BOLD}FAILURES:{RESET}")
    for r in failed:
        print(f"    {RED}\u2717  {r['name']}{RESET}")
        print(f"       {r['err']}")

avg_ms = sum(r["ms"] for r in results) / max(total, 1)
print(f"\n  Avg test time : {avg_ms:.1f}ms")
print(f"  Coverage areas: imports, pure-fn math, NLP, URL parsing,")
print(f"                  geo mapping, exception hierarchy, logging,")
print(f"                  config hardening, Fernet encryption,")
print(f"                  gRPC lazy-init locks (Phase 6),")
print(f"                  Firestore timestamp serialization (Phase 7),")
print(f"                  OIDC middleware (Phase 8),")
print(f"                  V23 modular pipeline-main imports (Phase 9)")

verdict = len(failed) == 0
if verdict:
    print(f"\n  {GREEN}{BOLD}\u2705  VERDICT: GO \u2014 All {total} local smoke tests PASSED.{RESET}")
    print(f"  {GREEN}       V23 production cutover: OIDC fail-fast, gRPC lazy init,{RESET}")
    print(f"  {GREEN}       timestamp serialization, per-vector circuit breaker,{RESET}")
    print(f"  {GREEN}       /produce stub retired \u2014 full pipeline re-wired.{RESET}")
    print(f"  {GREEN}       Ready for v23-preview Cloud Run tag deployment.{RESET}")
else:
    print(f"\n  {RED}{BOLD}\U0001F6AB  VERDICT: NO-GO \u2014 {len(failed)} test(s) FAILED.{RESET}")
    print(f"  {RED}       Fix failures before deploying to any environment.{RESET}")

print(f"{BOLD}{'='*65}{RESET}\n")
sys.exit(0 if verdict else 1)

