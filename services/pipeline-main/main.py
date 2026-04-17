import os
import random
import json
import httpx
import hashlib
import datetime
import hashlib
import threading
import uuid
import concurrent.futures
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from flask import Flask, request, jsonify
from google.cloud import firestore
from google.cloud import bigquery as _bq_lib
import google.auth
from google.cloud import secretmanager
from google.api_core.exceptions import AlreadyExists, ResourceExhausted
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type, retry_if_exception
from google.cloud.firestore_v1.transaction import transactional as _firestore_transactional
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from pydantic import ValidationError
from models import LeadPayload

app = Flask(__name__)


# =============================================================================
# V18: ASYNC GCS RAW FIREHOSE DUMP
# Dumps pre-filter raw social/web payloads into sideio-raw-firehose-lake.
# Runs in a daemon thread — never blocks the ingestion pipeline.
# Object path: raw/{tenant_id}/{YYYYMMDD}/{uuid}.json
# =============================================================================

GCS_FIREHOSE_BUCKET = os.environ.get("GCS_FIREHOSE_BUCKET", "sideio-raw-firehose-lake")

def _dump_raw_to_gcs(raw_payload: dict, tenant_id: str):
    """
    Asynchronously dumps a raw (pre-filter) lead payload to the GCS firehose lake.
    Must be fully self-contained — runs in a daemon thread.
    """
    try:
        from google.cloud import storage as gcs_lib
        import uuid, datetime, json

        gcs = gcs_lib.Client()
        bucket = gcs.bucket(GCS_FIREHOSE_BUCKET)

        date_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
        object_id = str(uuid.uuid4())
        blob_name = f"raw/{tenant_id}/{date_str}/{object_id}.json"

        # Enrich with dump metadata
        dump_payload = {
            "_dump_id":       object_id,
            "_dumped_at":     datetime.datetime.utcnow().isoformat() + "Z",
            "_tenant_id":     tenant_id,
            **raw_payload
        }

        blob = bucket.blob(blob_name)
        blob.upload_from_string(
            json.dumps(dump_payload, default=str),
            content_type="application/json"
        )
        print(f"[GCS FIREHOSE] Dumped raw payload → gs://{GCS_FIREHOSE_BUCKET}/{blob_name}")
    except Exception as e:
        print(f"[GCS FIREHOSE] Non-blocking dump failed: {e}")


def _async_gcs_dump(raw_payload: dict, tenant_id: str):
    """Fire-and-forget wrapper — spawns a daemon thread for GCS dump."""
    t = threading.Thread(target=_dump_raw_to_gcs, args=(raw_payload, tenant_id), daemon=True)
    t.start()

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "version": "12.99.1", "location": "us-central1"}), 200

# =============================================================================
# LAZY CLIENT SINGLETONS — gRPC PRE-FORK DEADLOCK FIX
#
# ROOT CAUSE OF DEADLOCK: Gunicorn forks worker processes AFTER module import.
# Any gRPC channel opened at module scope (firestore.Client(), secretmanager
# .SecretManagerServiceClient(), vertexai.init()) is inherited by the child
# process as a DEAD/LOCKED file descriptor. The first .get()/.stream() call
# in the child blocks forever — the channel-level mutex is held by the parent
# thread that no longer exists. No Python timeout fires because the lock is
# at the C-extension level, below Python's signal machinery.
#
# FIX: All three clients are now lazy singletons. The gRPC channel is opened
# inside get_db() / get_sm_client() / ensure_vertexai_init() on the FIRST
# call from within the already-forked worker process. The threading.Lock()
# prevents duplicate initialization under concurrent Flask requests.
# =============================================================================
import threading as _threading_init

_db_instance: "firestore.Client | None" = None
_db_lock = _threading_init.Lock()

def get_db() -> "firestore.Client":
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                print("[LAZY_INIT] Initializing Firestore client (post-fork)...")
                _db_instance = firestore.Client()
                print("[LAZY_INIT] Firestore client ready.")
    return _db_instance

# Backward-compat module-level alias — resolved lazily on first attribute access.
# All existing code references `db.collection(...)` which triggers get_db() on
# the first actual call, NOT at import time.
class _LazyDB:
    """Proxy that defers firestore.Client() construction until first attribute access."""
    def __getattr__(self, name):
        return getattr(get_db(), name)

db = _LazyDB()

_sm_instance: "secretmanager.SecretManagerServiceClient | None" = None
_sm_lock = _threading_init.Lock()

def get_sm_client() -> "secretmanager.SecretManagerServiceClient":
    global _sm_instance
    if _sm_instance is None:
        with _sm_lock:
            if _sm_instance is None:
                print("[LAZY_INIT] Initializing Secret Manager client (post-fork)...")
                _sm_instance = secretmanager.SecretManagerServiceClient()
                print("[LAZY_INIT] Secret Manager client ready.")
    return _sm_instance

sm_client = get_sm_client  # kept as callable alias; callers do get_sm_client()

_vertexai_initialized = False
_vertexai_lock = _threading_init.Lock()

def ensure_vertexai_init():
    global _vertexai_initialized
    if not _vertexai_initialized:
        with _vertexai_lock:
            if not _vertexai_initialized:
                print("[LAZY_INIT] Initializing Vertex AI SDK (post-fork)...")
                vertexai.init(location="us-central1")
                _vertexai_initialized = True
                print("[LAZY_INIT] Vertex AI SDK ready.")

project_id = os.environ.get("PROJECT_ID", "sideio-leads-v16")
PROJECT_ID = project_id
LOCATION   = os.environ.get("LOCATION", "asia-south1")
QUEUE      = os.environ.get("QUEUE", "lead-pipeline-queue")

SCRAPER_HEAVY_URL   = os.environ.get("SCRAPER_HEAVY_URL", "https://scraper-heavy-abc.a.run.app/scrape")
SERPER_API_KEY_NAME = f"projects/{project_id}/secrets/SERPER_API_KEY/versions/latest"
FERNET_KEY          = os.environ.get("ENCRYPTION_KEY", "uNqG8Jc-44SjK22N8B5-2GksnE5F_88_V5wQZ02j1A0=")
cipher_suite        = Fernet(FERNET_KEY.encode())

def call_gemini_2_5(prompt: str, expect_json: bool = True, response_schema=None, system_instruction=None):
    ensure_vertexai_init()  # gRPC-safe: no-op if already initialized in this worker
    model = GenerativeModel("gemini-2.5-flash", system_instruction=system_instruction)
    if expect_json:
        config = GenerationConfig(response_mime_type="application/json", response_schema=response_schema)
    else:
        config = None
    
    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(5), retry=retry_if_exception_type(ResourceExhausted))
    def _invoke_model():
        return model.generate_content(prompt, generation_config=config)
        
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_invoke_model)
            response = future.result(timeout=45.0)
    except concurrent.futures.TimeoutError:
        print("Vertex AI execution timed out / quota hang.")
        raise TimeoutError("Vertex AI timeout")
    
    if expect_json:
        # Native JSON mode eliminates the need for regex/markdown stripping
        return json.loads(response.text)
    return response.text

def get_secret(secret_name):
    response = get_sm_client().access_secret_version(request={"name": secret_name})
    return response.payload.data.decode("UTF-8")

def search_serper(query, location=None, gl=None):
    """
    FIX 3: Serper Resilience — 429-specific tenacity retry with exponential backoff.
    Thundering herd: 50 concurrent workers can saturate Serper's concurrency limit.
    The retry predicate targets ONLY 429 (rate limited) — auth failures (401/403)
    and server errors (5xx) are not retried to avoid burning the backoff budget.
    4 attempts: immediate + 4s + 8s + 16s = up to ~30s total, still within
    Cloud Tasks' default 10-minute task deadline.
    """
    api_key = get_secret(SERPER_API_KEY_NAME).strip()
    if not api_key:
        raise ValueError(
            f"CRITICAL: SERPER_API_KEY resolved to empty string from secret "
            f"'{SERPER_API_KEY_NAME}'. Verify the secret exists in GCP Secret Manager "
            f"with exact name 'SERPER_API_KEY' (uppercase) and has an active version."
        )
    url     = "https://google.serper.dev/search"
    payload_dict = {"q": query, "num": 20}
    if location:
        payload_dict["location"] = location
    if gl:
        payload_dict["gl"] = gl
    payload = json.dumps(payload_dict)
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    def _is_rate_limited(exc: BaseException) -> bool:
        """Retry predicate: only True for Serper 429 responses."""
        return (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code == 429
        )

    @retry(
        wait=wait_exponential(multiplier=2, min=4, max=32),
        stop=stop_after_attempt(4),
        retry=retry_if_exception(_is_rate_limited),
        reraise=False,
    )
    def _do_post():
        r = httpx.post(url, headers=headers, data=payload, timeout=30)
        if r.status_code == 429:
            r.raise_for_status()   # raises HTTPStatusError → triggers retry
        if r.status_code == 200:
            return r.json().get("organic", [])
        print(f"[SERPER] Non-retryable HTTP {r.status_code}: {r.text[:200]}")
        return []

    try:
        return _do_post()
    except Exception as e:
        print(f"[SERPER] All retries exhausted for query '{query[:60]}': {e}")
        _update_circuit_telemetry("serper_429")   # Signal circuit breaker: all retries exhausted
        return []


# =============================================================================
# CIRCUIT BREAKER TELEMETRY WRITER
# Maintains a 15-minute sliding window of error rates in Firestore.
# Called by search_serper (429 events) and scraper-heavy webhook (OOM events).
# The orchestrator's cron sweep reads these counters before dispatching tasks.
#
# Firestore document: system_telemetry/circuit_breaker_state
# Fields incremented atomically (Increment, never set):
#   serper_calls_window  — total Serper calls in the current window
#   serper_429s_window   — 429 responses in the current window
#   scraper_calls_window — total scrape triggers in the current window
#   scraper_ooms_window  — OOM/timeout failures in the current window
#   window_reset_at      — timestamp marking the start of the current window
# =============================================================================
def _update_circuit_telemetry(event_type: str):
    """
    Non-blocking circuit breaker telemetry update.
    event_type: "serper_call" | "serper_429" | "scraper_call" | "scraper_oom"
    Failure is swallowed — telemetry must never block the pipeline.
    """
    CB_WINDOW_MINUTES = int(os.environ.get("CB_WINDOW_MINUTES", "15"))
    cb_ref = db.collection("system_telemetry").document("circuit_breaker_state")

    increment_map = {
        "serper_call":  {"serper_calls_window":  firestore.Increment(1)},
        "serper_429":   {"serper_calls_window":  firestore.Increment(1),
                         "serper_429s_window":   firestore.Increment(1)},
        "scraper_call": {"scraper_calls_window": firestore.Increment(1)},
        "scraper_oom":  {"scraper_calls_window": firestore.Increment(1),
                         "scraper_ooms_window":  firestore.Increment(1)},
    }
    updates = increment_map.get(event_type)
    if not updates:
        return

    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        # Reset the window if it has expired (done inside a best-effort update)
        cb_snap = cb_ref.get()
        cb_data = cb_snap.to_dict() if cb_snap.exists else {}
        window_reset = cb_data.get("window_reset_at")
        if window_reset:
            if hasattr(window_reset, "tzinfo") and window_reset.tzinfo is None:
                window_reset = window_reset.replace(tzinfo=datetime.timezone.utc)
            elapsed = (now_utc - window_reset).total_seconds()
            if elapsed > CB_WINDOW_MINUTES * 60:
                # Window expired — reset all counters
                cb_ref.set({
                    "serper_calls_window":  0,
                    "serper_429s_window":   0,
                    "scraper_calls_window": 0,
                    "scraper_ooms_window":  0,
                    "window_reset_at":      now_utc,
                }, merge=False)
                # Re-apply the current event as the first count in the new window
                cb_ref.set(updates, merge=True)
                return
        else:
            # First write ever — set window start
            updates["window_reset_at"] = now_utc

        cb_ref.set(updates, merge=True)
    except Exception as tel_err:
        print(f"[CB TELEMETRY] Non-fatal write failure ({event_type}): {tel_err}")


def safe_truncate(text: str, max_bytes: int = 100000) -> str:
    """Enforce strict byte-level truncation to prevent Firestore 1MB document crashes."""
    encoded = text.encode('utf-8', errors='ignore')
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode('utf-8', errors='ignore')


# Social domains that need deeper path-level precision in the ontology map.
_SOCIAL_ONTOLOGY_DOMAINS = {
    "reddit.com", "facebook.com", "linkedin.com", "quora.com",
    "kaggle.com", "instagram.com", "twitter.com", "x.com", "youtube.com"
}

def parse_base_path(url: str) -> str:
    """
    Extracts the canonical base_path key for the ontology_map collection.

    Rules (per Phase 1 architectural ruling):
      • Social / Walled-Garden domains  →  domain + first 2 path segments
          reddit.com/r/Entrepreneur/comments/xyz  →  reddit.com/r/Entrepreneur
      • All other (B2B / news / directories) →  root domain only
          www.techcrunch.com/2024/03/article     →  techcrunch.com

    Strips www., query params, fragments, and trailing slashes.
    Returns 'unknown' as a safe sentinel if parsing fails.
    """
    try:
        parsed   = urlparse(url if url.startswith('http') else f'https://{url}')
        hostname = parsed.hostname or ''
        # Strip leading www.
        domain   = hostname.removeprefix('www.')
        if not domain:
            return 'unknown'

        if any(domain.endswith(s) for s in _SOCIAL_ONTOLOGY_DOMAINS):
            # Social: domain + up to 2 clean path segments
            segments = [s for s in parsed.path.split('/') if s]  # drop empties
            key_parts = [domain] + segments[:2]
            return '/'.join(key_parts)
        else:
            return domain
    except Exception:
        return 'unknown'


def validate_and_update_lead(payload_dict: dict, doc_ref) -> bool:
    """
    Universal Data Contract gatekeeper.

    Validates payload_dict against LeadPayload (Pydantic v2).
    On success  → writes validated, clean dict to Firestore. Returns True.
    On failure  → Dead Letter pattern: writes raw payload with
                  status='schema_violation' for debugging. Returns False.

    Also upserts the ontology_map on every successful write:
      - Creates the document if the base_path is new (total_yield=0, weight=1.0)
      - Increments total_yield by 1 on every valid lead

    Both dispatch() and finalize() must pass their final dicts through here
    instead of calling doc_ref.update() directly.

    Uses set(merge=True) universally:
      - Cartographer: updates an existing stub document (equivalent to update())
      - Autonomous Engine: creates a fresh document without any prior stub
    """
    try:
        validated = LeadPayload(**payload_dict)
        doc_ref.set(validated.to_firestore_dict(), merge=True)
        print(f"[CONTRACT] ✓ Validated lead {payload_dict.get('id', '?')} "
              f"(engine={validated.origin_engine}, score={validated.score})")

        # ── Ontology Map upsert (Phase 1) ──────────────────────────────
        # CORRECTNESS: read-then-write to preserve RLHF-trained baseline_weight.
        # merge=True with baseline_weight: 1.0 would silently reset RLHF weights
        # every time a domain produces a new lead. The read adds 1 Firestore op
        # but protects ontology signal integrity.
        source_url = payload_dict.get('source_url', '')
        base_path  = parse_base_path(source_url)
        if base_path and base_path != 'unknown':
            try:
                ontology_ref  = db.collection('ontology_map').document(base_path)
                ontology_snap = ontology_ref.get()
                if ontology_snap.exists:
                    # Domain known: only update yield + timestamp (preserve RLHF weight)
                    ontology_ref.update({
                        'total_yield': firestore.Increment(1),
                        'last_seen':   firestore.SERVER_TIMESTAMP,
                    })
                else:
                    # New domain: initialize with neutral baseline_weight
                    ontology_ref.set({
                        'base_path':       base_path,
                        'total_yield':     1,
                        'baseline_weight': 1.0,
                        'last_seen':       firestore.SERVER_TIMESTAMP,
                    })
                print(f"[ONTOLOGY] Upserted {base_path}")
            except Exception as oe:
                print(f"[ONTOLOGY] Upsert failed for {base_path}: {oe}")

        return True
    except ValidationError as ve:
        print(f"[CONTRACT] ✗ Schema violation for {payload_dict.get('id', '?')}: {ve}")
        # Dead Letter: preserve raw payload but quarantine it from the main feed
        dead_letter = dict(payload_dict)
        dead_letter["status"]            = "schema_violation"
        dead_letter["schema_error"]      = str(ve)
        dead_letter["schema_error_time"] = firestore.SERVER_TIMESTAMP
        try:
            doc_ref.set(dead_letter, merge=True)  # set(merge=True): safe for stub or fresh doc
        except Exception as dl_e:
            print(f"[CONTRACT] Dead-letter write also failed: {dl_e}")
        return False

# ---------------------------------------------------------------------------
# V14: SYNAPTIC ROUTER — Vector-to-Platform Dork Map
# Maps a sourcing vector string to platform-specific Google Search operators.
# Injected into generate_smart_query() to dynamically tailor search topology.
# ---------------------------------------------------------------------------
VECTOR_PLATFORM_MAP = {
    "Social/Forum Listening": [
        "site:reddit.com",
        "site:quora.com",
        "site:facebook.com/groups"
    ],
    "Review Hijacking": [
        "site:tripadvisor.com",
        "site:trustpilot.com"
    ],
    "Maps/GMB Targeting": [
        "site:google.com/maps",
        '"near me"'
    ],
    "Classic B2B": [
        "site:linkedin.com/company"
    ]
}

