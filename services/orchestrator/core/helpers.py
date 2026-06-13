"""
Orchestrator V23 — Shared Helper Library.

All business-logic helpers extracted from the legacy main.py monolith.
Blueprints import from here; nothing imports from main.py anymore.

Functions:
  parse_base_path           — Ontology map key derivation
  get_service_account_email — GCE metadata SA email fetch (cached)
  check_quota               — Tenant wallet quota check
  reserve_credits           — Atomic Firestore credit reservation
  release_reservation       — Atomic credit refund
  get_vector_weights        — Read sourcing vector weights from Firestore
  classify_sourcing_vector  — Gemini-backed vector classifier (P9)
  _get_router_config        — Epsilon-greedy router config fetch
  _pop_from_predictive_cache — CAS transactional pop from predictive cache
  _enqueue_bq_telemetry_task — Cloud Tasks BQ telemetry enqueue
  _handle_bq_push_task       — Synchronous BQ streaming insert
  _async_neg_signal_insert   — Fire-and-forget neg signal BQ insert
  _async_shadow_track        — Fire-and-forget shadow tracker BQ upsert
  _atomic_settle_txn         — Idempotent credit settlement transaction
  _call_gemini_bounded       — Hard-capped Gemini wrapper (15s)
  handle_purge               — DPDP tenant data erasure
"""
from __future__ import annotations

import concurrent.futures as _cf
import datetime
import json
import os
import re as _re_mod
import threading as _threading
import time
import urllib.request
from collections import Counter as _Counter
from urllib.parse import urlparse

from google.cloud.firestore_v1.transaction import transactional as _fs_transactional
from google.cloud import firestore

from core.clients import get_db, get_tasks_client  # type: ignore[import]
from core.config import (  # type: ignore[import]
    PROJECT_ID, LOCATION, QUEUE, ORCHESTRATOR_URL,
)

# ---------------------------------------------------------------------------
# Lazy DB accessor — all helpers call db() to get the shared Firestore client
# ---------------------------------------------------------------------------
def _db():
    return get_db()


def _tc():
    return get_tasks_client()


# =============================================================================
# ONTOLOGY MAP
# =============================================================================
_SOCIAL_ONTOLOGY_DOMAINS = {
    "reddit.com", "facebook.com", "linkedin.com", "quora.com",
    "kaggle.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
}


def parse_base_path(url: str) -> str:
    """Derive ontology_map collection key from a URL."""
    try:
        parsed   = urlparse(url if url.startswith("http") else f"https://{url}")
        hostname = parsed.hostname or ""
        domain   = hostname.removeprefix("www.")
        if not domain:
            return "unknown"
        if any(domain.endswith(s) for s in _SOCIAL_ONTOLOGY_DOMAINS):
            segments  = [s for s in parsed.path.split("/") if s]
            return "/".join([domain] + segments[:2])
        return domain
    except Exception:
        return "unknown"