# ---------------------------------------------------------------------------
# V20: UNIFIED QUERY BRAIN — P1+P2+P3 consolidated into a single Gemini call.
# Schema enforces all three output arrays in one round-trip, cutting input
# token submissions by ~65% vs the legacy 3-call chain.
# RLHF injection (historical_phrases → AND-suffix) is fully preserved.
# ---------------------------------------------------------------------------
_QUERY_BRAIN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "historical_phrases": {
            "type": "ARRAY",
            "description": "Exactly 3 short B2B trend phrases mined from historical lead pain_points. Empty array if no historical data supplied.",
            "items": {"type": "STRING"}
        },
        "symptom_dorks": {
            "type": "ARRAY",
            "description": "Exactly 3 Google Search operator strings targeting prospects publicly experiencing the user's solved problem. Each string must be a complete, ready-to-use search query including site: operators and negative keywords.",
            "items": {"type": "STRING"}
        },
        "translated_queries": {
            "type": "ARRAY",
            "description": "Exactly 3 natural-language, platform-native conversational queries humans type on the chosen sourcing vector. Empty array if no keywords supplied.",
            "items": {"type": "STRING"}
        }
    },
    "required": ["historical_phrases", "symptom_dorks", "translated_queries"]
}


def generate_smart_query(user_keywords, tenant_id, bio, sourcing_vector=None, persona_category=None):
    """
    V21: Hybrid Starter Motor — Confidence Threshold Router.

    Routes query generation between two modes:
      STATISTICAL  (Confidence ≥ threshold): Constructs Serper queries locally
                   using top N-grams + domains from swarm_analytics.Intent_Keywords.
                   Zero Gemini calls on this path.
      GEMINI_FALLBACK (Confidence < threshold): Falls back to the unified Gemini
                   prompt (legacy V20 behaviour). Never dropped as a fallback path.

    The threshold is read from system_config/router.intent_confidence_threshold
    (Firestore). Default: 1000. Override in production without redeployment.
    """
    # ── Step 1: Fetch RLHF history context (Firestore read — no Gemini call) ──
    pain_points: list = []
    try:
        query = db.collection("leads").where("tenant_id", "==", tenant_id).where("status", "in", ["contacted", "converted"]).limit(20)
        docs = list(query.stream())
        if not docs:
            query = db.collection("leads").where("status", "in", ["contacted", "converted"]).limit(20)
            docs = list(query.stream())
        pain_points = [d.to_dict().get("pain_point", "") for d in docs if d.to_dict().get("pain_point")]
    except Exception as e:
        print(f"[QUERY BRAIN] RLHF history fetch failed: {e}")

    # ── Step 2: Resolve persona_category for router key ————————————————
    _p_cat = (persona_category or "general").strip() or "general"

    # ── Step 2a: Confidence Threshold Router ─────────────────────────────
    # SELECT SUM(yield_weight) with a 3-second hard timeout.
    # Falls back to GEMINI_FALLBACK on any error or timeout.
    _CONF_THRESHOLD = 1000.0  # default; overridden from Firestore below
    try:
        _router_cfg    = db.collection("system_config").document("router").get().to_dict() or {}
        _CONF_THRESHOLD = float(_router_cfg.get("intent_confidence_threshold", 1000))
    except Exception:
        pass  # Non-fatal: use default

    _confidence    = 0.0
    _router_mode   = "GEMINI_FALLBACK"  # safe default

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _rex:
            def _query_confidence():
                _bq = _bq_lib.Client(project=PROJECT_ID)
                _q  = """
                    SELECT COALESCE(SUM(yield_weight), 0) AS total_confidence
                    FROM `{project}.swarm_analytics.Intent_Keywords`
                    WHERE (tenant_id = @tid OR tenant_id = 'GLOBAL')
                      AND persona_category = @cat
                """.format(project=PROJECT_ID)
                _jc = _bq.query(
                    _q,
                    job_config=_bq_lib.QueryJobConfig(
                        query_parameters=[
                            _bq_lib.ScalarQueryParameter("tid", "STRING", tenant_id),
                            _bq_lib.ScalarQueryParameter("cat", "STRING", _p_cat),
                        ]
                    )
                )
                rows = list(_jc.result(timeout=3))
                return float(rows[0]["total_confidence"]) if rows else 0.0

            _fut_conf = _rex.submit(_query_confidence)
            _confidence = _fut_conf.result(timeout=3.0)

        if _confidence >= _CONF_THRESHOLD:
            _router_mode = "STATISTICAL"
    except concurrent.futures.TimeoutError:
        print(f"[QUERY BRAIN] Confidence BQ timeout — falling back to Gemini.")
    except Exception as _ce:
        print(f"[QUERY BRAIN] Confidence query failed (non-fatal): {_ce}")

    print(f"[QUERY BRAIN] Persona='{_p_cat}' | Mode={_router_mode} | Confidence={int(_confidence)}")

    # ── Step 3a: STATISTICAL BUILD (Confidence ≥ threshold) ──────────────
    # Fetches top-3 N-grams and top-2 domains from BQ. Constructs queries
    # locally without calling Gemini. Zero LLM cost on this hot path.
    historical_phrases: list = []
    symptom_dorks: list      = []
    translated_queries: list = []

    if _router_mode == "STATISTICAL":
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _sex:
                def _fetch_stat_signals():
                    _bq = _bq_lib.Client(project=PROJECT_ID)
                    # Top-3 N-grams by yield_weight for query phrase injection
                    _ng_q = """
                        SELECT n_gram, SUM(yield_weight) AS w
                        FROM `{project}.swarm_analytics.Intent_Keywords`
                        WHERE (tenant_id = @tid OR tenant_id = 'GLOBAL')
                          AND persona_category = @cat
                        GROUP BY n_gram
                        ORDER BY w DESC
                        LIMIT 3
                    """.format(project=PROJECT_ID)
                    _ng_job = _bq.query(
                        _ng_q,
                        job_config=_bq_lib.QueryJobConfig(
                            query_parameters=[
                                _bq_lib.ScalarQueryParameter("tid", "STRING", tenant_id),
                                _bq_lib.ScalarQueryParameter("cat", "STRING", _p_cat),
                            ]
                        )
                    )
                    _ng_rows = list(_ng_job.result(timeout=3))
                    top_ngrams = [r["n_gram"] for r in _ng_rows if r["n_gram"]]

                    # Top-2 domains from Negative_Signals are already excluded via -site:
                    # For STATISTICAL dorks we use the top-2 *ontology wins* (future phase).
                    # For now, build symptom_dorks from the top N-grams directly.
                    return top_ngrams

                _fut_stat  = _sex.submit(_fetch_stat_signals)
                _top_ngrams = _fut_stat.result(timeout=3.5)

            if _top_ngrams:
                # historical_phrases: top N-grams surface well-converting language patterns
                historical_phrases = _top_ngrams[:3]
                # translated_queries: construct operator strings from top N-gram phrases
                keyword_str = ", ".join(user_keywords) if user_keywords else ""
                for ng in _top_ngrams[:3]:
                    if keyword_str:
                        translated_queries.append(f'"{ng}" AND ({keyword_str})')
                    else:
                        translated_queries.append(f'"{ng}"')
                # symptom_dorks: combine N-gram with bio signal
                if bio:
                    symptom_dorks = [
                        f'site:linkedin.com "{_top_ngrams[0]}" AND ("{bio[:40]}")'
                        if len(_top_ngrams) > 0 else "",
                        f'site:reddit.com "{_top_ngrams[0]}"'
                        if len(_top_ngrams) > 0 else "",
                    ]
                    symptom_dorks = [s for s in symptom_dorks if s]
                print(f"[QUERY BRAIN] STATISTICAL: built {len(translated_queries)} queries "
                      f"from N-grams {_top_ngrams}")
            else:
                # No signals yet despite confidence score; degrade gracefully to Gemini
                _router_mode = "GEMINI_FALLBACK"
                print("[QUERY BRAIN] STATISTICAL: no N-grams returned — degrading to Gemini.")

        except Exception as _se:
            _router_mode = "GEMINI_FALLBACK"
            print(f"[QUERY BRAIN] STATISTICAL build failed — falling back to Gemini: {_se}")

    # ── Step 3b: GEMINI FALLBACK (Confidence < threshold or STATISTICAL failed) ─
    if _router_mode == "GEMINI_FALLBACK":
        keyword_str  = ", ".join(user_keywords) if user_keywords else ""
        vector_label = sourcing_vector or "Classic B2B"
        history_ctx  = json.dumps(pain_points) if pain_points else "[]"

        unified_prompt = f"""You are the Sideio Query Brain. You will perform ALL THREE tasks below in a single response.

# TASK 1 — RLHF HISTORICAL MINING
Analyze these successful lead pain_point strings from previously converted leads.
Extract exactly 3 short, conceptual B2B trend phrases identifying the highest-value patterns across all entries.
If the data list is empty, return an empty array for historical_phrases.
Data: {history_ctx}

# TASK 2 — SYMPTOM DORKING
The user solves this business problem: '{bio}'.
Generate exactly 3 highly specific Google Search operator strings to find targets PUBLICLY EXPERIENCING this problem.
Rule 1: At least one query MUST target social/professional networks using 'site:linkedin.com', 'site:facebook.com', or 'site:reddit.com'.
Rule 2: Every query MUST append negative keywords to exclude noise (e.g. '-shop -cart -amazon -wiki -jobs -careers').
If no bio is provided, return an empty array for symptom_dorks.

# TASK 3 — INTENT EXPANSION
The user is targeting this audience: '{keyword_str}'.
Current sourcing vector: '{vector_label}'.
Translate this audience into exactly 3 natural-language, conversational queries real humans type on this specific platform.
Platform rules:
- Social/Forum Listening: first-person or question-style forum posts.
- Review Hijacking: complaint/review search phrases (e.g. 'problems with', 'looking for alternative to').
- Maps/GMB Targeting: geo-intent phrases (e.g. 'best [service] near me').
- Classic B2B: professional industry terminology.
If no audience keywords are provided, return an empty array for translated_queries.

Return ONLY the JSON object matching the schema. No explanation, no markdown."""

        try:
            result = call_gemini_2_5(
                unified_prompt,
                expect_json=True,
                response_schema=_QUERY_BRAIN_SCHEMA
            )
            if isinstance(result, dict):
                historical_phrases  = [p.strip() for p in result.get("historical_phrases",  []) if isinstance(p, str) and p.strip()][:3]
                symptom_dorks       = [s.strip() for s in result.get("symptom_dorks",       []) if isinstance(s, str) and s.strip()][:3]
                translated_queries  = [q.strip() for q in result.get("translated_queries",  []) if isinstance(q, str) and q.strip()][:3]
                print(f"[QUERY BRAIN] Gemini fallback OK: hist={len(historical_phrases)} symp={len(symptom_dorks)} tq={len(translated_queries)}")
        except Exception as e:
            print(f"[QUERY BRAIN] Gemini call failed: {e}. Falling back to literal keywords.")

    # ── Step 4: Assemble Serper query strings ──────────────────────────────────
    blacklist = "-wiki -jobs -careers -investors -support -\"login\" -www.zoominfo.com -www.ibm.com -www.amazon.com"

    # ── Step 4a: Negative Signal Shield injection ─────────────────────────────
    # Fetches Competitor / Author domains + entity names from BQ (3s hard timeout).
    # Empty on first run or BQ failure — pipeline continues unmodified.
    _shield_domains, _shield_entities = _fetch_neg_shield(tenant_id)

    # Append -site:<domain> operators for suppressed competitor/author domains
    if _shield_domains:
        _site_ops = " ".join(f"-site:{d}" for d in _shield_domains[:15] if d)
        blacklist  = f"{blacklist} {_site_ops}"

    # Append -intitle:"Entity Name" operators for suppressed entity names
    if _shield_entities:
        _title_ops = " ".join(f'-intitle:"{e}"' for e in _shield_entities[:10] if e)
        blacklist   = f"{blacklist} {_title_ops}"

    if _shield_domains or _shield_entities:
        print(f"[NEG SHIELD] Injected {len(_shield_domains)} domain blocks + "
              f"{len(_shield_entities)} title blocks into Serper query")

    # RLHF injection: historical trend phrases appended as AND-suffix
    historical_str = ""
    if historical_phrases:
        phrases_escaped = [f'"{p}"' for p in historical_phrases[:3]]
        historical_str  = " AND (" + " OR ".join(phrases_escaped) + ")"

    smart_queries: list = []

    # Translated intent queries
    _kw_str = ", ".join(user_keywords) if user_keywords else ""
    _vec_label = sourcing_vector or "Classic B2B"
    if translated_queries:
        for tq in translated_queries:
            smart_queries.append(f'"{tq}"{historical_str} {blacklist}')
        print(f"[QUERY BRAIN] {len(translated_queries)} queries assembled | mode={_router_mode} | vector='{_vec_label}'")
    elif _kw_str:
        # Hard fallback: literal keywords if both router paths produced nothing
        for kw in (user_keywords or []):
            smart_queries.append(f'("{kw}"){historical_str} {blacklist}')

    # Symptom dorks (P2 output)
    for sd in symptom_dorks:
        smart_queries.append(f'{sd} {blacklist}')

    # V14: Vector-specific platform dorks appended last
    if sourcing_vector and sourcing_vector in VECTOR_PLATFORM_MAP:
        for platform_dork in VECTOR_PLATFORM_MAP[sourcing_vector]:
            smart_queries.append(f'{platform_dork}{historical_str} {blacklist}')
        print(f"[SYNAPTIC ROUTER] Appended {len(VECTOR_PLATFORM_MAP[sourcing_vector])} platform dorks for vector: '{sourcing_vector}'")

    return smart_queries

def filter_serper_noise(serper_results):
    clean_results = []
    enterprise_domains = ["ibm.com", "amazon.com", "microsoft.com", "g2.com", "capterra.com", "zoominfo.com"]
    noise_paths = ["/legal", "/pricing", "/docs", "/author/", "/login"]
    noise_snippets = ["sign in", "access denied", "forgot password", "please enable cookies"]
    
    for r in serper_results:
        link = r.get("link", "").lower()
        snippet = r.get("snippet", "").lower()
        if any(d in link for d in enterprise_domains): continue
        if any(p in link for p in noise_paths): continue
        if any(s in snippet for s in noise_snippets): continue
        clean_results.append(r)
        
    return clean_results

def pre_filter_gemini(snippets, bio, location_target):
    """
    V14: Returns a tiered dict {"High": [...urls], "Medium": [...urls]}.
    Low-confidence URLs are silently dropped.
    Uses strict JSON schema enforcement — no flat URL list hallucinations.
    """
    if not snippets:
        return {"High": [], "Medium": []}

    tiering_schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "url":             {"type": "STRING"},
                "confidence_tier": {"type": "STRING", "enum": ["High", "Medium", "Low"]},
                "reason":          {"type": "STRING"}
            },
            "required": ["url", "confidence_tier", "reason"]
        }
    }

    prompt = f"""CONFIDENCE TIERING GATE: Evaluate each URL snippet against the user's business context.

USER BIO: '{bio}'
TARGET LOCATION: '{location_target}'

# STEP 1 — PERSONA CLASSIFICATION (execute this first, before evaluating any URL)
Read the USER BIO and classify the user as:
- B2B Vendor: sells tools, services, or software TO businesses or professionals.
- B2C Service Provider: sells help, coaching, advice, or services DIRECTLY to individual consumers or students.

# STEP 2 — PERSONA-LOCKED TIERING RULES
Apply the correct ruleset based on the persona you classified:

IF B2B Vendor:
- High: The URL belongs to a business or professional entity that is EXPLICITLY experiencing the pain point the user solves, correct intent, correct geo.
- Medium: Ambiguous intent or geo, but clearly a relevant industry vertical.
- Low: Competitor, manufacturer, directory, aggregator (JustDial, Alibaba, Yelp, IndiaMart), SEO blog, D2C retail.

IF B2C Service Provider:
- High: The URL or snippet belongs to an INDIVIDUAL (not a company) who is EXPLICITLY expressing the pain point, frustration, or need in their own words.
- Medium: Ambiguous individual, or individual whose need is implied but not explicit.
- Low: Agency, university admin page, corporate entity, competitor, directory, or any organisational URL. Route ALL institutional/agency results to Low — B2C providers target individuals, not organisations.

# STEP 3 — UNIVERSAL RULES (always apply)
SOCIAL PLATFORM RULE: For Reddit, Quora, Facebook, LinkedIn — evaluate the SPECIFIC POST or COMMENT INTENT, not the platform. An individual asking for help = High/Medium. Platform homepage = Low.
GEO RULE: If a target explicitly serves a different region than '{location_target}', mark as Low.

Snippets to evaluate:
{json.dumps(snippets)}"""

    try:
        tiered_results = call_gemini_2_5(prompt, expect_json=True, response_schema=tiering_schema)
        if not isinstance(tiered_results, list):
            raise ValueError("Expected list from tiering gate")
    except Exception as e:
        print(f"[TIER GATE] Gemini tiering failed: {e}. Falling back to empty result.")
        return {"High": [], "Medium": []}

    output = {"High": [], "Medium": []}
    for item in tiered_results:
        tier = item.get("confidence_tier", "Low")
        url  = item.get("url", "").strip()
        if not url.startswith("http"):
            continue
        if tier == "High":
            output["High"].append(url)
        elif tier == "Medium":
            output["Medium"].append(url)
        # Low: silently drop

    print(f"[TIER GATE] High={len(output['High'])}, Medium={len(output['Medium'])}, Low dropped.")
    return output