# =============================================================================
# SERVICE ACCOUNT EMAIL (GCE metadata)
# =============================================================================
def get_service_account_email() -> str:
    url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            print(f"[SA EMAIL] Attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(1.5 ** attempt)
    print("[SA EMAIL] Critical: metadata fetch permanently failed.")
    return ""


# =============================================================================
# QUOTA / WALLET HELPERS
# =============================================================================
def check_quota(tenant_id: str):
    """Return (is_valid: bool, status_code: int, message: str).

    Postmortem Fix #1: Unified dual-accounting-path read.
    Previously read only `consumed_credits + shard_sum`, while _atomic_settle_txn
    writes `total_consumed`. These two paths diverged silently over time.
    Fix: take max(total_consumed, consumed_credits + shard_sum) to cover both
    schema paths regardless of migration state.
    """
    user_doc = _db().collection("users").document(tenant_id).get()
    if not user_doc.exists:
        return False, 401, "Unknown identity."
    data = user_doc.to_dict()
    if data.get("role") == "super_admin":
        return True, 200, "OK"
    if data.get("approval_status") != "approved":
        return False, 403, "Your application is under review. Please wait for L0 approval."
    wallet        = data.get("wallet", {})
    credits       = wallet.get("allocated_credits", 0)
    # Path A: new atomic settle path (total_consumed field)
    total_consumed = int(wallet.get("total_consumed", 0) or 0)
    # Path B: legacy shard path (consumed_credits field + shard subcollection)
    legacy_consumed = int(wallet.get("consumed_credits", 0) or 0)
    shard_sum = sum(
        s.to_dict().get("consumed_credits", 0)
        for s in _db().collection("users").document(tenant_id)
                       .collection("wallet_shards").stream()
    )
    # Use whichever path shows higher consumption — prevents over-delivery on drift
    consumed = max(total_consumed, legacy_consumed + shard_sum)
    if (credits - consumed) <= 0:
        return False, 402, "Beta quota exhausted. Contact admin to reload."
    return True, 200, "OK"


@_fs_transactional
def _reserve_credits_txn(transaction, user_ref, batch_cost: int):
    snapshot  = user_ref.get(transaction=transaction)
    if not snapshot.exists:
        raise ValueError("Tenant wallet document does not exist.")
    wallet    = (snapshot.to_dict() or {}).get("wallet", {})
    allocated = int(wallet.get("allocated_credits", 0) or 0)
    consumed  = int(wallet.get("total_consumed",    0) or 0)
    reserved  = int(wallet.get("reserved_credits",  0) or 0)
    available = allocated - consumed - reserved
    if available < batch_cost:
        raise ValueError(
            f"Insufficient credits: {available} available, {batch_cost} requested."
        )
    transaction.update(user_ref, {"wallet.reserved_credits": firestore.Increment(batch_cost)})
    return available - batch_cost


def reserve_credits(tenant_id: str, batch_cost: int) -> bool:
    if batch_cost <= 0:
        return True
    user_ref = _db().collection("users").document(tenant_id)
    txn      = _db().transaction()
    try:
        _reserve_credits_txn(txn, user_ref, batch_cost)
        return True
    except (ValueError, Exception) as e:
        print(f"[RESERVE] Denied for {tenant_id}: {e}")
        return False


def release_reservation(tenant_id: str, count: int = 1):
    if count <= 0:
        return
    try:
        _db().collection("users").document(tenant_id).update(
            {"wallet.reserved_credits": firestore.Increment(-count)}
        )
    except Exception as e:
        print(f"[REFUND] Failed for {tenant_id}: {e}")


@_fs_transactional
def _atomic_settle_txn(transaction, user_ref, lead_ref, outcome, count):
    if lead_ref is not None:
        lead_snap = lead_ref.get(transaction=transaction)
        lead_data = lead_snap.to_dict() if lead_snap.exists else {}
        if lead_data.get("credit_settled"):
            raise ValueError("already_settled")
        transaction.update(lead_ref, {"credit_settled": True})
    if outcome == "success":
        transaction.update(user_ref, {
            "wallet.total_consumed":   firestore.Increment(count),
            "wallet.reserved_credits": firestore.Increment(-count),
        })
    else:
        transaction.update(user_ref, {"wallet.reserved_credits": firestore.Increment(-count)})


# =============================================================================
# VECTOR WEIGHTS / SYNAPTIC ROUTER
# =============================================================================
def get_vector_weights() -> dict:
    try:
        doc = _db().collection("system_telemetry").document("vector_weights").get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        print(f"[VECTOR WEIGHTS] Read failed: {e}")
    return {
        "Classic B2B": 10, "Social/Forum Listening": 8,
        "Review Hijacking": 5, "Maps/GMB Targeting": 3,
    }


_SOURCING_VECTOR_SCHEMA = {
    "type": "STRING",
    "enum": ["Social/Forum Listening", "Review Hijacking", "Classic B2B", "Maps/GMB Targeting"],
}


def classify_sourcing_vector(bio: str, industry_weights: dict) -> str:
    if not bio:
        return "Classic B2B"
    try:
        from vertexai.generative_models import GenerativeModel, GenerationConfig  # type: ignore[import]
        prompt = (
            f"Based on this user's business bio: '{bio}', and the global sourcing success "
            f"weights: {json.dumps(industry_weights)}, select the single optimal digital "
            "sourcing vector: \"Social/Forum Listening\", \"Review Hijacking\", "
            "\"Classic B2B\", or \"Maps/GMB Targeting\". Output ONLY the chosen string."
        )
        model    = GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(
            prompt,
            generation_config=GenerationConfig(
                response_mime_type="application/json",
                response_schema=_SOURCING_VECTOR_SCHEMA,
                temperature=0.1,
            ),
        )
        vector = json.loads(response.text) if response.text.strip().startswith('"') else response.text.strip().strip('"')
        valid  = {"Social/Forum Listening", "Review Hijacking", "Classic B2B", "Maps/GMB Targeting"}
        return vector if vector in valid else "Classic B2B"
    except Exception as e:
        print(f"[SYNAPTIC ROUTER] Classification failed: {e}")
        return "Classic B2B"


def _get_router_config(db_client=None) -> dict:
    db_client = db_client or _db()
    ref = db_client.collection("system_config").document("router")
    doc = ref.get()
    if not doc.exists:
        defaults = {"exploit_ratio": 0.10, "discovery_allocation": 0.15,
                    "initialized_at": firestore.SERVER_TIMESTAMP}
        ref.set(defaults)
        return defaults
    return doc.to_dict()


@_fs_transactional
def _cas_pop_one(transaction, cache_ref, leads_ref, now_utc):
    snap = cache_ref.get(transaction=transaction)
    if not snap.exists:
        raise ValueError("already_consumed")
    data   = snap.to_dict() or {}
    status = data.get("status")
    if status != "new":
        raise ValueError(f"status_changed: now '{status}'")
    expire_at = data.get("expire_at")
    if expire_at is not None:
        if hasattr(expire_at, "tzinfo") and expire_at.tzinfo is None:
            expire_at = expire_at.replace(tzinfo=datetime.timezone.utc)
        if expire_at <= now_utc:
            raise ValueError(f"expired: expire_at={expire_at.isoformat()}")
    promoted_data = dict(data)
    promoted_data.update({
        "status": "new", "promotedAt": firestore.SERVER_TIMESTAMP,
        "origin_engine": "autonomous",
        "expire_at": now_utc + datetime.timedelta(days=90),
    })
    transaction.set(leads_ref, promoted_data, merge=True)
    transaction.delete(cache_ref)
    return promoted_data


def _pop_from_predictive_cache(tenant_id: str, db_client, count: int) -> list:
    db_client = db_client or _db()
    if count <= 0:
        return []
    now_utc   = datetime.datetime.now(datetime.timezone.utc)
    from google.cloud.firestore_v1.base_query import FieldFilter
    cache_col = (
        db_client.collection("users").document(tenant_id)
                 .collection("predictive_cache")
    )
    try:
        candidates = list(
            cache_col
            .where(filter=FieldFilter("status",    "==", "new"))
            .where(filter=FieldFilter("expire_at", ">",  now_utc))
            .order_by("expire_at")
            .order_by("score", direction="DESCENDING")
            .limit(count * 3)
            .stream()
        )
    except Exception as e:
        print(f"[ROUTER-CAS] Cache query failed for {tenant_id}: {e}")
        return []

    promoted = []
    for cache_doc in candidates:
        if len(promoted) >= count:
            break
        cache_ref = cache_col.document(cache_doc.id)
        leads_ref = db_client.collection("leads").document(cache_doc.id)
        txn = db_client.transaction()
        try:
            lead_data = _cas_pop_one(txn, cache_ref, leads_ref, now_utc)
            promoted.append(lead_data)
        except (ValueError, Exception) as e:
            print(f"[ROUTER-CAS] Skip {cache_doc.id[:12]}: {e}")
    return promoted


# =============================================================================
# BQ TELEMETRY TASKS
# =============================================================================
def _enqueue_bq_telemetry_task(tenant_id: str, lead_dict: dict, status: str):
    if not ORCHESTRATOR_URL:
        return
    try:
        import hashlib, uuid
        raw_signal  = (lead_dict.get("intent_signal", "") or "") + "|" + (lead_dict.get("sourcing_vector", "") or "")
        intent_hash = hashlib.sha256(raw_signal.encode()).hexdigest()
        stripped    = {
            "score":           lead_dict.get("score"),
            "sourcing_vector": lead_dict.get("sourcing_vector"),
            "prism_mode":      lead_dict.get("prism_mode"),
            "tech_stack":      lead_dict.get("tech_stack_found", []),
            "hiring_intent":   lead_dict.get("hiring_intent_found"),
            "origin_engine":   lead_dict.get("origin_engine"),
            "campaign_id":     lead_dict.get("campaign_id"),
        }
        event_id     = str(uuid.uuid4())
        task_payload = {
            "event_id": event_id,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "tenant_id": tenant_id,
            "prism_mode": lead_dict.get("prism_mode") or "GeneralDomain",
            "conversion_status": status,
            "intent_hash": intent_hash,
            "raw_signal_payload": json.dumps(stripped),
        }
        from google.cloud import tasks_v2 as tv2
        clients      = _tc()
        queue_path   = clients.queue_path(PROJECT_ID, LOCATION, QUEUE)
        sa_email     = get_service_account_email().strip()
        target_url   = f"{ORCHESTRATOR_URL}/api/internal/telemetry/bq-push"
        task: dict   = {
            "http_request": {
                "http_method": tv2.HttpMethod.POST, "url": target_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(task_payload).encode(),
            }
        }
        if sa_email:
            task["http_request"]["oidc_token"] = {
                "service_account_email": sa_email, "audience": ORCHESTRATOR_URL,
            }
        clients.create_task(request={"parent": queue_path, "task": task})
    except Exception as e:
        print(f"[BQ TASK] Non-fatal enqueue error: {e}")


def _handle_bq_push_task(payload: dict) -> bool:
    try:
        from google.cloud import bigquery as _bq_lib
        # REGIONALITY FIX: explicit location prevents default US routing
        bq        = _bq_lib.Client(project=PROJECT_ID, location="asia-south1")
        table_ref = f"{PROJECT_ID}.swarm_analytics.rlhf_events"
        row = {
            "event_id":           payload.get("event_id"),
            "timestamp":          payload.get("timestamp"),
            "tenant_id":          payload.get("tenant_id"),
            "prism_mode":         payload.get("prism_mode"),
            "conversion_status":  payload.get("conversion_status"),
            "intent_hash":        payload.get("intent_hash"),
            "raw_signal_payload": payload.get("raw_signal_payload"),
        }
        errors = bq.insert_rows_json(table_ref, [row])
        return len(errors) == 0
    except Exception as e:
        print(f"[BQ INSERT] Failed: {e}")
        return False


# =============================================================================
# NEGATIVE SIGNAL (async BQ)
# =============================================================================
def _do_neg_signal_insert(entity_name: str, root_domain: str, rejection_reason: str, tenant_id: str):
    try:
        from google.cloud import bigquery as _bq
        import datetime as _dt, uuid as _uuid
        # REGIONALITY FIX: explicit location prevents default US routing
        bq        = _bq.Client(project=PROJECT_ID, location="asia-south1")
        table_ref = f"{PROJECT_ID}.swarm_analytics.Negative_Signals"
        row = {
            "entity_name":      entity_name, "root_domain": root_domain,
            "rejection_reason": rejection_reason, "tenant_id": tenant_id,
            "timestamp":        _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        errors = bq.insert_rows_json(table_ref, [row])
        if errors:
            print(f"[NEG SIGNAL] BQ error: {errors}")
    except Exception as e:
        print(f"[NEG SIGNAL] Non-blocking error: {e}")


def _async_neg_signal_insert(entity_name: str, root_domain: str, rejection_reason: str, tenant_id: str):
    try:
        t = _threading.Thread(
            target=_do_neg_signal_insert,
            args=(entity_name, root_domain, rejection_reason, tenant_id),
            daemon=True,
        )
        t.start()
    except Exception as e:
        print(f"[NEG SIGNAL] Thread spawn failed: {e}")


# =============================================================================
# SHADOW TRACKER (async BQ)
# =============================================================================
_SHADOW_STOP_WORDS = frozenset({
    "the","and","for","are","but","not","you","all","can","was","one","our",
    "out","get","has","its","may","new","now","see","too","use","way","who",
    "did","put","say","that","this","with","have","from","they","know","want",
    "been","good","much","some","time","very","when","come","here","just",
    "like","long","make","many","more","only","over","such","take","than",
    "them","well","were","will","also","into","most","their","there","these",
    "what","your","about","which","would","could","after","where","while",
})


def _extract_ngrams(text: str, n_min: int = 2, n_max: int = 3, top_k: int = 5) -> list:
    if not text:
        return []
    tokens       = _re_mod.findall(r"\b[a-z]{3,}\b", text.lower())
    clean_tokens = [t for t in tokens if t not in _SHADOW_STOP_WORDS]
    ngrams       = []
    for n in range(n_min, n_max + 1):
        for i in range(len(clean_tokens) - n + 1):
            ngrams.append(" ".join(clean_tokens[i : i + n]))
    counter = _Counter(ngrams)
    return [ng for ng, _ in counter.most_common(top_k)]


def _do_shadow_track(persona_category: str, pain_point: str, tenant_id: str):
    """Stream shadow track n-grams to BigQuery.

    Postmortem Fix #4: The previous implementation used a BigQuery MERGE DML
    with an UNNEST of inline STRUCT literals:
        MERGE T USING (SELECT * FROM UNNEST([STRUCT(@tenant_id ... @ng_0 ...)]))
    This pattern is NOT valid in BQ streaming DML — the UNNEST source must be
    a named table or CTE, not an inline array of structs. The query failed with
    'Unrecognised name: ng_0' on every call, swallowed by bare except → print.
    Result: Intent_Keywords table was permanently empty, RLHF never activated.

    Fix: Replace with streaming insert_rows_json. One row per n-gram per call.
    The query_brain SUM(yield_weight) threshold correctly aggregates these rows.
    RLHF now activates after the very first approved lead.
    """
    try:
        from google.cloud import bigquery as _bq
        import datetime as _dt
        ngrams = _extract_ngrams(pain_point)
        if not ngrams:
            return
        bq        = _bq.Client(project=PROJECT_ID, location="asia-south1")
        table_ref = f"{PROJECT_ID}.swarm_analytics.Intent_Keywords"
        now_iso   = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            {
                "tenant_id":        tenant_id,
                "persona_category": persona_category,
                "n_gram":           ng,
                "occurrence_count": 1,
                "yield_weight":     1.0,
                "last_seen":        now_iso,
            }
            for ng in ngrams
        ]
        errors = bq.insert_rows_json(table_ref, rows)
        if errors:
            print(f"[SHADOW TRACKER] BQ insert errors: {errors}")
    except Exception as e:
        print(f"[SHADOW TRACKER] Error: {e}")


def _async_shadow_track(persona_category: str, pain_point: str, tenant_id: str):
    try:
        t = _threading.Thread(
            target=_do_shadow_track,
            args=(persona_category, pain_point, tenant_id),
            daemon=True,
        )
        t.start()
    except Exception as e:
        print(f"[SHADOW TRACKER] Thread spawn failed: {e}")


# =============================================================================
# GEMINI BOUNDED WRAPPER
# =============================================================================
def _call_gemini_bounded(prompt: str, config=None, timeout_s: float = 15.0):
    from vertexai.generative_models import GenerativeModel, GenerationConfig  # type: ignore[import]
    model = GenerativeModel("gemini-2.5-flash")

    def _invoke():
        if config:
            return model.generate_content(prompt, generation_config=config)
        return model.generate_content(
            prompt, generation_config=GenerationConfig(temperature=0.2)
        )

    with _cf.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_invoke)
        try:
            return future.result(timeout=timeout_s)
        except _cf.TimeoutError:
            future.cancel()
            raise TimeoutError(f"Gemini exceeded {timeout_s}s hard cap.")