def extract_root_domain(url):
    try:
        netloc = urlparse(url).netloc.lower()
        if not netloc:
             netloc = urlparse('http://' + url).netloc.lower()
        netloc = netloc.replace('www.', '')
        return netloc
    except:
        return ""


# =============================================================================
# NEGATIVE SIGNAL SHIELD — Serper Query Suppressor
#
# Fetches the top 20 suppressed domains + entity names from the
# swarm_analytics.Negative_Signals table (written by the orchestrator's
# RLHF rejection hook on every Competitor / Author rejection).
#
# Design contract:
#   • Hard 3-second max timeout — BQ latency NEVER extends the scraping loop.
#   • Returns ([], []) on ANY failure — the pipeline degrades gracefully.
#   • Deduplicates domains and entity names before returning.
#   • Result is injected as -site: / -intitle: operators into the blacklist
#     assembled in generate_smart_query(), not passed to the LLM.
# =============================================================================

def _fetch_neg_shield(tenant_id: str) -> tuple:
    """
    Returns (blocked_domains: list[str], blocked_entities: list[str]).
    Both lists are deduplicated and capped at 20 entries each.
    Falls back to ([], []) on any error or timeout.
    """
    try:
        bq = _bq_lib.Client(project=PROJECT_ID)

        # Parameterised query — prevents SQL injection from tenant_id.
        # Scoped to the rejecting tenant PLUS cross-tenant global signals
        # (tenant_id = 'GLOBAL') written by L0 admin overrides.
        query = """
            SELECT root_domain, entity_name
            FROM `{project}.swarm_analytics.Negative_Signals`
            WHERE (tenant_id = @tenant_id OR tenant_id = 'GLOBAL')
              AND root_domain IS NOT NULL
            GROUP BY root_domain, entity_name
            ORDER BY COUNT(*) DESC
            LIMIT 20
        """.format(project=PROJECT_ID)

        job_config = _bq_lib.QueryJobConfig(
            query_parameters=[
                _bq_lib.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            ],
            # Enforce a 3-second wall-clock timeout via the BQ Jobs API.
            # If BQ is slow, we raise TimeoutError and return empty lists.
        )

        # Run BQ query in a thread with a strict 3s wall-clock timeout.
        # concurrent.futures.ThreadPoolExecutor reuse avoids per-call overhead.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(bq.query, query, job_config=job_config)
            job  = _fut.result(timeout=3.0)

        rows = list(job.result(timeout=3.0))

        blocked_domains  = list({r["root_domain"] for r in rows if r["root_domain"]})
        blocked_entities = list({r["entity_name"]  for r in rows if r["entity_name"]})

        print(f"[NEG SHIELD] Loaded {len(blocked_domains)} domains, "
              f"{len(blocked_entities)} entities for tenant={tenant_id[:8]}...")
        return blocked_domains, blocked_entities

    except concurrent.futures.TimeoutError:
        print("[NEG SHIELD] BQ timeout (>3s) — proceeding without shield.")
        return [], []
    except Exception as _e:
        print(f"[NEG SHIELD] Fetch failed (non-fatal): {_e}")
        return [], []

def deep_context_serper_dork(domain, tenant_id, sourcing_vector="Classic B2B", source_url=""):
    """
    V14.4 HOTFIX: Enrichment Gatekeeper.
    Skips ALL Serper calls for:
      1. Social/UGC domains (reddit, facebook, instagram, youtube, etc.)
         EXCEPTION: linkedin.com/company/ URLs are strict B2B — always enrich.
      2. B2C sourcing vectors (no company LinkedIn / Naukri job listings exist)
    """
    if not domain: return "", False

    # ── FIX 3: LinkedIn Company Exception ────────────────────────────────────
    # linkedin.com/company/ URLs are pure B2B signals (company profiles).
    # They must NOT be blocked by the social blacklist — they require full
    # company enrichment. Check the source_url path before evaluating domain.
    _source_lower = (source_url or "").lower()
    _is_linkedin_company = "linkedin.com/company/" in _source_lower

    # ── GATEKEEPER: Social Domain Blacklist ───────────────────────────────────
    # linkedin.com/company/ bypasses this gate (handled above).
    ENRICHMENT_SOCIAL_BLACKLIST = [
        "reddit.com", "facebook.com", "instagram.com", "youtube.com",
        "linkedin.com", "quora.com", "twitter.com", "x.com", "medium.com"
    ]
    cleaned_domain = domain.lower().replace("www.", "")
    if not _is_linkedin_company:
        for blocked in ENRICHMENT_SOCIAL_BLACKLIST:
            if blocked in cleaned_domain:
                print(f"[ENRICHMENT] Bypassing company enrichment for B2C/Social domain: {domain}")
                return "", False
    else:
        print(f"[ENRICHMENT] linkedin.com/company/ detected — treating as strict B2B: {source_url}")

    # ── GATEKEEPER: B2C Persona Bypass ───────────────────────────────────────
    # B2C vectors do not have company LinkedIn pages or Naukri job listings.
    # Running enrichment searches against them burns credits with zero signal.
    B2C_VECTORS = [
        "Reddit B2C", "Quora B2C", "Google Maps B2C",
        "TripAdvisor B2C", "YouTube B2C", "Facebook Groups B2C"
    ]
    if sourcing_vector in B2C_VECTORS:
        print(f"[ENRICHMENT] Bypassing company enrichment for B2C/Social domain: {domain} (vector={sourcing_vector})")
        return "", False

    api_key = get_secret(SERPER_API_KEY_NAME).strip()
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    context_data = []

    def fetch_serper(url, payload):
        try:
             db.collection("usage_metrics").document(tenant_id).set({"serper_searches": firestore.Increment(1)}, merge=True)
             resp = httpx.post(url, headers=headers, json=payload, timeout=15)
             if resp.status_code == 200: return resp.json()
        except:
             pass
        return {}

    # Vector A: GMB / Local
    gmb_data = fetch_serper("https://google.serper.dev/places", {"q": domain, "num": 3})
    for place in gmb_data.get("places", []):
         context_data.append(f"[GMB] Rating: {place.get('rating', 'N/A')}, Reviews: {place.get('ratingCount', 'N/A')}, Address: {place.get('address', 'N/A')}")

    # Vector B: Social
    social_query = f"site:linkedin.com/company OR site:facebook.com \"{domain}\""
    social_data = fetch_serper("https://google.serper.dev/search", {"q": social_query, "num": 3})
    for org in social_data.get("organic", []):
         context_data.append(f"[SOCIAL] {org.get('snippet', '')}")

    # Vector C: Hiring Intent
    hiring_query = f"site:naukri.com/job-listings OR site:instahyre.com/job OR site:linkedin.com/jobs OR site:indeed.com/cmp \"{domain}\""
    hiring_data = fetch_serper("https://google.serper.dev/search", {"q": hiring_query, "num": 3})

    hiring_signatures = ["we are hiring", "job description", "apply today", "openings", "careers", "looking for", "lakh", "lpa", "fresher"]
    native_hiring_intent_found = False

    for job in hiring_data.get("organic", []):
         snippet_lower = job.get('snippet', '').lower()
         context_data.append(f"[HIRING] {snippet_lower}")
         if any(sig in snippet_lower for sig in hiring_signatures):
             native_hiring_intent_found = True

    return " | ".join(context_data)[:3000], native_hiring_intent_found


def scrape_url(url):
    # Lightweight scrape
    try:
        resp = httpx.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Smart WAF Heuristics Check (Cloudflare / Incapsula)
        title_str = soup.title.string.lower() if soup.title and soup.title.string else ""
        body_str = soup.get_text(separator=' ', strip=True)[:2000].lower()
        search_blob = f"{title_str} {body_str}"
        
        waf_fingerprints = ["just a moment...", "attention required!", "access denied", "cloudflare"]
        for fingerprint in waf_fingerprints:
            if fingerprint in search_blob:
                raise ValueError(f"WAF block explicitly detected ({fingerprint})")
                
        # Tech Stack X-Ray (Zero Cost)
        html_blob = str(soup).lower()
        tech_signatures = {
             "wordpress": "wp-content",
             "shopify": "cdn.shopify.com",
             "stripe": "js.stripe.com",
             "react": "react-root",
             "hubspot": "js.hs-scripts.com",
             "salesforce": "force.com",
             "google analytics": "google-analytics.com",
             "segment": "cdn.segment.com",
             "intercom": "widget.intercom.io"
        }
        found_tech = [name for name, sig in tech_signatures.items() if sig in html_blob]
                
        text = soup.get_text(separator=' ', strip=True)
        if len(text) < 500: # Potential JS Heavy page
            raise ValueError("Too little content, likely JS framework")
            
        extracted_emails = list({a['href'].replace('mailto:', '').split('?')[0].strip() for a in soup.find_all('a', href=True) if a['href'].startswith('mailto:')})
        extracted_phones = list({a['href'].replace('tel:', '').strip() for a in soup.find_all('a', href=True) if a['href'].startswith('tel:')})

        return safe_truncate(text), found_tech, extracted_emails, extracted_phones # Strict truncation
    except Exception as e:
        print(f"Fallback to heavy scraper for {url} due to {str(e)}")
        raise ValueError("DEFERRED")

def final_score_and_dm(text, active_campaigns, context_payload, tech_stack, historical_dms=None, source_url=None):
    """
    V18 Multi-Campaign Swarm: Evaluates lead against ALL active campaigns.
    """
    social_domains = ["reddit.com", "quora.com", "facebook.com", "linkedin.com", "instagram.com"]
    is_social_source = source_url and any(d in source_url.lower() for d in social_domains)
    social_platform = "other"
    if source_url:
        if "reddit.com"    in source_url: social_platform = "reddit"
        elif "quora.com"   in source_url: social_platform = "other"
        elif "facebook.com" in source_url: social_platform = "facebook"
        elif "linkedin.com" in source_url: social_platform = "linkedin"
        elif "instagram.com" in source_url: social_platform = "instagram"

    social_uri_rule = ""
    if is_social_source:
        social_uri_rule = f"""
SOCIAL PROFILE URI RULE (MANDATORY — this source is from a social/forum platform):
The source URL '{source_url}' originates from a social network or forum.
You MUST extract the URL of the original poster's user profile from the DOM text.
Map this profile link to the contact_endpoints array using the correct platform enum ('{social_platform}').
Do NOT return an empty contact_endpoints array if a user profile link is present in the text.
Look for patterns like '/u/', '/user/', '/profile/', '@username', or any author attribution link."""

    import json

    def _resolve_campaign_bio(c: dict) -> str:
        """Resolve the effective bio for a campaign in the Gemini scoring prompt.
        Priority: persona_bio > effective_bio > campaign_focus > raw bio.
        Strips CHILD_CAMPAIGN_OVERRIDE sentinel."""
        if c.get("persona_id") and c.get("persona_bio"):
            return c["persona_bio"]
        raw = c.get("bio", "")
        if raw == "CHILD_CAMPAIGN_OVERRIDE":
            return (c.get("effective_bio") or
                    c.get("campaign_focus") or
                    c.get("pain_point") or "")
        return raw

    campaigns_str = json.dumps([{
        "campaign_id": c.get("id", c.get("name")),
        "bio":         _resolve_campaign_bio(c),
        "keywords":    c.get("persona_keywords") or c.get("keywords", "")
    } for c in active_campaigns], indent=2)

    prompt = f"""You are a Dynamic Intent Analyzer evaluating a lead against multiple campaigns.
Your evaluation mode adapts based on the source context:
- SOURCE TYPE: {'SOCIAL/FORUM POST' if is_social_source else 'COMPANY WEBSITE/FORMAL PAGE'}
- PLATFORM: {social_platform.upper()}

# STEP 1 — CROSS-POLLINATION EVALUATION MATRIX
Read the text DOM and evaluate it against EACH of these active campaigns:
{campaigns_str}

Score the lead (1-10) for EACH campaign. Return only campaigns where score >= 4.

## SCORING MODE — applies based on SOURCE TYPE:

[IF COMPANY WEBSITE / FORMAL PAGE]
Base the score on how well the target's business, industry, and explicit needs match the campaign's bio and keywords.
Require clear B2B signals: industry fit, company size, tech stack indicators, or stated business problems.

[IF SOCIAL/FORUM POST — Reddit, Facebook, Quora, or any forum]
IGNORE the lack of formal company structure, B2B keywords, or domain authority.
Base the score PURELY on the intensity and specificity of the pain point expressed in the post or comment.
A score of 8-10 is valid for a person posting a raw, urgent, personal frustration that directly matches a campaign's solution.
A score of 4-6 is valid for general curiosity or exploratory questions about the problem space.
Do NOT penalise for missing company details — this is expected on social platforms.

# STEP 2 — OUTREACH COPILOT DRAFT
Identify the campaign with the HIGHEST match score (primary pain point).
If other campaigns also matched (score >= 4), incorporate them as secondary "Shield" value.
NEVER pitch a bundle; lead with one solution, reinforce with secondary benefits.

## TONE MODE — adapts based on {social_platform}:

[IF {social_platform} is 'linkedin' OR source is a company website]
Use the direct, professional "Spear & Shield" pitch.
Tone: warm, confident, peer-to-peer. No fluff, no generic greetings.
Open with the specific pain signal you detected, then state the solution clearly.

[IF {social_platform} is 'reddit' OR 'facebook' OR 'other' (forums/communities)]
Do NOT pitch immediately. This is a community-native context.
Write a SHORT, casual, empathetic opening message that:
  1. Acknowledges the exact frustration they posted about in their own language
  2. Asks a single, open-ended question to invite a reply (conversation-first)
  3. Only hints at a potential solution in the final sentence — do not hard-sell
Tone: like a helpful community member who genuinely gets it, not a salesperson.
Length: max 3 sentences total.

# STEP 3 — EXTRACTION RULES
For hiring_intent_found: Return ONLY 'Yes' or 'No'. No explanation.

For contact_endpoints: Extract ALL reachable contact surfaces explicitly present in the text.
Each endpoint must have a 'platform' from the strict enum and a 'uri'.
URI PROTOCOL RULE (MANDATORY): Every URI MUST include its full protocol prefix:
- Web profile URLs: must start with https://
- Email addresses: return ONLY the email string
- Phone numbers: return ONLY the digits/number string
- If a URI would be a naked domain, DO NOT include it.
REDDIT TARGETING RULE: Extract the href of the original poster's attribution link.
PHONE DEDUPLICATION: Max 2 numbers.
Do NOT invent contacts. Only extract what is explicitly present.
{social_uri_rule}

For intent_signal: Write one precise sentence explaining the specific signal proving they need the solution.

## SCHEMA GRACE FOR SOCIAL LEADS:
If the source is a social/forum post, it is CORRECT and EXPECTED to output:
- decision_maker_title: "Unknown" (individuals on social platforms rarely have formal titles)
- company_size_tier: "Unknown" (no company affiliation required for social leads)
- company_name: the poster's username or "Unknown" if not determinable
Never mark these as failures — social leads are scored on pain intensity, not B2B formality.

CONTEXTUAL DORKING DATA:
{context_payload}

DETECTED TECH STACK:
{', '.join(tech_stack) if tech_stack else 'None extracted'}
"""
    if historical_dms:
        prompt += f"\nPast successful converted messages (match tone and length strictly): {historical_dms}\n"
    prompt += f"\nUsing all context, output your evaluation and drafting.\n\nText DOM:\n{text}"

    sys_inst = (
        "You are a Dynamic Intent Analyzer with adaptive persona intelligence. "
        "When evaluating COMPANY WEBSITE sources: act as an elite B2B profiler — "
        "demand formal business signals, extract decision-makers, and score based on industry fit. "
        "When evaluating SOCIAL/FORUM POST sources: act as a community intelligence analyst — "
        "score purely on pain point intensity and emotional urgency, ignore missing B2B structure, "
        "and draft empathetic conversation-starter outreach rather than pitches. "
        "Never hallucinate contacts. Never drop a lead solely because it lacks a company domain."
    )


    schema = {
        "type": "OBJECT",
        "properties": {
            "matched_campaigns": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "campaign_id": {"type": "STRING"},
                        "raw_score": {"type": "INTEGER"}
                    },
                    "required": ["campaign_id", "raw_score"]
                }
            },
            "dm": {
                "type": "STRING",
                "description": "Drafted Spear & Shield outreach message. Output exact string 'N/A' if insufficient data."
            },
            "pain_point": {
                "type": "STRING",
                "description": "Specific pain point extracted. Output 'N/A' if insufficient data."
            },
            "icebreaker_angle": {
                "type": "STRING",
                "description": "The tactical angle for the icebreaker. Output 'N/A' if insufficient data."
            },
            "intent_signal": {
                "type": "STRING",
                "description": "One precise sentence: the specific signal in the content proving they need the user's solution."
            },
            "hiring_intent_found": {
                "type": "STRING",
                "enum": ["Yes", "No"]
            },
            "tech_stack_found": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Only verified software technologies found in the HTML. No internal notes."
            },
            "contact_endpoints": {
                "type": "ARRAY",
                "description": "ALL reachable contact surfaces found. Only extract explicitly present contacts.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "platform": {
                            "type": "STRING",
                            "enum": ["instagram", "reddit", "whatsapp", "gmb", "email", "linkedin", "facebook", "other"]
                        },
                        "uri": {
                            "type": "STRING",
                            "description": "The email address, profile URL, phone number, map link, or handle."
                        }
                    },
                    "required": ["platform", "uri"]
                }
            },
            "decision_maker_name": {
                "type": "STRING",
                "description": "Specific human name found in the text. Use 'Unknown' if not found."
            },
            "decision_maker_title": {
                "type": "STRING",
                "description": "Title of the decision maker. Use 'Unknown' if not found."
            },
            "company_size_tier": {
                "type": "STRING",
                "description": "Strictly one of: 'Startup', 'Mid-Market', 'Enterprise', 'Unknown'."
            },
            "primary_objection_hypothesis": {
                "type": "STRING",
                "description": "One sentence: why they might reject the pitch based on their site context."
            },
            "company_name": {
                "type": "STRING",
                "description": "The legal or trading name of the company/brand. Output 'Unknown' if not determinable from the text."
            }
        },
        "required": [
            "matched_campaigns", "dm", "pain_point", "icebreaker_angle", "intent_signal",
            "hiring_intent_found", "tech_stack_found", "contact_endpoints",
            "decision_maker_name", "decision_maker_title",
            "company_size_tier", "primary_objection_hypothesis"
        ]
    }

    try:
        data = call_gemini_2_5(prompt, expect_json=True, response_schema=schema, system_instruction=sys_inst)
        if not isinstance(data, dict):
            raise ValueError("Parsed JSON is not a dictionary.")

        matched_campaigns = data.get("matched_campaigns", [])
        if not matched_campaigns:
            final_score = 0
            matched_ids = []
            trend_mapped = False
            highest_campaign = "Unknown"
        else:
            # Sort descending by raw_score
            matched_campaigns.sort(key=lambda x: x.get("raw_score", 0), reverse=True)
            base_score = float(matched_campaigns[0].get("raw_score", 0))
            highest_campaign = matched_campaigns[0].get("campaign_id", "Unknown")
            matched_ids = [str(c.get("campaign_id")) for c in matched_campaigns]

            multiplier = 1.0
            if len(matched_campaigns) == 2:
                multiplier = 1.3
            elif len(matched_campaigns) >= 3:
                multiplier = 1.6

            final_score = int(min(base_score * multiplier, 10.0))
            trend_mapped = len(matched_campaigns) >= 3

        return {
            "score":                        final_score,
            "matched_campaign_ids":         matched_ids,
            "trend_mapped":                 trend_mapped,
            "highest_campaign_id":          highest_campaign,
            "pain_point":                   data.get("pain_point", "Unknown"),
            "hiring_intent_found":          data.get("hiring_intent_found", "No"),
            "tech_stack_found":             data.get("tech_stack_found", []),
            "icebreaker_angle":             data.get("icebreaker_angle", ""),
            "intent_signal":                data.get("intent_signal", ""),
            "dm":                           data.get("dm", "Failed to generate DM"),
            "contact_endpoints":            data.get("contact_endpoints", []),
            "decision_maker_name":          data.get("decision_maker_name", "Unknown"),
            "decision_maker_title":         data.get("decision_maker_title", "Unknown"),
            "company_size_tier":            data.get("company_size_tier", "Unknown"),
            "primary_objection_hypothesis": data.get("primary_objection_hypothesis", "Unknown"),
            "company_name":                 data.get("company_name") or None,
        }
    except Exception as e:
        raise ValueError(f"LLM Parsing Failure: {e}")

@app.route("/produce", methods=["POST"])
def produce():
    """
    V14.4: THE PRODUCER — 24-Hour Serper Fetch Job.
    Runs Intent Translation (Step 3) and Serper Execution (Step 4).
    Deduplicates against global leads collection.
    Writes unprocessed URLs to campaigns/{id}.unprocessed_queue.
    Does NOT call the Gemini Gate. Halts here.

    Auth: Cloud Tasks MUST inject X-CloudTasks-QueueName header.
    Service is --no-allow-unauthenticated; Cloud Run IAM enforces OIDC.
    This header check is an additional defense-in-depth layer.
    """
    if not request.headers.get("X-CloudTasks-QueueName"):
        print(f"[PRODUCER] REJECTED: Missing X-CloudTasks-QueueName header. "
              f"Direct access not permitted. IP={request.remote_addr}")
        return jsonify({"error": "Forbidden", "message": "Task queue header required."}), 403

    import logging as _tlog
    _tlog.info("TRACE-1: produce() entered. Parsing payload...")
    lead_data = request.json or {}
    tenant_id   = lead_data.get("tenant_id")
    campaign_id = lead_data.get("campaign_id")
    _tlog.info("TRACE-2: payload parsed. tenant_id=%s campaign_id=%s", tenant_id, campaign_id)
    if not tenant_id or not campaign_id:
        print(f"[PRODUCER] CRITICAL: Missing tenant_id or campaign_id. Aborting.")
        return jsonify({"error": "Missing campaign_id or tenant_id"}), 400

    _tlog.info("TRACE-3: Acquiring Firestore db handle (lazy init)...")
    campaign_ref  = get_db().collection("campaigns").document(campaign_id)
    _tlog.info("TRACE-4: db handle ready. Fetching campaign document...")
    try:
        campaign = campaign_ref.get().to_dict() or {}
    except Exception as _cg_err:
        _tlog.error("TRACE-4-FAIL: campaign .get() failed: %s", _cg_err)
        return jsonify({"error": "Firestore timeout or error fetching campaign"}), 500
    _tlog.info("TRACE-5: Campaign doc fetched. sourcing_vector=%s",
               campaign.get("sourcing_vector"))
    bio             = campaign.get("bio", "")
    sourcing_vector = campaign.get("sourcing_vector", "Classic B2B")
    location        = campaign.get("location", "").strip()
    gl              = campaign.get("gl", "").strip()

    # ── V23 Persona Vault field extraction (priority over legacy fields) ────────
    # PREVIOUS BUG: code read campaign.get('keywords') first, then tried to
    # override if persona_id was set. Campaigns created via the Persona Vault
    # flow may not have a root-level 'keywords' field at all — only persona_bio
    # and persona_keywords. The legacy read returned "" and the persona block
    # only overwrote if the persona fields were non-empty, but the keywords list
    # was already empty by the time it was checked → silent HTTP 400 abort.
    #
    # FIX: persona fields are now primary. Legacy fields are the fallback.
    _persona_id   = campaign.get("persona_id", "")
    _persona_bio  = campaign.get("persona_bio", "").strip()
    _persona_keys = campaign.get("persona_keywords", "").strip()

    # bio: persona_bio → campaign.bio → ""
    bio = _persona_bio or campaign.get("bio", "")
    if _persona_id and _persona_bio:
        print(f"[PERSONA PRODUCER] Injected persona '{campaign.get('persona_name', _persona_id)}' "
              f"bio='{bio[:60]}' for campaign {campaign_id}")

    # raw_keywords: persona_keywords → campaign.keywords → ""
    raw_keywords = _persona_keys or campaign.get("keywords", "")
    if isinstance(raw_keywords, str):
        keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
    else:
        keywords = list(raw_keywords) if raw_keywords else []

    if _persona_id and _persona_keys and not keywords:
        print(f"[PERSONA PRODUCER] persona_keywords present but parsed to empty "
              f"list for campaign {campaign_id} — raw='{_persona_keys[:80]}'")
    # ─────────────────────────────────────────────────────────────────────────

    if not keywords:
        # RISK 4 FIX: synthesise from bio so the producer never aborts on
        # campaigns that have bio context but no explicit keyword list.
        if bio:
            keywords = [w.strip() for w in bio.split() if len(w.strip()) > 3][:5]
            print(f"[PRODUCER] No keywords — synthesised {len(keywords)} terms from bio "
                  f"for campaign {campaign_id}")
        if not keywords:
            import logging as _stdlib_log
            _stdlib_log.error(
                "[PRODUCER] CRITICAL ABORT: campaign %s has empty keywords AND empty bio. "
                "No Serper query can be constructed. "
                "Campaign fields: persona_id=%r persona_keywords=%r keywords=%r bio=%r",
                campaign_id, _persona_id,
                campaign.get("persona_keywords"),
                campaign.get("keywords"),
                campaign.get("bio"),
            )
            return jsonify({
                "error": "Empty keywords matrix",
                "campaign_id": campaign_id,
                "debug": {
                    "persona_id": _persona_id,
                    "persona_keywords": campaign.get("persona_keywords"),
                    "keywords": campaign.get("keywords"),
                    "bio": campaign.get("bio"),
                }
            }), 400

    _tlog.info("TRACE-6: Keywords resolved. count=%d bio_len=%d sourcing=%s",
               len(keywords), len(bio), sourcing_vector)

    # ── V19: CHILD_CAMPAIGN_OVERRIDE sentinel guard ──────────────────────────────
    # DT child campaigns set bio='CHILD_CAMPAIGN_OVERRIDE' as a routing marker.
    # Feeding this literal string to Gemini causes garbage-in → zero usable queries
    # → empty smart_keywords list → Serper loop never runs → silent 200.
    # Resolve: use effective_bio (stored at creation by orchestrator), then fall
    # back to campaign_focus, then to synthesized keywords string.
    # ────────────────────────────────────────────────────────────────────────────
    if bio == "CHILD_CAMPAIGN_OVERRIDE":
        bio = (campaign.get("effective_bio") or
               campaign.get("campaign_focus") or
               ", ".join(keywords))
        print(f"[PRODUCER] CHILD_CAMPAIGN_OVERRIDE resolved → bio='{bio[:80]}'")

    _tlog.info("TRACE-7: Calling generate_smart_query (Vertex AI)...")
    print(f"[SYNAPTIC ROUTER] Campaign {campaign_id} → sourcing vector: '{sourcing_vector}'")

    # ── Step 3: Intent Translation + Smart Query Generation ─────────────────
    _persona_cat = (
        campaign.get("persona_name") or
        campaign.get("name") or "general"
    ).strip()
    smart_keywords = generate_smart_query(
        keywords, tenant_id, bio, sourcing_vector,
        persona_category=_persona_cat
    )
    _tlog.info("TRACE-8: generate_smart_query complete. smart_keyword_count=%d", len(smart_keywords))

    # Telemetry billing
    db.collection("usage_metrics").document(tenant_id).set(
        {"serper_searches": firestore.Increment(len(smart_keywords))}, merge=True
    )

    # ── Step 4: Serper Execution ─────────────────────────────────────────────
    # P2: Track snippet text alongside each URL for walled-garden hand-off
    SOCIAL_DOMAINS_PRODUCER = ["reddit.com", "linkedin.com", "facebook.com",
                                "instagram.com", "x.com", "twitter.com",
                                "quora.com", "youtube.com", "team-bhp.com"]

    raw_urls    = []       # ordered list of raw URLs
    snippet_db  = {}       # url → {"title": ..., "snippet": ...} for hand-off
    _tlog.info("TRACE-9: Entering Serper execution loop. keyword_count=%d", len(smart_keywords))

    for kw in smart_keywords:
        # ── Query Builder Guard: clean location string, no orphaned AND operators ──
        # Strips whitespace, ignores 'all', prevents contradictory geo injection.
        clean_location = location.strip() if location else ""
        if clean_location and clean_location.lower() != "all":
            search_query = f"{kw} AND {clean_location}"
        else:
            search_query = kw
        raw_results  = search_serper(search_query, location=clean_location or None, gl=gl or None)
        # V18: Async GCS Firehose Dump — pre-filter raw Swarm noise → sideio-raw-firehose-lake
        _async_gcs_dump({
            "query":          search_query,
            "campaign_id":    campaign_id,
            "sourcing_vector": sourcing_vector,
            "keyword":        kw,
            "result_count":   len(raw_results) if raw_results else 0,
            "raw_results":    raw_results or [],
        }, tenant_id)
        filtered     = filter_serper_noise(raw_results)
        for r in filtered:
            link = r.get("link")
            if link and link not in raw_urls:
                raw_urls.append(link)
                # Capture snippet for later scraped_cache persistence
                snippet_db[link] = {
                    "title":   r.get("title",   ""),
                    "snippet": r.get("snippet", "")
                }

    fetched_count = len(raw_urls)
    print(f"[FUNNEL] Campaign: {campaign_id} | Producer: Fetched {fetched_count} URLs")

    # ── P2: Persist Serper snippets to scraped_cache for social/walled-garden URLs ──
    # This is the hand-off point. The Consumer will read these from Firestore
    # instead of receiving an empty snippet_map.
    for surl, meta in snippet_db.items():
        s_domain = extract_root_domain(surl)
        is_social_url = any(s_domain.endswith(d) for d in SOCIAL_DOMAINS_PRODUCER)
        combined_text = f"{meta['title']}\n{meta['snippet']}".strip()
        if is_social_url and combined_text:
            try:
                cache_key = surl.replace('/', '_')
                db.collection("scraped_cache").document(cache_key).set({
                    "url":      surl,
                    "text":     combined_text,
                    "source":   "serper_snippet",
                    "tech_stack": [],
                    "emails":   [],
                    "phones":   []
                }, merge=True)
            except Exception as se:
                print(f"[PRODUCER] Snippet persist failed for {surl}: {se}")

    # ── P0: Native Global Deduplication — path-aware for social domains ───────
    # Previously: all reddit threads hashed to SHA256(tenant_reddit.com)
    # Fix: social URLs hash by full URL; B2B URLs still hash by domain.
    existing_ids = set()
    try:
        known_docs = db.collection("leads").where(
            "tenant_id", "==", tenant_id
        ).select(["url"]).stream()
        for doc in known_docs:
            d = doc.to_dict()
            u = d.get("url", "")
            if u:
                d_domain   = extract_root_domain(u)
                is_social  = any(d_domain.endswith(s) for s in SOCIAL_DOMAINS_PRODUCER)
                dedup_key  = u if is_social else d_domain   # ← FIX: full URL for social
                lead_id_ex = hashlib.sha256(f"{tenant_id}_{dedup_key}".encode()).hexdigest()
                existing_ids.add(lead_id_ex)
                existing_ids.add(u)  # also track raw URL as secondary guard
    except Exception as dedup_e:
        print(f"[PRODUCER] Dedup query failed: {dedup_e}. Continuing without dedup.")

    fresh_urls = []
    for url in raw_urls:
        f_domain  = extract_root_domain(url)
        is_social = any(f_domain.endswith(s) for s in SOCIAL_DOMAINS_PRODUCER)
        dedup_key = url if is_social else f_domain          # ← FIX: full URL for social
        lead_id_h = hashlib.sha256(f"{tenant_id}_{dedup_key}".encode()).hexdigest()
        if lead_id_h not in existing_ids and url not in existing_ids:
            fresh_urls.append(url)

    duped_count  = fetched_count - len(fresh_urls)
    queued_count = len(fresh_urls)
    print(f"[FUNNEL] Campaign: {campaign_id} | Producer: Fetched {fetched_count} URLs | Deduplicated: {duped_count} | Queued: {queued_count}")

    # ── Write to unprocessed_queue (additive merge, cap at 200) ─────────────
    current_queue = campaign.get("unprocessed_queue", [])
    combined = list(dict.fromkeys(current_queue + fresh_urls))  # preserve order, dedupe
    combined = combined[:200]  # hard cap to prevent runaway growth

    campaign_ref.update({
        "unprocessed_queue":  combined,
        "last_produced_at":   firestore.SERVER_TIMESTAMP,
    })

    print(f"[PRODUCER] Campaign {campaign_id}: queue now has {len(combined)} URLs.")
    return jsonify({
        "status":        "produced",
        "fetched":       fetched_count,
        "deduplicated":  duped_count,
        "queued":        queued_count,
        "queue_depth":   len(combined)
    }), 200


# =============================================================================
# THE PRISM ENGINE — Hybrid Architecture (V18)
# =============================================================================
#
# Architecture Overview:
#   ┌──────────────────────────────────────────────────────────────┐
#   │  OperatingModeRouter                                         │
#   │  Reads campaign.target_personas (from Digital Twin)          │
#   │  → Classifies each URL as:                                   │
#   │      WalledGarden  — social/forum/walled domains             │
#   │      GeneralDomain — open B2B web                            │
#   │      B2B2C         — consumer-intent → distributor search    │
#   └──────────┬───────────────────┬─────────────────┬────────────┘
#              │                   │                 │
#    WalledGardenHook    GeneralDomainHook    B2B2CIntermediaryFinder
#    (3× parallel Serper  (httpx scrape        (find local distributors
#     triangulation)       + WAF fallback       for consumer intents)
#                          to snippet-path)
#
# All hooks return:
#   { "text": str, "tech_stack": list, "emails": list, "phones": list,
#     "mode": str, "fallback_used": bool }
#
# Error boundaries:
#   • Each hook catches its own exceptions and returns a structured
#     result dict, never raising into the calling dispatch() loop.
#   • WAF detection in GeneralDomainHook → immediate fallback to
#     WalledGardenHook snippet-path for that URL only.
# =============================================================================

# --------------------------------------------------------------------------
# WALLED GARDEN DOMAIN REGISTRY
# Determines which domains are routed to WalledGardenHook by default.
# Both the OperatingModeRouter AND the existing SOCIAL_DOMAINS_PRODUCER
# use this list — kept as a single source of truth.
# --------------------------------------------------------------------------
WALLED_GARDEN_DOMAINS: set[str] = {
    "reddit.com", "facebook.com", "instagram.com",
    "x.com", "twitter.com", "quora.com", "youtube.com", "team-bhp.com",
    "tiktok.com", "pinterest.com", "snapchat.com", "threads.net",
    # NOTE: "linkedin.com" is intentionally omitted here.
    # linkedin.com/company/ URLs are strict B2B and must undergo full enrichment
    # via GeneralDomainHook. The OperatingModeRouter.route() method applies a
    # path-level check below to send /company/ URLs to GeneralDomain.
}