# =============================================================================
# DPDP DATA ERASURE
# =============================================================================
def handle_purge(request):
    """DPDP-compliant tenant data erasure.

    Postmortem Fix #8: Previous implementation had .limit(100) on every
    collection query, silently leaving 65k+ leads undeleted while returning
    a 200 success. Now paginated to handle unlimited document counts.
    Also erases: global_lead_locks (by lock_entity), scraped_cache,
    predictive_cache subcollection, autonomous_dedup subcollection.
    BigQuery rows are NOT erased here (requires a scheduled BQ DELETE job —
    document in runbook as a separate manual step for BQ row erasure).
    """
    from flask import jsonify
    data      = request.json or {}
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return jsonify({"error": "Missing tenant_id"}), 400

    def _paginated_delete(collection_ref, field=None, value=None, batch_size=400):
        """Delete all documents matching an optional field filter in batches."""
        deleted = 0
        while True:
            if field:
                query = collection_ref.where(field_path=field, op_string="==", value=value).limit(batch_size)
            else:
                query = collection_ref.limit(batch_size)
            docs  = list(query.stream())
            if not docs:
                break
            for doc in docs:
                doc.reference.delete()
                deleted += 1
        return deleted

    total_erased = 0

    # 1. Campaigns
    total_erased += _paginated_delete(
        _db().collection("campaigns"), field="tenant_id", value=tenant_id
    )

    # 2. Leads — collect lock_entities for step 4, and scraped_cache keys for step 5
    lock_entities = []
    cache_keys    = []
    lead_query = _db().collection("leads").where(field_path="tenant_id", op_string="==", value=tenant_id)
    while True:
        batch = list(lead_query.limit(400).stream())
        if not batch:
            break
        for doc in batch:
            ld = doc.to_dict() or {}
            if ld.get("lock_entity"):
                lock_entities.append(ld["lock_entity"])
            url = ld.get("url") or ld.get("source_url", "")
            if url:
                cache_keys.append(url.replace("/", "_"))
            doc.reference.delete()
            total_erased += 1

    # 3. global_lead_locks for this tenant's domains
    for le in lock_entities:
        try:
            _db().collection("global_lead_locks").document(le).delete()
            total_erased += 1
        except Exception:
            pass

    # 4. scraped_cache entries
    for ck in cache_keys:
        try:
            _db().collection("scraped_cache").document(ck).delete()
            total_erased += 1
        except Exception:
            pass

    # 5. predictive_cache subcollection
    pred_col = _db().collection("users").document(tenant_id).collection("predictive_cache")
    total_erased += _paginated_delete(pred_col)

    # 6. autonomous_dedup subcollection
    dedup_col = _db().collection("autonomous_dedup")
    total_erased += _paginated_delete(
        dedup_col, field="tenant_id", value=tenant_id
    )

    # 7. wallet_shards
    shards_col = _db().collection("users").document(tenant_id).collection("wallet_shards")
    total_erased += _paginated_delete(shards_col)

    # 8. tenant user document itself
    try:
        _db().collection("users").document(tenant_id).delete()
        total_erased += 1
    except Exception:
        pass

    return jsonify({
        "message": f"Erased tenant {tenant_id} data.",
        "documents_deleted": total_erased,
        "note": "BigQuery rows require a separate scheduled DELETE job. See runbook.",
    }), 200