# WAF fingerprints shared between GeneralDomainHook and the existing scrape_url()
_WAF_FINGERPRINTS = [
    "just a moment", "attention required!", "cloudflare ray id",
    "access denied", "403 forbidden", "please verify you are human",
    "enable javascript and cookies to continue",
    "checking if the site connection is secure",
]


def _is_waf_blocked(html_or_text: str) -> bool:
    """Returns True if the response looks like a WAF/bot-challenge page."""
    lowered = html_or_text.lower()
    return any(fp in lowered for fp in _WAF_FINGERPRINTS)


# --------------------------------------------------------------------------
# LAYER 0 — OPERATING MODE ROUTER
# --------------------------------------------------------------------------

class OperatingModeRouter:
    """
    Classifies a candidate URL into one of three processing modes:
      • 'WalledGarden'  — social/UGC domains; snippet-based analysis
      • 'GeneralDomain' — open web domains; httpx DOM scrape
      • 'B2B2C'         — consumer-intent URLs requiring intermediary search

    Classification logic (priority order):
      1. If the URL's root domain matches WALLED_GARDEN_DOMAINS → WalledGarden
      2. If any Digital Twin target_persona description contains B2B2C signals
         AND the URL contains consumer-intent keywords → B2B2C
      3. Else → GeneralDomain

    B2B2C signal detection is lightweight (keyword-level) to stay under 8s budget.
    """

    # Consumer-intent keywords that trigger B2B2C mode when present in the URL path
    _B2B2C_URL_SIGNALS   = {"review", "compare", "best", "near-me", "near+me",
                             "recommendation", "alternative", "vs", "find"}

    # Persona description keywords that indicate this campaign has B2B2C targets
    _B2B2C_PERSONA_FLAGS = {
        "consumer", "individual", "student", "patient", "retail",
        "end user", "end-user", "buyer", "shopper", "household",
        "b2b2c", "distributor", "reseller", "channel partner",
    }

    def __init__(self, target_personas: list[dict]):
        """
        :param target_personas: List of persona dicts from campaign.target_personas
                                (populated by the Digital Twin engine).
                                Each has keys: name, description, location_hint.
        """
        self._personas     = target_personas or []
        self._has_b2b2c    = self._detect_b2b2c_campaign()

    def _detect_b2b2c_campaign(self) -> bool:
        """
        Returns True if ANY persona description contains a B2B2C signal keyword.
        We OR across all personas so a mixed B2B/B2B2C campaign is still flagged.
        """
        for persona in self._personas:
            desc = (persona.get("description", "") + " " + persona.get("name", "")).lower()
            if any(flag in desc for flag in self._B2B2C_PERSONA_FLAGS):
                return True
        return False

    def route(self, url: str) -> str:
        """
        Returns one of: 'WalledGarden', 'GeneralDomain', 'B2B2C'
        """
        root_domain = extract_root_domain(url)
        url_lower   = url.lower()

        # ── FIX 3: LinkedIn company profile → strict B2B, NOT WalledGarden ──
        # linkedin.com/company/ URLs are verified company profiles — they carry
        # full B2B enrichment signal. Route them to GeneralDomain so they
        # receive httpx scraping + deep_context_serper_dork enrichment.
        if "linkedin.com" in root_domain and "/company/" in url_lower:
            print(f"[ROUTER] linkedin.com/company/ → GeneralDomain (strict B2B): {url}")
            return "GeneralDomain"

        # Priority 1: walled garden domain check (linkedin non-company URLs → WalledGarden)
        if "linkedin.com" in root_domain or any(root_domain.endswith(wg) for wg in WALLED_GARDEN_DOMAINS):
            return "WalledGarden"

        # Priority 2: B2B2C — only if campaign has B2B2C persona AND URL signals consumer intent
        if self._has_b2b2c:
            if any(sig in url_lower for sig in self._B2B2C_URL_SIGNALS):
                return "B2B2C"

        return "GeneralDomain"

    def summarise_personas(self) -> str:
        """Returns a compact text summary of target personas for Gemini prompts."""
        if not self._personas:
            return "No target personas defined."
        lines = []
        for i, p in enumerate(self._personas[:3], 1):
            lines.append(
                f"{i}. {p.get('name', 'Unknown')} — {p.get('description', '')} "
                f"[{p.get('location_hint', 'Global')}]"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# LAYER 1A — WALLED GARDEN HOOK
# Triangulation: 3 parallel Serper queries → concatenated snippets.
# Applies thin-payload truncation penalty on the output text.
# Reads/writes scraped_cache for deduplication.
# --------------------------------------------------------------------------

class WalledGardenHook:
    """
    Processes walled-garden / social URLs via Serper snippet triangulation.

    3-step pipeline:
      A. Execute 3 parallel Serper queries:
           i.   site:{domain} "{url_path_keywords}"
           ii.  "{domain_name}" intent signals
           iii. "{url_slug}" community discussion
      B. Concatenate all organic + KG snippets as the text payload.
      C. Apply Truncation Penalty: if total text < 500 chars, prefix a
         shadow-learner marker so the scoring threshold drops to 6.

    scraped_cache contract:
      • Always reads first — avoids duplicate Serper spend.
      • Always writes on fresh fetch — supplies Producer hand-off context.
    """

    def __init__(self, db_client, serper_key: str):
        self._db         = db_client
        self._serper_key = serper_key

    def _build_queries(self, url: str, root_domain: str, persona_summary: str) -> list[str]:
        """
        3-query triangulation tailored to the URL structure.

        Query rationale:
          Q1 site-scoped: finds the specific page/post as indexed by Google.
          Q2 brand + intent: finds third-party discussion about this entity.
          Q3 persona-aware: finds pages where the target persona discusses this.
        """
        parsed   = urlparse(url)
        # Extract meaningful path words (drop slashes, numbers, and single chars)
        path_slug = " ".join(
            w for w in parsed.path.replace("-", " ").replace("_", " ").split("/")
            if len(w) > 2 and not w.isdigit()
        )[:80]

        q1 = f'site:{root_domain} {path_slug}'.strip()
        q2 = f'"{root_domain}" {path_slug[:50]}'.strip()
        # Q3: persona-driven — extract first meaningful noun phrase from summary
        persona_hint = (persona_summary.split("—")[0].split("\n")[0][:60]).strip()
        q3 = f'"{root_domain}" {persona_hint}'.strip() if persona_hint else q2

        return [q1, q2, q3]

    def _run_serper(self, query: str) -> dict:
        headers = {"X-API-KEY": self._serper_key, "Content-Type": "application/json"}
        try:
            resp = httpx.post(
                "https://google.serper.dev/search",
                headers=headers,
                json={"q": query, "num": 10},
                timeout=6.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"[WALLED-GARDEN] Serper query failed: {e}")
        return {}

    def _extract_snippets(self, serper_result: dict) -> str:
        parts: list[str] = []
        kg = serper_result.get("knowledgeGraph", {})
        if kg.get("description"):
            parts.append(f"[KG] {kg['description']}")
        for r in serper_result.get("organic", [])[:8]:
            snippet = r.get("snippet", "").strip()
            title   = r.get("title",   "").strip()
            if snippet:
                parts.append(snippet)
            elif title:
                parts.append(title)
        return " ".join(parts)

    def fetch(self, url: str, root_domain: str, persona_summary: str, tenant_id: str) -> dict:
        """
        Returns:
          { text, tech_stack, emails, phones, mode, fallback_used }
        """
        cache_key = url.replace("/", "_")
        cache_ref = self._db.collection("scraped_cache").document(cache_key)

        # ── Cache read — avoid duplicate Serper spend ──────────────────────
        try:
            cached = cache_ref.get()
            if cached.exists:
                c = cached.to_dict()
                if c.get("text"):
                    print(f"[WALLED-GARDEN] Cache HIT for {url} ({len(c['text'])} chars)")
                    return {
                        "text":         c["text"],
                        "tech_stack":   c.get("tech_stack", ["Social Platform Snippet"]),
                        "emails":       c.get("emails", []),
                        "phones":       c.get("phones", []),
                        "mode":         "WalledGarden",
                        "fallback_used": False,
                    }
        except Exception as ce:
            print(f"[WALLED-GARDEN] Cache read error for {url}: {ce}")

        # ── 3-way parallel Serper triangulation ───────────────────────────
        queries = self._build_queries(url, root_domain, persona_summary)
        all_texts: list[str] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(self._run_serper, q): q for q in queries}
            for future in concurrent.futures.as_completed(futures, timeout=7.0):
                try:
                    result = future.result()
                    extracted = self._extract_snippets(result)
                    if extracted:
                        all_texts.append(extracted)
                except Exception as fe:
                    print(f"[WALLED-GARDEN] Triangulation future failed: {fe}")

        combined_text = " ".join(all_texts).strip()

        # ── Truncation Penalty — shadow learner marker ────────────────────
        # If text is thin (< 500 chars), tag it so dispatch() drops the accept
        # threshold from 7 to 6. This is the "Shadow Learner route" signal.
        is_thin = len(combined_text) < 500
        if is_thin:
            combined_text = f"[SHADOW_LEARNER_THIN_PAYLOAD] {combined_text}"
            print(f"[WALLED-GARDEN] Thin payload ({len(combined_text)} chars) — shadow learner tagged")

        # ── Cache write — hand-off for future Consumer runs ───────────────
        if combined_text:
            try:
                cache_ref.set({
                    "url":        url,
                    "text":       safe_truncate(combined_text),
                    "source":     "walled_garden_triangulation",
                    "tech_stack": ["Social Platform Snippet"],
                    "emails":     [],
                    "phones":     [],
                }, merge=True)
            except Exception as cw:
                print(f"[WALLED-GARDEN] Cache write failed for {url}: {cw}")

        # ── Serper spend telemetry ─────────────────────────────────────────
        try:
            shard_id = random.randint(0, 9)
            self._db.collection("usage_metrics").document(tenant_id).set(
                {"serper_searches": firestore.Increment(len(queries))}, merge=True
            )
        except Exception:
            pass

        return {
            "text":         combined_text,
            "tech_stack":   ["Social Platform Snippet"],
            "emails":       [],
            "phones":       [],
            "mode":         "WalledGarden",
            "fallback_used": False,
        }


# --------------------------------------------------------------------------
# LAYER 1B — GENERAL DOMAIN HOOK
# httpx DOM scrape with WAF detection → fallback to WalledGarden snippet-path.
# Reads/writes scraped_cache.
# Runs Digital Twin persona embedding match scoring on extracted text.
# --------------------------------------------------------------------------

# Tech stack X-ray signatures (shared with existing scrape_url())
_TECH_SIGNATURES: dict[str, str] = {
    "wordpress":      "wp-content",
    "shopify":        "cdn.shopify.com",
    "stripe":         "js.stripe.com",
    "react":          "react-root",
    "hubspot":        "js.hs-scripts.com",
    "salesforce":     "force.com",
    "google analytics": "google-analytics.com",
    "segment":        "cdn.segment.com",
    "intercom":       "widget.intercom.io",
    "crisp":          "crisp.chat",
    "zendesk":        "zopim.com",
    "drift":          "drift.com/drift-frame",
}


def _extract_tech_stack(html_blob: str) -> list[str]:
    lowered = html_blob.lower()
    return [name for name, sig in _TECH_SIGNATURES.items() if sig in lowered]


def _persona_match_score(text: str, persona_summary: str) -> int:
    """
    Lightweight keyword-overlap score between scraped text and persona descriptions.
    Returns 0-10. Used as a tiebreaker / early-drop signal.

    Implementation: token intersection. Avoids an extra Gemini call on this path.
    A full embedding cosine-similarity approach is deferred to a future sprint
    (would require storing persona embeddings in Firestore).
    """
    import re
    persona_tokens = set(re.findall(r"\b\w{4,}\b", persona_summary.lower()))
    text_tokens    = set(re.findall(r"\b\w{4,}\b", text.lower()[:8000]))
    if not persona_tokens:
        return 5  # neutral
    overlap = len(persona_tokens & text_tokens)
    return min(10, int((overlap / max(len(persona_tokens), 1)) * 20))


class GeneralDomainHook:
    """
    Processes open-web B2B domains via httpx DOM scrape.

    Processing flow:
      1. Cache check (avoid re-scrape of recently seen domains).
      2. httpx.get(url, timeout=10) — standard User-Agent.
      3. WAF detection on response body → if blocked, delegate to
         WalledGardenHook (snippet path) for this URL only.
      4. BeautifulSoup text extraction + Tech-Stack X-Ray.
      5. Digital Twin persona match scoring (lightweight token overlap).
      6. Cache write to scraped_cache.

    Error boundaries:
      • Any non-WAF network/parse error → returns empty text (calling
        dispatch() will mark the lead as failed_scrape).
      • WAF detection → returns WalledGarden fallback result with
        fallback_used=True so the caller logs the fallback event.
    """

    def __init__(self, db_client, serper_key: str):
        self._db         = db_client
        self._serper_key = serper_key
        self._wg_hook    = WalledGardenHook(db_client, serper_key)

    def fetch(
        self,
        url: str,
        root_domain: str,
        persona_summary: str,
        tenant_id: str,
    ) -> dict:
        """
        Returns:
          { text, tech_stack, emails, phones, mode, fallback_used,
            persona_match_score }
        """
        cache_key = url.replace("/", "_")
        cache_ref = self._db.collection("scraped_cache").document(cache_key)

        # ── Cache read ──────────────────────────────────────────────────────
        try:
            cached = cache_ref.get()
            if cached.exists:
                c = cached.to_dict()
                if c.get("text") and c.get("source") != "serper_snippet":
                    print(f"[GENERAL-DOMAIN] Cache HIT for {url}")
                    return {
                        "text":               c["text"],
                        "tech_stack":         c.get("tech_stack", []),
                        "emails":             c.get("emails", []),
                        "phones":             c.get("phones", []),
                        "mode":               "GeneralDomain",
                        "fallback_used":      False,
                        "persona_match_score": _persona_match_score(c["text"], persona_summary),
                    }
        except Exception as ce:
            print(f"[GENERAL-DOMAIN] Cache read error for {url}: {ce}")

        # ── httpx DOM scrape ────────────────────────────────────────────────
        text       = ""
        tech_stack : list[str] = []
        emails     : list[str] = []
        phones     : list[str] = []
        fallback   = False

        try:
            resp = httpx.get(
                url,
                timeout=httpx.Timeout(connect=4.0, read=10.0, write=10.0, pool=1.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SideioBot/1.0; +https://sideio.com)"},
            )

            raw_html = resp.text

            # ── WAF detection → immediate fallback ────────────────────────
            if _is_waf_blocked(raw_html) or resp.status_code in (403, 429, 503):
                print(f"[GENERAL-DOMAIN] WAF/block detected for {url} (HTTP {resp.status_code}). "
                      f"Falling back to WalledGarden snippet path.")
                wg_result = self._wg_hook.fetch(url, root_domain, persona_summary, tenant_id)
                wg_result["fallback_used"] = True
                wg_result["mode"]          = "GeneralDomain→WalledGardenFallback"
                wg_result["persona_match_score"] = _persona_match_score(
                    wg_result.get("text", ""), persona_summary
                )
                return wg_result

            # ── BeautifulSoup extraction ───────────────────────────────────
            soup = BeautifulSoup(raw_html, "html.parser")

            # Tech-Stack X-Ray (zero-cost, regex on raw HTML)
            tech_stack = _extract_tech_stack(raw_html.lower())

            # Email and phone harvesting from <a> tags
            emails = list({
                a["href"].replace("mailto:", "").split("?")[0].strip()
                for a in soup.find_all("a", href=True)
                if a["href"].startswith("mailto:")
            })[:5]
            phones = list({
                a["href"].replace("tel:", "").strip()
                for a in soup.find_all("a", href=True)
                if a["href"].startswith("tel:")
            })[:3]

            # Text extraction — prioritise semantic HTML5 sections
            # (header, main, article, section) over raw body dump
            semantic_zones = soup.find_all(["main", "article", "section", "header"])
            if semantic_zones:
                text = " ".join(
                    zone.get_text(separator=" ", strip=True)
                    for zone in semantic_zones
                )
            else:
                text = soup.get_text(separator=" ", strip=True)

            if len(text) < 150:
                # JS-heavy / thin page — treat as walled-garden fallback
                print(f"[GENERAL-DOMAIN] Too little text ({len(text)} chars) for {url}. "
                      f"Falling back to WalledGarden snippet path.")
                wg_result = self._wg_hook.fetch(url, root_domain, persona_summary, tenant_id)
                wg_result["fallback_used"]       = True
                wg_result["mode"]                = "GeneralDomain→WalledGardenFallback"
                wg_result["persona_match_score"] = _persona_match_score(
                    wg_result.get("text", ""), persona_summary
                )
                return wg_result

        except httpx.TimeoutException:
            print(f"[GENERAL-DOMAIN] httpx timeout for {url}. Falling back to snippet path.")
            wg_result = self._wg_hook.fetch(url, root_domain, persona_summary, tenant_id)
            wg_result["fallback_used"]       = True
            wg_result["mode"]                = "GeneralDomain→WalledGardenFallback(Timeout)"
            wg_result["persona_match_score"] = _persona_match_score(
                wg_result.get("text", ""), persona_summary
            )
            return wg_result
        except Exception as e:
            print(f"[GENERAL-DOMAIN] Unexpected scrape error for {url}: {e}")
            return {
                "text": "", "tech_stack": [], "emails": [], "phones": [],
                "mode": "GeneralDomain", "fallback_used": False,
                "persona_match_score": 0,
            }

        # ── Cache write ─────────────────────────────────────────────────────
        try:
            cache_ref.set({
                "url":        url,
                "text":       safe_truncate(text),
                "source":     "general_domain_httpx",
                "tech_stack": tech_stack,
                "emails":     emails,
                "phones":     phones,
            }, merge=True)
        except Exception as cw:
            print(f"[GENERAL-DOMAIN] Cache write failed for {url}: {cw}")

        pms = _persona_match_score(text, persona_summary)
        print(f"[GENERAL-DOMAIN] Scraped {url}: {len(text)} chars | "
              f"tech={tech_stack} | persona_match={pms}")

        return {
            "text":               safe_truncate(text),
            "tech_stack":         tech_stack,
            "emails":             emails,
            "phones":             phones,
            "mode":               "GeneralDomain",
            "fallback_used":      False,
            "persona_match_score": pms,
        }


# --------------------------------------------------------------------------
# LAYER 1C — B2B2C INTERMEDIARY FINDER
# Takes a consumer-intent URL + geographic hint from Digital Twin personas.
# Searches for local distributors/resellers in that geographic area.
# --------------------------------------------------------------------------

class B2B2CIntermediaryFinder:
    """
    B2B2C Bridge: finds local distributor/reseller/channel partners
    who carry the product/service relevant to the consumer-intent URL.

    Pipeline:
      1. Extract the consumer intent phrase from the URL path + page context.
      2. Derive target geography from the campaign's Digital Twin persona
         location_hints (preferring the most specific region).
      3. Execute 2 Serper queries:
           a. distributor/reseller search for the product category in that geo.
           b. local stockist/channel partner search.
      4. Return normalised intermediary candidates as text for final_score_and_dm().

    The output text is formatted to prime the Gemini scoring prompt:
    "These are B2B2C intermediaries (distributors/resellers) who serve [consumer
    profile] in [region]. Score and draft an outreach for the vendor..."
    """

    def __init__(self, db_client, serper_key: str):
        self._db         = db_client
        self._serper_key = serper_key

    def _serper_search(self, query: str, gl: str | None = None) -> list[dict]:
        headers = {"X-API-KEY": self._serper_key, "Content-Type": "application/json"}
        payload: dict = {"q": query, "num": 10}
        if gl:
            payload["gl"] = gl
        try:
            resp = httpx.post(
                "https://google.serper.dev/search",
                headers=headers,
                json=payload,
                timeout=6.0,
            )
            if resp.status_code == 200:
                return resp.json().get("organic", [])
        except Exception as e:
            print(f"[B2B2C] Serper search failed: {e}")
        return []

    def _derive_geo(self, personas: list[dict]) -> tuple[str, str]:
        """
        Returns (location_string, gl_code) from the campaign's persona hints.
        Prefers the most specific (non-'Global') hint across all personas.
        """
        _GL_MAP = {
            "india": ("India", "in"), "usa": ("USA", "us"),
            "united states": ("USA", "us"), "uk": ("UK", "gb"),
            "united kingdom": ("UK", "gb"), "canada": ("Canada", "ca"),
            "australia": ("Australia", "au"), "germany": ("Germany", "de"),
            "singapore": ("Singapore", "sg"), "uae": ("UAE", "ae"),
            "dubai": ("UAE", "ae"), "global": ("", ""),
        }
        for persona in personas:
            hint = persona.get("location_hint", "Global").lower()
            if hint and hint != "global":
                for kw, vals in _GL_MAP.items():
                    if kw in hint:
                        return vals
        return "", ""

    def find_intermediaries(
        self,
        consumer_url: str,
        root_domain:  str,
        personas:     list[dict],
        persona_summary: str,
        tenant_id:    str,
    ) -> dict:
        """
        Returns the standard hook result dict with mode='B2B2C'.
        """
        location_str, gl = self._derive_geo(personas)

        # Derive product/service category from URL path + persona summary
        parsed    = urlparse(consumer_url)
        url_words = " ".join(
            w for w in parsed.path.replace("-", " ").replace("_", " ").split("/")
            if len(w) > 2
        )[:60]
        # Use the first persona name as category signal
        persona_category = (personas[0].get("name", "") if personas else "")[:50]
        product_category = (url_words or persona_category or root_domain)[:80]

        # Build geo-scoped distributor queries
        geo_suffix = f" {location_str}" if location_str else ""
        queries = [
            f'"{product_category}" distributor reseller{geo_suffix} -site:alibaba.com',
            f'"{product_category}" channel partner stockist{geo_suffix} B2B',
        ]

        print(f"[B2B2C] Finding intermediaries: category='{product_category}', "
              f"geo='{location_str}', gl='{gl}'")

        all_snippets: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self._serper_search, q, gl or None) for q in queries]
            for future in concurrent.futures.as_completed(futures, timeout=7.0):
                try:
                    results = future.result()
                    for r in results[:6]:
                        link    = r.get("link", "")
                        snippet = r.get("snippet", "")
                        title   = r.get("title", "")
                        if snippet:
                            all_snippets.append(f"[INTERMEDIARY] {title} — {link}\n{snippet}")
                except Exception as fe:
                    print(f"[B2B2C] Intermediary future error: {fe}")

        combined_text = "\n\n".join(all_snippets) if all_snippets else ""

        # Prime the Gemini scoring context
        context_header = (
            f"[B2B2C BRIDGE MODE]\n"
            f"Consumer Intent Source: {consumer_url}\n"
            f"Product/Service Category: {product_category}\n"
            f"Target Geography: {location_str or 'Global'}\n"
            f"Campaign Persona: {persona_summary[:200]}\n\n"
            f"The following are local distributors, resellers, or channel partners "
            f"who can reach the consumer segment described above. "
            f"Score each as a B2B lead for the vendor (NOT the consumer):\n\n"
            f"{combined_text}"
        )

        # Serper spend telemetry
        try:
            self._db.collection("usage_metrics").document(tenant_id).set(
                {"serper_searches": firestore.Increment(len(queries))}, merge=True
            )
        except Exception:
            pass

        return {
            "text":               safe_truncate(context_header),
            "tech_stack":         ["B2B2C Intermediary Search"],
            "emails":             [],
            "phones":             [],
            "mode":               "B2B2C",
            "fallback_used":      False,
            "persona_match_score": 5,   # neutral — Gemini scores the intermediaries
        }


# --------------------------------------------------------------------------
# THE PRISM PIPELINE — Orchestrator
# Entry point: PrismPipeline(campaign_doc, db, serper_key)
#              .process_url(url, tenant_id) → hook_result dict
# --------------------------------------------------------------------------

class PrismPipeline:
    """
    Composes OperatingModeRouter + the three hooks into a single
    callable that dispatch() uses per URL.

    Usage in dispatch():
        prism = PrismPipeline(campaign, db, serper_key)
        hook_result = prism.process_url(url, tenant_id)
        text       = hook_result["text"]
        tech_stack = hook_result["tech_stack"]
        ...

    All decisions (mode, fallback) are logged at the INFO level
    so Cloud Logging dashboards can surface per-mode funnel metrics.
    """

    def __init__(self, campaign_doc: dict, db_client, serper_key: str):
        target_personas = campaign_doc.get("target_personas", [])
        self._router    = OperatingModeRouter(target_personas)
        self._personas  = target_personas
        self._wg_hook   = WalledGardenHook(db_client, serper_key)
        self._gd_hook   = GeneralDomainHook(db_client, serper_key)
        self._b2c_hook  = B2B2CIntermediaryFinder(db_client, serper_key)
        self._persona_summary = self._router.summarise_personas()

    def process_url(self, url: str, tenant_id: str) -> dict:
        """
        Routes the URL through the correct hook and returns a unified result.
        Never raises — all exceptions are caught and returned as empty text.
        """
        root_domain = extract_root_domain(url)
        mode        = self._router.route(url)

        print(f"[PRISM] URL: {url} | Domain: {root_domain} | Mode: {mode}")

        try:
            if mode == "WalledGarden":
                return self._wg_hook.fetch(url, root_domain, self._persona_summary, tenant_id)

            elif mode == "B2B2C":
                return self._b2c_hook.find_intermediaries(
                    url, root_domain, self._personas, self._persona_summary, tenant_id
                )

            else:  # GeneralDomain
                return self._gd_hook.fetch(url, root_domain, self._persona_summary, tenant_id)

        except Exception as e:
            print(f"[PRISM] Unhandled exception for {url} (mode={mode}): {e}")
            return {
                "text": "", "tech_stack": [], "emails": [], "phones": [],
                "mode": mode, "fallback_used": False,
                "persona_match_score": 0,
            }


# =============================================================================
# FIX 1: CREDIT SETTLEMENT HELPER
# Called after each URL completes in dispatch() and finalize().
# Enqueues a Cloud Task to /api/internal/credits/settle on the orchestrator.
# outcome="success": total_consumed += 1, reserved_credits -= 1 (settled)
# outcome="failure": reserved_credits -= 1 only (refund, no credit consumed)
# =============================================================================
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "")


def _settle_credit(tenant_id: str, outcome: str, count: int = 1, lead_id: str = ""):
    """
    FIX 1B: Non-blocking credit settlement via Cloud Tasks.
    lead_id is threaded through as an idempotency key: the orchestrator's
    /api/internal/credits/settle endpoint atomically stamps credit_settled=True
    on the lead document inside a Firestore transaction before applying any
    wallet Increment. Cloud Tasks retries that arrive after a successful write
    are rejected by the idempotency guard and return 200 (acknowledged).
    Falls back to direct wallet_shards write if ORCHESTRATOR_URL is not set
    (pre-migration path — no idempotency guarantee on fallback).
    """
    if not ORCHESTRATOR_URL:
        # Pre-migration fallback: direct shard increment (no idempotency key)
        try:
            _shard_id = random.randint(0, 9)
            if outcome == "success":
                db.collection("users").document(tenant_id).collection("wallet_shards") \
                    .document(str(_shard_id)).set(
                        {"consumed_credits": firestore.Increment(1)}, merge=True
                    )
        except Exception as fb_e:
            print(f"[SETTLE-FALLBACK] Shard write failed: {fb_e}")
        return

    try:
        from google.cloud import tasks_v2 as _tv2
        _tc         = _tv2.CloudTasksClient()
        _queue_path = _tc.queue_path(
            os.environ.get("PROJECT_ID", ""),
            os.environ.get("LOCATION", "us-central1"),
            os.environ.get("QUEUE", "lead-pipeline-queue"),
        )
        _body = json.dumps({
            "tenant_id": tenant_id,
            "outcome":   outcome,
            "count":     count,
            "lead_id":   lead_id,   # FIX 1B: idempotency key — checked transactionally in orchestrator
        }).encode()
        _tc.create_task(
            parent=_queue_path,
            task={
                "http_request": {
                    "http_method": _tv2.HttpMethod.POST,
                    "url":         f"{ORCHESTRATOR_URL}/api/internal/credits/settle",
                    "headers":     {"Content-Type": "application/json"},
                    "body":        _body,
                }
            }
        )
        print(f"[SETTLE] Enqueued: tenant={tenant_id}, outcome={outcome}, "
              f"lead={lead_id[:12] if lead_id else 'N/A'}...")
    except Exception as e:
        print(f"[SETTLE] Enqueue failed (non-fatal): {e}")


# =============================================================================
# FIX 2: ATOMIC LOCK ACQUISITION — @_firestore_transactional
# Previously: a bare lock_ref.get() followed by lock_ref.set() with the
# contention exception silently swallowed (except: print → proceed).
# Under multi-tenant concurrency, all workers bypassed the TOCTOU gap.
#
# Now: a single Firestore transaction atomically reads + conditionally writes.
# On ABORTED / DEADLINE_EXCEEDED, the decorator raises → caller hits the
# except branch which now does `continue` (skip URL) instead of proceeding.
# This eliminates duplicate leads and wasted Serper/Gemini calls under load.
# =============================================================================
@_firestore_transactional
def _acquire_lead_lock(transaction, lock_ref, now_utc):
    """
    Atomically acquires a global exclusivity lock for a domain or social entity.
    Returns True  → lock acquired (new lock or prior lock is expired).
    Returns False → domain is within its 14-day exclusivity window; caller skips.
    Raises        → Firestore contention (ABORTED/DEADLINE_EXCEEDED); caller skips.
    """
    snap = lock_ref.get(transaction=transaction)
    if snap.exists:
        locked_until = snap.to_dict().get("locked_until")
        if locked_until:
            if hasattr(locked_until, "tzinfo") and locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=datetime.timezone.utc)
            if locked_until > now_utc:
                return False  # Still locked — caller should skip this URL
    # Acquire: set new lock (or overwrite an expired one) inside the transaction
    transaction.set(lock_ref, {"locked_until": now_utc + datetime.timedelta(days=14)})
    return True


@app.route("/dispatch", methods=["POST"])
def dispatch():
    if not request.headers.get("X-CloudTasks-QueueName"):
        print(f"[DISPATCH] REJECTED: Missing X-CloudTasks-QueueName header. "
              f"Direct access not permitted. IP={request.remote_addr}")
        return jsonify({"error": "Forbidden", "message": "Task queue header required."}), 403
    """
    V14.4: THE CONSUMER — 4-Hour Drip Processor.
    Pops exactly 10 URLs from campaigns/{id}.unprocessed_queue (destructive read).
    Runs Step 5 (Gemini Confidence Gate) and Step 6 (Playwright Scraper).
    Does NOT call Serper. If queue is empty, exits gracefully.
    """
    lead_data = request.json
    tenant_id = lead_data.get("tenant_id")

    target_campaign_id = lead_data.get("campaign_id") or (
        lead_data.get("matched_campaigns")[0] if lead_data.get("matched_campaigns") else None
    )
    if not target_campaign_id:
        print("[CONSUMER] CRITICAL: No identifiable campaign context. Dropping task.")
        return jsonify({"error": "Missing campaign_id context"}), 400

    campaign_id  = target_campaign_id
    print(f"[CONSUMER] campaign_id={campaign_id} tenant_id={tenant_id} — fetching campaign document...")
    campaign_ref = db.collection("campaigns").document(campaign_id)
    try:
        campaign     = campaign_ref.get().to_dict() or {}
    except Exception as _cget_err:
        print(f"[CONSUMER] CRITICAL: Firestore campaign.get() blocked/failed: {_cget_err}")
        return jsonify({"error": "Firestore timeout fetching campaign"}), 500
    print(f"[CONSUMER] Campaign document loaded. sourcing_vector={campaign.get('sourcing_vector')} "
          f"queue_depth={len(campaign.get('unprocessed_queue', []))}")
    bio          = campaign.get("bio", "")
    sourcing_vector = campaign.get("sourcing_vector", "Classic B2B")
    location     = campaign.get("location", "").strip()

    # ── PERSONA VAULT: inject linked persona bio at consumer path ─────────────
    # Uses the same denormalised fields written at campaign creation.
    # Ensures PRISM scoring and Gemini prompts use the correct persona context
    # even on campaigns created long before Persona Vault was introduced.
    _c_persona_id = campaign.get("persona_id", "")
    if _c_persona_id:
        _c_persona_bio  = campaign.get("persona_bio", "").strip()
        if _c_persona_bio:
            bio = _c_persona_bio
            print(f"[PERSONA CONSUMER] Injected persona '{campaign.get('persona_name', _c_persona_id)}' "
                  f"for campaign {campaign_id}")
    # ─────────────────────────────────────────────────────────────────────────

    from google.cloud.firestore_v1.base_query import FieldFilter
    # V18 Multi-Campaign Swarm: Pre-fetch ALL active campaigns for tenant ecosystem
    print(f"[CONSUMER] Fetching active campaigns for tenant {tenant_id}...")
    try:
        active_campaigns_docs = db.collection("campaigns").where(filter=FieldFilter("tenant_id", "==", tenant_id)).where(filter=FieldFilter("status", "==", "active")).stream()
        active_campaigns = []
        for doc in active_campaigns_docs:
            d = doc.to_dict()
            d["id"] = doc.id
            active_campaigns.append(d)
    except Exception as _ac_err:
        print(f"[CONSUMER] active_campaigns query failed (non-fatal): {_ac_err}. Using current campaign only.")
        active_campaigns = []
    if not active_campaigns:
        active_campaigns = [campaign]

    # ── FIX 2: Persona Injection — load target_personas from tenant_profiles ──
    # The campaign doc's target_personas array is often empty because the
    # Digital Twin writes personas to the tenant_profiles sub-collection,
    # not directly onto the campaign document. Perform an explicit lookup here
    # and inject them into the campaign dict before constructing PrismPipeline.
    # Fallback: if profile is missing, use campaign.bio as a synthetic persona
    # so the LLM always has scoring criteria.
    _raw_personas = campaign.get("target_personas", [])
    if not _raw_personas:
        try:
            _profile_ref  = db.collection("tenant_profiles").document(tenant_id)
            _profile_snap = _profile_ref.get()
            if _profile_snap.exists:
                _raw_personas = _profile_snap.to_dict().get("target_personas", [])
                print(f"[PRISM] target_personas loaded from tenant_profiles: {len(_raw_personas)} persona(s)")
        except Exception as _pe:
            print(f"[PRISM] tenant_profiles lookup failed: {_pe}")

    if not _raw_personas and bio:
        # Bio fallback: synthesise a single generic persona so scoring has context
        _raw_personas = [{"name": "Target Persona", "description": bio, "location_hint": location or "Global"}]
        print(f"[PRISM] personas=0 after profile lookup — using bio fallback persona.")

    # Inject resolved personas back into the campaign dict for PrismPipeline
    campaign["target_personas"] = _raw_personas

    # ── V18: PrismPipeline — instantiate once per dispatch() call ────────────
    # Reads target_personas from the campaign doc (now guaranteed non-empty).
    # Falls back gracefully to GeneralDomain mode if personas are absent.
    try:
        _serper_key_for_prism = get_secret(SERPER_API_KEY_NAME).strip()
        prism = PrismPipeline(campaign, db, _serper_key_for_prism)
        print(f"[PRISM] Instantiated for campaign {campaign_id} | "
              f"personas={len(campaign.get('target_personas', []))}")
    except Exception as prism_init_err:
        print(f"[PRISM] Init failed: {prism_init_err}. Prism disabled for this batch.")
        prism = None

    # ── Destructive Queue Pop (Batch of 10) — Race Condition Safe ────────────
    # We immediately write the queue MINUS the batch before processing begins.
    # This prevents double-processing if two tasks fire concurrently.
    current_queue = campaign.get("unprocessed_queue", [])

    if not current_queue:
        print(f"[CONSUMER] Campaign {campaign_id}: unprocessed_queue is empty. Exiting gracefully.")
        return jsonify({"status": "queue_empty", "processed": 0}), 200

    BATCH_SIZE    = 10
    batch_urls    = current_queue[:BATCH_SIZE]
    remaining     = current_queue[BATCH_SIZE:]

    # Atomic destructive read — remove batch from queue immediately
    campaign_ref.update({"unprocessed_queue": remaining})

    print(f"[FUNNEL] Campaign: {campaign_id} | Consumer: Processing Batch of {len(batch_urls)} URLs")

    print(f"[CONSUMER] Fetching user preferences for tenant {tenant_id}...")
    try:
        user_doc            = db.collection("users").document(tenant_id).get()
        preferences_weights = user_doc.to_dict().get("preferences_weights", {}) if user_doc.exists else {}
    except Exception as _udoc_err:
        print(f"[CONSUMER] user doc .get() blocked/failed: {_udoc_err}. Continuing with empty preferences.")
        preferences_weights = {}
    print(f"[CONSUMER] User preferences loaded. Hydrating snippet_map for {len(batch_urls)} URLs...")

    # ── P2: Hydrate snippet_map from scraped_cache (Producer hand-off) ────────
    # The Producer wrote Serper snippets to scraped_cache with source=serper_snippet.
    # Load them now so the social short-circuit path has real context.
    snippet_map = {}
    for batch_url in batch_urls:
        b_domain   = extract_root_domain(batch_url)
        is_social  = any(b_domain.endswith(s) for s in ["reddit.com", "linkedin.com", "facebook.com",
                                                          "instagram.com", "x.com", "twitter.com",
                                                          "quora.com", "youtube.com", "team-bhp.com"])
        if is_social:
            try:
                cache_key = batch_url.replace('/', '_')
                cdoc = db.collection("scraped_cache").document(cache_key).get()
                if cdoc.exists:
                    cached_text = cdoc.to_dict().get("text", "")
                    if cached_text:
                        snippet_map[batch_url] = cached_text
            except Exception as sm_e:
                print(f"[CONSUMER] snippet_map hydration failed for {batch_url}: {sm_e}")

    # ── Step 5: Confidence Tiering Gate ─────────────────────────────────────
    # Feed batch URLs into pre_filter_gemini. Include snippet text where available
    # so the tiering LLM has real context (not just bare URLs) for social leads.
    print(f"[CONSUMER] Calling pre_filter_gemini for {len(batch_urls)} URLs (hard timeout=30s)...")
    synthetic_snippets = [
        {"link": u, "snippet": snippet_map.get(u, ""), "title": ""}
        for u in batch_urls
    ]
    try:
        import concurrent.futures as _cf_gate
        with _cf_gate.ThreadPoolExecutor(max_workers=1) as _gate_pool:
            _gate_future = _gate_pool.submit(pre_filter_gemini, synthetic_snippets, bio, location)
            tiered = _gate_future.result(timeout=30)
    except Exception as _gate_err:
        print(f"[CONSUMER] pre_filter_gemini timed out or failed ({_gate_err}). "
              f"Treating ALL {len(batch_urls)} URLs as High-tier to unblock scraper.")
        tiered = {"High": batch_urls, "Medium": [], "Low": []}
    high_urls   = tiered.get("High", [])
    medium_urls = tiered.get("Medium", [])
    print(f"[CONSUMER] Gemini gate complete: High={len(high_urls)} Medium={len(medium_urls)}")

    velocity_threshold = int(os.environ.get("VELOCITY_THRESHOLD", "10"))
    try:
        cutoff_24h   = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        recent_count = (
            db.collection("leads")
            .where("tenant_id", "==", tenant_id)
            .where("status",    "==", "new")
            .where("createdAt", ">=", cutoff_24h)
            .count().get()[0][0].value
        )
    except Exception as vel_e:
        print(f"[VELOCITY] Count query failed: {vel_e}. Defaulting to allow Medium.")
        recent_count = 0

    allow_medium      = recent_count < velocity_threshold
    approved_urls     = high_urls + (medium_urls if allow_medium else [])
    gate_rejected     = len(batch_urls) - len(approved_urls)
    print(f"[FUNNEL] Campaign: {campaign_id} | Gate (Step 5) Rejected: {gate_rejected} | Passed to Scraper: {len(approved_urls)}")

    url_to_tier = {u: "High" for u in high_urls}
    url_to_tier.update({u: "Medium" for u in medium_urls})

    # ── Step 6: Playwright Scraper + Gemini Extraction ──────────────────────
    SOCIAL_DOMAINS = ["linkedin.com", "facebook.com", "reddit.com", "instagram.com",
                      "x.com", "twitter.com", "team-bhp.com", "quora.com", "youtube.com"]

    all_results    = []
    scrape_success = 0
    scrape_failed  = 0

    for url in approved_urls:
        target_domain = extract_root_domain(url)
        if not target_domain:
            continue

        # Social path detection
        is_social = any(target_domain.endswith(s) for s in SOCIAL_DOMAINS)
        if is_social:
            parsed_url  = urlparse(url)
            exact_path  = f"{parsed_url.netloc}{parsed_url.path}".lower().replace('www.', '')
            lock_entity = hashlib.sha256(exact_path.encode()).hexdigest()
            dedupe_target = exact_path
        else:
            lock_entity   = target_domain
            dedupe_target = target_domain

        # Global Exclusivity Lock — FIX 2: Atomic transactional acquisition
        # The old pattern (get → set in separate RPCs) was a TOCTOU race.
        # 50 concurrent workers targeting ibm.com all read 'no lock' simultaneously
        # and then all wrote → 50 duplicate leads. The prior `except: print` swallowed
        # DEADLINE_EXCEEDED so contention silently bypassed the gate entirely.
        # Now: transaction serialises the read+write; any exception means skip.
        lock_ref = db.collection("global_lead_locks").document(lock_entity)
        try:
            now_utc   = datetime.datetime.now(datetime.timezone.utc)
            _lock_txn = db.transaction()
            _acquired = _acquire_lead_lock(_lock_txn, lock_ref, now_utc)
            if not _acquired:
                print(f"[EXCLUSIVITY] Dropping {url}. Entity {lock_entity} "
                      f"is within 14-day exclusivity window.")
                continue
        except Exception as le:
            # ABORTED / DEADLINE_EXCEEDED under contention → skip, do not proceed.
            # Old code: print + fall-through (all workers bypassed the lock).
            # New code: continue → this worker relinquishes the URL.
            print(f"[LOCK FAIL] {le} — skipping {url} to prevent duplicate lead.")
            continue

        # Deterministic Dedup Gateway
        lead_id_str = f"{tenant_id}_{dedupe_target}"
        lead_id     = hashlib.sha256(lead_id_str.encode()).hexdigest()
        doc_ref     = db.collection("leads").document(lead_id)

        try:
            # ── DPDP TTL: expire_at = now + 90 days (Firestore native TTL watches this field) ──
            # Leads NOT pushed to CRM will be auto-deleted after 90 days.
            # pushToCRM() sets expire_at=null to permanently exempt CRM leads.
            _expire_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=90)
            doc_ref.create({
                "tenant_id":         tenant_id,
                "matched_campaigns": [campaign_id],
                "url":               url,
                "lock_entity":       lock_entity,    # FIX 2: stored for zombie recovery
                "confidence_tier":   url_to_tier.get(url, "High"),
                "sourcing_vector":   sourcing_vector,
                "status":            "processing",
                "is_in_crm":         False,
                "createdAt":         firestore.SERVER_TIMESTAMP,
                "expire_at":         _expire_at,
            })
        except AlreadyExists:
            print(f"[UAR] Cross-campaign duplicate for {target_domain}. Updating matched_campaigns.")
            doc_ref.update({"matched_campaigns": firestore.ArrayUnion([campaign_id])})
            continue

        try:
            # ── PRISM ENGINE: route → scrape → return unified result ─────────
            # PrismPipeline handles cache reads/writes, WAF fallback, B2B2C
            # mode switching, and scraped_cache persistence internally.
            # If prism was not initialised (init error), fall back to the
            # legacy scrape_url() path to maintain zero-downtime guarantee.

            text, tech_stack, emails, phones = "", [], [], []
            prism_mode    = "legacy"
            fallback_used = False

            if prism is not None:
                hook_result   = prism.process_url(url, tenant_id)
                text          = hook_result.get("text", "")
                tech_stack    = hook_result.get("tech_stack", [])
                emails        = hook_result.get("emails", [])
                phones        = hook_result.get("phones", [])
                prism_mode    = hook_result.get("mode", "GeneralDomain")
                fallback_used = hook_result.get("fallback_used", False)

                # Shadow Learner: WalledGarden thin payloads are pre-tagged
                # by WalledGardenHook with [SHADOW_LEARNER_THIN_PAYLOAD].
                # This marker is read by the threshold logic below.

                if fallback_used:
                    print(f"[PRISM] Fallback used for {url}: {prism_mode}")

                # Last-resort: if Prism returned empty text (e.g. JS SPA that
                # httpx cannot render), defer to scraper-heavy exactly as before.
                if not text:
                    print(f"[PRISM] Empty text from {prism_mode} for {url}. "
                          f"Queueing async task to scraper-heavy.")
                    from google.cloud import tasks_v2 as _tv2
                    _tc     = _tv2.CloudTasksClient()
                    _parent = _tc.queue_path(PROJECT_ID, LOCATION, QUEUE)
                    _task   = {
                        "http_request": {
                            "http_method": _tv2.HttpMethod.POST,
                            "url": SCRAPER_HEAVY_URL,
                            "headers": {"Content-Type": "application/json"},
                            "body": json.dumps({
                                "url": url, "lead_id": lead_id, "tenant_id": tenant_id,
                                "campaign_id": campaign_id, "bio": bio,
                                "target_domain": target_domain,
                                "preferences_weights": preferences_weights
                            }).encode()
                        }
                    }
                    _tc.create_task(parent=_parent, task=_task)
                    continue

            else:
                # Prism init failed — legacy scrape_url() path
                try:
                    text, tech_stack, emails, phones = scrape_url(url)
                    prism_mode = "legacy_scrape_url"
                    scrape_success += 1
                except ValueError as e:
                    if str(e) == "DEFERRED":
                        print(f"[DEFERRED] Queueing async task to scraper-heavy for {url}")
                        from google.cloud import tasks_v2 as _tv2
                        _tc = _tv2.CloudTasksClient()
                        _parent = _tc.queue_path(PROJECT_ID, LOCATION, QUEUE)
                        _task = {
                            "http_request": {
                                "http_method": _tv2.HttpMethod.POST,
                                "url": SCRAPER_HEAVY_URL,
                                "headers": {"Content-Type": "application/json"},
                                "body": json.dumps({
                                    "url": url, "lead_id": lead_id, "tenant_id": tenant_id,
                                    "campaign_id": campaign_id, "bio": bio,
                                    "target_domain": target_domain,
                                    "preferences_weights": preferences_weights
                                }).encode()
                            }
                        }
                        _tc.create_task(parent=_parent, task=_task)
                        continue
                    print(f"[SCRAPE FAIL] {url}: {e}")
                    doc_ref.update({"status": "failed_scrape", "error": str(e)})
                    scrape_failed += 1
                    continue

            # Stamp prism_mode on the stub (for analytics in Cloud Logging)
            try:
                doc_ref.update({"prism_mode": prism_mode, "fallback_used": fallback_used})
            except Exception:
                pass

            if text:
                bot_keywords = ["Cloudflare Ray ID", "Please verify you are human",
                                "Enable JavaScript and cookies to continue",
                                "Checking if the site connection is secure",
                                "Access Denied", "403 Forbidden"]
                if any(kw.lower() in text.lower() for kw in bot_keywords):
                    doc_ref.update({"status": "failed", "error": "Blocked by Cloudflare/WAF"})
                    scrape_failed += 1
                    continue

                shard_id = random.randint(0, 9)
                db.collection("usage_metrics").document(tenant_id).collection("shards") \
                    .document(str(shard_id)).set({"gemini_calls": firestore.Increment(1)}, merge=True)
                # FIX 1: wallet_shards retained for analytics; authoritative settlement via orchestrator
                db.collection("users").document(tenant_id).collection("wallet_shards") \
                    .document(str(shard_id)).set({"consumed_credits": firestore.Increment(1)}, merge=True)

                context_payload, native_hiring_intent = deep_context_serper_dork(target_domain, tenant_id, sourcing_vector, source_url=url)

                # RLHF Fit Score
                fit_score = 0
                if native_hiring_intent:
                    fit_score += preferences_weights.get("hiring_intent", 0)
                for tech in tech_stack:
                    fit_score += preferences_weights.get(f"tech_{tech}", 0)
                if fit_score <= -3:
                    print(f"[RLHF] Dropping {target_domain} (fit_score={fit_score}).")
                    doc_ref.delete()
                    # ── FIX 1: RLHF gate — release orphaned exclusivity lock ──
                    # The lock was acquired before processing started and will
                    # never be automatically released if we skip the score gate.
                    # Explicitly delete it so the domain is not permanently locked.
                    try:
                        db.collection("global_lead_locks").document(lock_entity).delete()
                        print(f"[RLHF] Orphaned lock released for entity: {lock_entity}")
                    except Exception as _rlhf_lock_err:
                        print(f"[RLHF] Lock release failed (non-fatal): {_rlhf_lock_err}")
                    continue

                try:
                    evaluation = final_score_and_dm(text, active_campaigns, context_payload, tech_stack, source_url=url)
                except TimeoutError:
                    db.collection("leads").document(lead_id).update({"status": "failed", "error": "Vertex AI timeout"})
                    scrape_failed += 1
                    continue
                except Exception as e:
                    db.collection("leads").document(lead_id).update({"status": "failed", "error": str(e)})
                    scrape_failed += 1
                    continue

                # ── P4: Dynamic acceptance threshold ─────────────────────────
                # Snippet-sourced leads (< 500 chars) lack DOM depth.
                # Gemini cannot confidently score them >= 7 even with clear intent.
                # Lower to >= 6 for thin payloads so snippet leads are not mass-dropped.
                # V18: WalledGardenHook tags thin payloads with [SHADOW_LEARNER_THIN_PAYLOAD]
                # prefix — also treated as thin regardless of char count.
                _is_shadow_thin  = text.strip().startswith("[SHADOW_LEARNER_THIN_PAYLOAD]")
                is_thin_payload  = _is_shadow_thin or len(text.strip()) < 500
                accept_threshold = 6 if is_thin_payload else 7
                print(f"[THRESHOLD] Payload: {len(text)} chars → threshold: {accept_threshold} "
                      f"(thin={is_thin_payload}, shadow={_is_shadow_thin}, mode={prism_mode})")


                if evaluation.get("score", 0) >= accept_threshold:
                    contact_endpoints = list(evaluation.get("contact_endpoints", []))
                    existing_uris     = {e["uri"] for e in contact_endpoints}
                    for em in (emails or [])[:3]:
                        if em and em not in existing_uris:
                            contact_endpoints.append({"platform": "email", "uri": em})
                            existing_uris.add(em)
                    for ph in (phones or [])[:2]:
                        if ph and ph not in existing_uris:
                            contact_endpoints.append({"platform": "other", "uri": ph})
                            existing_uris.add(ph)

                    # ── Universal Data Contract write (dispatch / Prism path) ──
                    lead_payload = {
                        "id":                           lead_id,
                        "source_url":                   url,
                        "tenant_id":                    tenant_id,
                        "origin_engine":                "cartographer",
                        "score":                        evaluation.get("score", 0),
                        "matched_campaign_ids":         evaluation.get("matched_campaign_ids", []),
                        "matched_campaigns":            [campaign_id],          # UI filter field
                        "campaign_id":                  campaign_id,            # UI scalar fallback
                        "trend_mapped":                 evaluation.get("trend_mapped", False),
                        "highest_campaign_id":          evaluation.get("highest_campaign_id", "Unknown"),
                        "pain_point":                   evaluation.get("pain_point", ""),
                        "dm":                           evaluation.get("dm", ""),
                        "intent_signal":                evaluation.get("intent_signal", ""),
                        "hiring_intent_found":          evaluation.get("hiring_intent_found", "No"),
                        "tech_stack_found":             evaluation.get("tech_stack_found", []),
                        "icebreaker_angle":             evaluation.get("icebreaker_angle", ""),
                        "contact_endpoints":            contact_endpoints,
                        "decision_maker_name":          evaluation.get("decision_maker_name", "Unknown"),
                        "decision_maker_title":         evaluation.get("decision_maker_title", "Unknown"),
                        "company_size_tier":            evaluation.get("company_size_tier", "Unknown"),
                        "primary_objection_hypothesis": evaluation.get("primary_objection_hypothesis", "Unknown"),
                        "company_name":                 evaluation.get("company_name"),
                        "dossier_text":                 None,
                        "sourcing_vector":               sourcing_vector,
                        "confidence_tier":               url_to_tier.get(url, "High"),
                        "prism_mode":                   prism_mode,
                        "prism_fallback":                fallback_used,
                        "status":                        "new",
                        "is_in_crm":                     False,  # Required: UI query .where('is_in_crm','==',false)
                    }
                    validate_and_update_lead(lead_payload, doc_ref)
                    _settle_credit(tenant_id, "success", lead_id=lead_id)  # FIX 1B: lead_id = idempotency key

                    scrape_success += 1

                    # Meta WhatsApp Business API Trigger
                    if evaluation.get("score", 0) >= 8:
                        tenant_doc       = db.collection("users").document(tenant_id).get().to_dict() or {}
                        wa_token_encrypted = tenant_doc.get("wa_token")
                        wa_phone_id      = tenant_doc.get("wa_phone_id")
                        admin_phone      = tenant_doc.get("admin_phone")
                        wa_token         = None
                        if wa_token_encrypted:
                            try:
                                wa_token = cipher_suite.decrypt(wa_token_encrypted.encode()).decode()
                            except:
                                wa_token = wa_token_encrypted
                        if wa_token and wa_phone_id and admin_phone:
                            wa_payload = {
                                "messaging_product": "whatsapp",
                                "to": admin_phone, "type": "interactive",
                                "interactive": {
                                    "type": "button",
                                    "body": {"text": f"🔥 Hot Lead!\n{url}\nScore: {evaluation.get('score')}/10\n{evaluation.get('pain_point')}\n\nDM: {evaluation.get('dm')}"},
                                    "action": {"buttons": [
                                        {"type": "reply", "reply": {"id": f"approve_{lead_id}", "title": "✅ Approve"}},
                                        {"type": "reply", "reply": {"id": f"ignore_{lead_id}",  "title": "🚫 Ignore"}}
                                    ]}
                                }
                            }
                            try:
                                httpx.post(
                                    f"https://graph.facebook.com/v18.0/{wa_phone_id}/messages",
                                    json=wa_payload,
                                    headers={"Authorization": f"Bearer {wa_token}", "Content-Type": "application/json"},
                                    timeout=5
                                )
                            except Exception as wa_e:
                                print(f"[WA] Meta POST failed: {wa_e}")

                    all_results.append({"url": url, "score": evaluation.get("score")})
                else:
                    # Score below threshold — clean delete (not a ghost)
                    print(f"[SCORE GATE] {url} scored {evaluation.get('score', 0)} < {accept_threshold}. Deleting stub.")
                    doc_ref.delete()
                    # ── FIX 1: Score gate — release orphaned exclusivity lock ──
                    # The lock_entity was acquired at the top of this URL's loop.
                    # If we delete the lead stub here, the lock document remains
                    # and permanently blocks this domain for the 14-day window
                    # even though no lead was persisted. Delete it explicitly.
                    try:
                        db.collection("global_lead_locks").document(lock_entity).delete()
                        print(f"[SCORE GATE] Orphaned lock released for entity: {lock_entity}")
                    except Exception as _sg_lock_err:
                        print(f"[SCORE GATE] Lock release failed (non-fatal): {_sg_lock_err}")
            else:
                # ── P3: text is empty after all code paths — ghost state prevention ──
                print(f"[DEAD PAYLOAD] No text for {url} after all paths. Marking failed_scrape.")
                doc_ref.update({"status": "failed_scrape", "error": "Empty DOM — no cache, no snippet, no scrape"})
                scrape_failed += 1

        except Exception as loop_e:
            print(f"[CONSUMER] Pipeline loop crashed for {url}: {loop_e}")
            try:
                db.collection('leads').document(lead_id).update(
                    {'status': 'failed', 'error': 'Consumer pipeline crash'}
                )
            except:
                pass
            scrape_failed += 1
            continue

    print(f"[FUNNEL] Campaign: {campaign_id} | Scraper Success: {scrape_success} | Failed/Timeout: {scrape_failed}")
    return jsonify({"processed_leads": len(all_results), "scrape_success": scrape_success, "scrape_failed": scrape_failed}), 200
def extract_dense_payload(text, bio):
    import re
    paragraphs = [p.strip() for p in text.split('\n') if len(p.strip()) > 30]
    bio_words = set(re.findall(r'\b\w{4,}\b', bio.lower()))
    about_us_terms = {"we are", "founded", "team", "mission", "services", "our goal", "about us"}
    
    scored_paragraphs = []
    for p in paragraphs:
        raw_p = p.lower()
        score = 0
        for term in about_us_terms:
            if term in raw_p:
                score += 3
        words = set(re.findall(r'\b\w{4,}\b', raw_p))
        score += len(words.intersection(bio_words))
        scored_paragraphs.append((score, p))
        
    scored_paragraphs.sort(key=lambda x: x[0], reverse=True)
    return "\n\n".join([p for s, p in scored_paragraphs[:10]])

@app.route("/finalize", methods=["POST"])
def finalize():
    # Receive decoupled webhook from scraper-heavy
    data = request.json
    text = data.get("text", "")
    emails = data.get("emails", [])
    phones = data.get("phones", [])
    lead_id = data.get("lead_id")
    tenant_id = data.get("tenant_id")
    campaign_id = data.get("campaign_id")
    bio = data.get("bio", "")
    url = data.get("url", "")

    from google.cloud.firestore_v1.base_query import FieldFilter
    # V18 Multi-Campaign Swarm: Pre-fetch ALL active campaigns for tenant ecosystem
    active_campaigns_docs = db.collection("campaigns").where(filter=FieldFilter("tenant_id", "==", tenant_id)).where(filter=FieldFilter("status", "==", "active")).stream()
    active_campaigns = []
    for doc in active_campaigns_docs:
        d = doc.to_dict()
        d["id"] = doc.id
        active_campaigns.append(d)
    if not active_campaigns:
        active_campaigns = [{"id": campaign_id, "bio": bio}]
    target_domain = data.get("target_domain", "")
    preferences_weights = data.get("preferences_weights", {})
    tech_stack = ["Fallback Scraper Used"]
    
    if not lead_id or not tenant_id:
        return jsonify({"error": "Missing crucial context"}), 400
        
    doc_ref = db.collection("leads").document(lead_id)

    # ── FIX 1A: Idempotency Guard ───────────────────────────────────────────────
    # Cloud Tasks at-least-once delivery: a network drop between Cloud Run's load
    # balancer and Cloud Tasks after a clean HTTP 200 causes an invisible retry.
    # Without this guard: Gemini fires twice, wallet_shards gets a second
    # Increment(1), and set(merge=True) overwrites Firestore with different values.
    # Guard: read current lead status; if already beyond 'processing', acknowledge
    # the task (return 200) without executing any downstream logic.
    try:
        _idem_snap   = doc_ref.get().to_dict() or {}
        _idem_status = _idem_snap.get("status")
        if _idem_status not in ("processing", "failed_scrape", None):
            print(f"[FINALIZE] Idempotency guard: lead {lead_id} already "
                  f"in status='{_idem_status}'. Acknowledging task without reprocessing.")
            return jsonify({"status": "already_processed", "lead_status": _idem_status}), 200
    except Exception as _idem_err:
        # On Firestore read failure, proceed rather than permanently drop
        # a valid finalize callback. Rare double-charge is the lesser evil.
        print(f"[FINALIZE] Idempotency read failed (proceeding cautiously): {_idem_err}")

    if not text:
        doc_ref.update({"status": "failed_scrape", "error": "scraper-heavy returned empty text"})
        return jsonify({"status": "dropped empty text"}), 200
        
    bot_keywords = ["Cloudflare Ray ID", "Please verify you are human", "Enable JavaScript and cookies to continue", "Checking if the site connection is secure", "Access Denied", "403 Forbidden"]
    if any(keyword.lower() in text.lower() for keyword in bot_keywords):
        doc_ref.update({"status": "failed", "error": "Blocked by Cloudflare/WAF"})
        return jsonify({"status": "blocked by waf"}), 200
        
    try:
        # Python Fast-Fail Gate
        tenant_doc = db.collection("users").document(tenant_id).get().to_dict() or {}
        global_b2b_blocklist = ['add to cart', 'shopping bag', 'checkout', 'shipping policy', 'return policy', 'in stock']
        dynamic_blocklist = tenant_doc.get("dynamic_blocklist", [])
        b2b_blacklist = global_b2b_blocklist + [str(x).lower() for x in dynamic_blocklist]
        
        fail_score = sum(text.lower().count(term) for term in b2b_blacklist)
        if fail_score > 3:
            doc_ref.update({"status": "failed", "error": "Dropped by Python Heuristics (Cost Saved)"})
            return jsonify({"status": "heuristic_drop"}), 200

        # Token Reduction via Density Extraction
        dense_text = extract_dense_payload(text, bio)

        # Re-enter processing flow
        import datetime
        expire_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
        cache_ref = db.collection("scraped_cache").document(url.replace('/','_'))
        
        cache_ref.set({
            "url": url, 
            "text": safe_truncate(dense_text), 
            "tech_stack": tech_stack, 
            "emails": emails, 
            "phones": phones,
            "expireAt": expire_at
        }, merge=True)
        
        shard_id = random.randint(0, 9)
        db.collection("usage_metrics").document(tenant_id).collection("shards").document(str(shard_id)).set({"gemini_calls": firestore.Increment(1)}, merge=True)
        db.collection("users").document(tenant_id).collection("wallet_shards").document(str(shard_id)).set({"consumed_credits": firestore.Increment(1)}, merge=True)
        
        # Fetch sourcing_vector from the lead doc (scraper-heavy payload doesn't include it)
        lead_doc_data   = doc_ref.get().to_dict() or {}
        sourcing_vector = lead_doc_data.get("sourcing_vector", "Classic B2B")

        context_payload, native_hiring_intent = deep_context_serper_dork(target_domain, tenant_id, sourcing_vector)
        
        try:
            docs = db.collection("leads").where("tenant_id", "==", tenant_id).where("status", "==", "converted").order_by("updatedAt", direction=firestore.Query.DESCENDING).limit(3).stream()
            historical_dms = [doc.to_dict().get("dm") for doc in docs if doc.to_dict().get("dm")]
            
            evaluation = final_score_and_dm(dense_text, active_campaigns, context_payload, tech_stack, historical_dms, source_url=url)
        except TimeoutError:
            doc_ref.update({"status": "failed", "error": "Vertex AI timeout"})
            return jsonify({"status": "timeout"}), 200
        except Exception as e:
            doc_ref.update({"status": "failed", "error": str(e)})
            return jsonify({"status": "failed"}), 200
        
        # P4: Dynamic threshold — scraper-heavy always provides full DOM text, so
        # dense_text >= 500 chars is expected; standard >= 7 applies.
        # Guard for edge cases where Playwright returned a thin extract.
        is_thin_payload  = len(dense_text.strip()) < 500
        accept_threshold = 6 if is_thin_payload else 7
        print(f"[FINALIZE THRESHOLD] Payload: {len(dense_text)} chars → threshold: {accept_threshold}")

        if evaluation.get("score", 0) >= accept_threshold:
            # V14: Polymorphic contact merge — LLM endpoints + Playwright-scraped contacts
            contact_endpoints = list(evaluation.get("contact_endpoints", []))
            existing_uris = {e["uri"] for e in contact_endpoints}
            for em in (emails or [])[:3]:
                if em and em not in existing_uris:
                    contact_endpoints.append({"platform": "email", "uri": em})
                    existing_uris.add(em)
            for ph in (phones or [])[:2]:
                if ph and ph not in existing_uris:
                    contact_endpoints.append({"platform": "other", "uri": ph})
                    existing_uris.add(ph)

            # ── Universal Data Contract write (finalize/scraper-heavy path) ──
            lead_payload = {
                "id":                           lead_id,
                "source_url":                   url,
                "tenant_id":                    tenant_id,
                "origin_engine":                "cartographer",
                "score":                        evaluation.get("score", 0),
                "matched_campaign_ids":         evaluation.get("matched_campaign_ids", []),
                "matched_campaigns":            [campaign_id],          # UI filter field
                "campaign_id":                  campaign_id,            # UI scalar fallback
                "trend_mapped":                 evaluation.get("trend_mapped", False),
                "highest_campaign_id":          evaluation.get("highest_campaign_id", "Unknown"),
                "pain_point":                   evaluation.get("pain_point", ""),
                "dm":                           evaluation.get("dm", ""),
                "intent_signal":                evaluation.get("intent_signal", ""),
                "hiring_intent_found":          evaluation.get("hiring_intent_found", "No"),
                "tech_stack_found":             evaluation.get("tech_stack_found", []),
                "icebreaker_angle":             evaluation.get("icebreaker_angle", ""),
                "contact_endpoints":            contact_endpoints,
                "decision_maker_name":          evaluation.get("decision_maker_name", "Unknown"),
                "decision_maker_title":         evaluation.get("decision_maker_title", "Unknown"),
                "company_size_tier":            evaluation.get("company_size_tier", "Unknown"),
                "primary_objection_hypothesis": evaluation.get("primary_objection_hypothesis", "Unknown"),
                "company_name":                 evaluation.get("company_name"),
                "dossier_text":                 None,
                "sourcing_vector":               sourcing_vector,
                "status":                        "new",
                "is_in_crm":                     False,  # Required: UI query .where('is_in_crm','==',false)
            }
            validate_and_update_lead(lead_payload, doc_ref)
            
            # Simplified WhatsApp Meta Call (V13)
            if evaluation.get("score", 0) >= 8:
                tenant_doc = db.collection("users").document(tenant_id).get().to_dict() or {}
                wa_token_encrypted = tenant_doc.get("wa_token")
                wa_phone_id = tenant_doc.get("wa_phone_id")
                admin_phone = tenant_doc.get("admin_phone")
                
                wa_token = None
                if wa_token_encrypted:
                    try:
                        from google.cloud import kms
                        import base64
                        kms_client = kms.KeyManagementServiceClient()
                        key_name = get_secret("kms_wa_key_path").strip()
                        ciphertext = base64.b64decode(wa_token_encrypted)
                        response = kms_client.decrypt(
                            request={'name': key_name, 'ciphertext': ciphertext}
                        )
                        wa_token = response.plaintext.decode('utf-8')
                    except Exception as e:
                        print(f"KMS Decryption failed: {e}. Attempting Fernet fallback.")
                        try:
                            wa_token = cipher_suite.decrypt(wa_token_encrypted.encode()).decode()
                        except:
                            wa_token = wa_token_encrypted
                        
                if wa_token and wa_phone_id and admin_phone:
                    wa_payload = {
                        "messaging_product": "whatsapp",
                        "to": admin_phone,
                        "type": "interactive",
                        "interactive": {
                            "type": "button",
                            "body": {
                                "text": f"🔥 Hot Lead Found!\nCompany: {url}\nScore: {evaluation.get('score')}/10\nWhy: {evaluation.get('pain_point')}\nTech Stack: {', '.join(evaluation.get('tech_stack_found', []))}\nHiring: {evaluation.get('hiring_intent_found', '')}\n\nDrafted DM: {evaluation.get('dm')}"
                            },
                            "action": {
                                "buttons": [
                                    {"type": "reply", "reply": {"id": f"approve_{lead_id}", "title": "✅ Approve"}},
                                    {"type": "reply", "reply": {"id": f"ignore_{lead_id}", "title": "🚫 Ignore"}}
                                ]
                            }
                        }
                    }
                    try:
                        httpx.post(f"https://graph.facebook.com/v18.0/{wa_phone_id}/messages", json=wa_payload, headers={"Authorization": f"Bearer {wa_token}", "Content-Type": "application/json"}, timeout=5)
                    except:
                        pass
        else:
            # Score below threshold — delete stub
            doc_ref.delete()
            # ── FIX 1: finalize() score gate — release orphaned exclusivity lock ──
            # Retrieve the lock_entity stored on the lead stub at creation time
            # (dispatch stored it as lead_doc_data["lock_entity"]).
            # Deleting this ensures the domain is not permanently blocked after
            # a failed finalize() scoring pass.
            try:
                _finalize_lock_entity = lead_doc_data.get("lock_entity")
                if _finalize_lock_entity:
                    db.collection("global_lead_locks").document(_finalize_lock_entity).delete()
                    print(f"[FINALIZE SCORE GATE] Orphaned lock released for entity: {_finalize_lock_entity}")
            except Exception as _fl_err:
                print(f"[FINALIZE SCORE GATE] Lock release failed (non-fatal): {_fl_err}")
            
    except Exception as hook_err:
        doc_ref.update({"status": "failed", "error": "Finalize webhook crash"})
        
    return jsonify({"status": "finalized"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
