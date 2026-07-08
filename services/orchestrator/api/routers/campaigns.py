"""
Orchestrator V23 — /api/campaigns Blueprint.

Routes migrated from the legacy trigger_daily_sweep catch-all:
  POST   /api/campaigns                  — create campaign + zero-wait enqueue
  PUT    /api/campaigns/<id>             — update campaign fields + BQ telemetry
  DELETE /api/campaigns/<id>             — soft/hard delete
  POST   /api/campaigns/<id>/ignite      — Day-1 dual-ignition (producer + consumer)
  POST   /api/campaigns/<id>/consume     — QA manual consume bypass
  POST   /api/campaigns/<id>/run         — Epsilon-greedy router dispatch
"""
from __future__ import annotations

import datetime
import json
import os
import random

from flask import Blueprint, jsonify, request
from google.cloud import tasks_v2
from google.cloud.firestore_v1.base_query import FieldFilter
from google.protobuf import timestamp_pb2

from core.clients import get_db, get_tasks_client  # type: ignore[import]
from core.config import PROJECT_ID, LOCATION, QUEUE, PIPELINE_URL  # type: ignore[import]
from core.auth import require_auth  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]
from core.helpers import (  # type: ignore[import]
    check_quota,
    get_service_account_email,
    get_vector_weights,
    classify_sourcing_vector,
    classify_intelligence_strategy,
    is_consumer_archetype,
    reserve_credits,
    release_reservation,
    _get_router_config,
    _pop_from_predictive_cache,
)

class _LazyDb:
    def __getattr__(self, name):
        return getattr(get_db(), name)

db = _LazyDb()

class _LazyTasks:
    def __getattr__(self, name):
        return getattr(get_tasks_client(), name)

tasks_client = _LazyTasks()

bp = Blueprint("campaigns", __name__)
log = get_logger("orchestrator.v23.campaigns")

MAX_CHILD_CAMPAIGNS = int(os.environ.get("MAX_CHILD_CAMPAIGNS", 5))

_CAMPAIGN_UPDATE_ALLOWED = {"name", "bio", "keywords", "status", "gl", "location", "persona_id", "drip_interval_minutes", "geo_hierarchy", "updatedAt", "sourcing_vector", "next_produce_due"}

# ---------------------------------------------------------------------------
# FIX (2026-06-21): Campaign field sanitizer — server-side write boundary.
# Scrubs system error strings, fallback sentinels, and non-geographic location
# values BEFORE they are persisted to Firestore. This is the last line of
# defense against upstream Prism UI bugs or API callers sending polluted data.
# ---------------------------------------------------------------------------
_FIELD_JUNK_PATTERNS: frozenset = frozenset({
    "fallback intent processing required",
    "internal server error",
    "traceback",
    "shadow_learner",
    "[shadow_learner",
    "placeholder bio",
    "test_keyword",
    "sample_data",
})
_LOCATION_JUNK_TOKENS: frozenset = frozenset({
    "interested", "customers", "vehicle", "users", "audience",
    "persona", "error", "exception", "fallback",
})


def _sanitize_campaign_fields(data: dict) -> dict:
    """Scrub campaign fields at the Firestore write boundary.

    Modifies *data* in-place and returns it for chaining convenience.
    Fields checked:
        - ``bio``: cleared if it contains known system error strings.
        - ``keywords``: individual keywords containing junk are dropped.
        - ``location``: cleared if it contains non-geographic tokens or is too long.

    Returns:
        The sanitized *data* dict.
    """
    # -- bio ---------------------------------------------------------------
    bio = data.get("bio", "")
    if bio and isinstance(bio, str) and bio != "CHILD_CAMPAIGN_OVERRIDE":
        if any(junk in bio.lower() for junk in _FIELD_JUNK_PATTERNS):
            log.warning("campaign_field_sanitized",
                        field="bio", original=bio[:120],
                        note="Bio contained system junk — cleared.")
            data["bio"] = ""

    # -- keywords ----------------------------------------------------------
    kw = data.get("keywords", "")
    if kw and isinstance(kw, str):
        parts = [k.strip() for k in kw.split(",") if k.strip()]
        clean = [
            k for k in parts
            if len(k) >= 2
            and not any(junk in k.lower() for junk in _FIELD_JUNK_PATTERNS)
        ]
        if len(clean) < len(parts):
            log.warning("campaign_field_sanitized",
                        field="keywords",
                        dropped=len(parts) - len(clean),
                        remaining=len(clean))
            data["keywords"] = ", ".join(clean)

    # -- location ----------------------------------------------------------
    loc = data.get("location", "")
    if loc and isinstance(loc, str):
        if (len(loc) > 100
                or any(tok in loc.lower() for tok in _LOCATION_JUNK_TOKENS)):
            log.warning("campaign_field_sanitized",
                        field="location", original=loc[:120],
                        note="Location contained non-geographic data — cleared.")
            data["location"] = ""

    return data

# ---------------------------------------------------------------------------
# GL / Geo mapping (canonical copy)
# ---------------------------------------------------------------------------
_GL_MAP: dict[str, str] = {
    "usa": "us", "united states": "us",
    "uk": "uk", "united kingdom": "uk", "england": "uk", "scotland": "uk",
    "canada": "ca", "australia": "au", "germany": "de",
    "singapore": "sg", "uae": "ae", "dubai": "ae", "abu dhabi": "ae",
    "india": "in",
    "kerala": "in", "karnataka": "in", "maharashtra": "in",
    "gujarat": "in", "rajasthan": "in", "tamil nadu": "in",
    "andhra pradesh": "in", "telangana": "in", "uttar pradesh": "in",
    "west bengal": "in", "punjab": "in", "haryana": "in",
    "madhya pradesh": "in", "bihar": "in", "odisha": "in",
    "assam": "in", "goa": "in", "jharkhand": "in",
    "mumbai": "in", "delhi": "in", "new delhi": "in",
    "bangalore": "in", "bengaluru": "in", "hyderabad": "in",
    "chennai": "in", "kolkata": "in", "pune": "in",
    "ahmedabad": "in", "jaipur": "in", "kochi": "in",
    "thiruvananthapuram": "in", "surat": "in", "lucknow": "in",
    "coimbatore": "in", "indore": "in", "bhopal": "in",
    "visakhapatnam": "in", "nagpur": "in", "chandigarh": "in",
}


def _resolve_gl(location_raw: str) -> str:
    loc = location_raw.strip().lower()
    gl = _GL_MAP.get(loc)
    if not gl:
        for key, val in _GL_MAP.items():
            if loc.startswith(key) or key in loc:
                gl = val
                break
    if gl:
        return gl
    if loc in ("worldwide", "global", ""):
        return ""
    return ""


def _oidc_task(url: str, payload: dict, sa_email: str, base_url: str) -> dict:
    t: dict = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        }
    }
    if sa_email:
        t["http_request"]["oidc_token"] = {
            "service_account_email": sa_email,
            "audience": base_url,
        }
    return t


# =============================================================================
# POST /api/campaigns
# =============================================================================
@bp.route("/api/campaigns", methods=["POST"])
@require_auth
def create_campaign(uid, tenant_id, user_role):
    """Create a new campaign, denormalise persona, zero-wait enqueue."""
    from google.cloud import firestore  # local import for SERVER_TIMESTAMP

    is_valid, status_code, err_msg = check_quota(tenant_id)
    if not is_valid:
        return jsonify({"error": err_msg}), status_code

    data = request.json or {}
    data.pop("tenant_id", None)

    name = (data.get("name") or "").strip()
    if not name or len(name) > 200:
        return jsonify({"error": "Campaign name is required (max 200 chars)"}), 400
    data["name"] = name
    bio = (data.get("bio") or "").strip()
    if len(bio) > 5000:
        return jsonify({"error": "Bio exceeds maximum length of 5000 characters"}), 400
    data["bio"] = bio
    keywords_raw = data.get("keywords", "")
    if isinstance(keywords_raw, str) and len(keywords_raw) > 2000:
        return jsonify({"error": "Keywords exceed maximum length of 2000 characters"}), 400

    # GL derivation
    loc_raw = (data.get("location") or "").strip()
    data["gl"] = _resolve_gl(loc_raw)

    # Active campaign cap
    # J-12 FIX: Use server-side .count().get() instead of list(...).stream().
    # The old approach did a full collection scan across ALL campaigns for this
    # tenant on every campaign creation, causing slow POSTs for tenants with
    # many campaigns. .count() performs a server-side aggregate with no doc reads.
    active_count = (
        db.collection("campaigns")
          .where(filter=FieldFilter("tenant_id", "==", tenant_id))
          .where(filter=FieldFilter("status", "==", "active"))
          .count()
          .get()[0][0].value
    )
    if active_count >= MAX_CHILD_CAMPAIGNS:
        return jsonify({
            "error": f"Maximum of {MAX_CHILD_CAMPAIGNS} active campaigns allowed per tenant."
        }), 403

    # RLHF human-edited telemetry
    if data.get("human_edited"):
        product_name     = data.get("name", "").strip()
        orig_hook        = data.pop("orig_hook", "")
        orig_adv         = data.pop("orig_adv", "")
        target_angle_hook = data.pop("target_angle_hook", orig_hook)
        target_angle_adv  = data.pop("target_angle_adv", orig_adv)
        data.pop("human_edited", None)
        if product_name:
            doc_id = "".join(c for c in product_name.lower() if c.isalnum() or c in "-_")[:100]
            if doc_id:
                try:
                    db.collection("market_trend_cache").document(doc_id).set({
                        "market_trend_hook": target_angle_hook,
                        "unfair_advantage":  target_angle_adv,
                        "updatedAt":         firestore.SERVER_TIMESTAMP,
                        "rlhf_source_tenant": tenant_id,
                    }, merge=True)
                except Exception as e:
                    log.warning("rlhf_telemetry_failed", error=str(e))

    data["tenant_id"] = tenant_id
    data["createdAt"] = firestore.SERVER_TIMESTAMP
    data["updatedAt"] = firestore.SERVER_TIMESTAMP

    # Persona Vault: denormalise linked persona
    # J-9 FIX: Frontend now sends inline persona_bio and persona_keywords in the
    # payload for child campaigns to guard against Firestore eventual consistency
    # races. We use Firestore as primary source and fall back to inline fields
    # if the persona document is not yet replicated at read time.
    persona_id_val = (data.get("persona_id") or "").strip()
    # Stash inline fallback values before they may be overwritten by Firestore read
    _inline_persona_bio  = (data.pop("persona_bio",      None) or "").strip()
    _inline_persona_keys = (data.pop("persona_keywords", None) or "").strip()
    if persona_id_val:
        try:
            p_snap = (
                db.collection("tenant_profiles")
                  .document(tenant_id)
                  .collection("personas")
                  .document(persona_id_val)
                  .get()
            )
            if p_snap.exists:
                p_data = p_snap.to_dict() or {}
                data["persona_bio"]               = p_data.get("bio", "") or _inline_persona_bio
                data["persona_keywords"]          = p_data.get("keywords", "") or _inline_persona_keys
                data["persona_name"]              = p_data.get("name", "")
                data["persona_targeting_signals"] = p_data.get("targeting_signals", [])
                if not data.get("bio"):
                    data["bio"] = data["persona_bio"]
                log.info("persona_linked", persona=p_data.get("name"),
                         negative_signals=len(data["persona_targeting_signals"]))
            elif _inline_persona_bio:
                # J-9 FIX: Persona doc not yet visible (eventual consistency race).
                # Use the inline fields the frontend pre-attached from its local persona state.
                data["persona_bio"]      = _inline_persona_bio
                data["persona_keywords"] = _inline_persona_keys
                if not data.get("bio"):
                    data["bio"] = _inline_persona_bio
                log.warning("persona_denormalise_eventual_consistency_fallback",
                            persona_id=persona_id_val, used_inline=True)
        except Exception as p_err:
            log.warning("persona_denormalise_error", error=str(p_err))
            # Non-fatal: apply inline fallback if available
            if _inline_persona_bio:
                data["persona_bio"]      = _inline_persona_bio
                data["persona_keywords"] = _inline_persona_keys

    try:
        drip_interval_mins = int(data.get("drip_interval_minutes") or 240)
        if drip_interval_mins <= 0:
            drip_interval_mins = 240
    except Exception:
        drip_interval_mins = 240
    data["drip_interval_minutes"] = drip_interval_mins

    # Server-side field sanitization — last line of defense
    _sanitize_campaign_fields(data)

    try:
        _, doc_ref = db.collection("campaigns").add(data)
    except Exception as e:
        log.error("firestore_campaign_create_error", error=str(e), tenant_id=tenant_id)
        return jsonify({"error": "Failed to create campaign"}), 500

    # Synaptic Router classification (V23 archetype-based)
    bio = data.get("bio", "")
    weights = get_vector_weights()
    if bio and bio != "CHILD_CAMPAIGN_OVERRIDE":
        vector  = classify_sourcing_vector(bio, weights)
        doc_ref.update({"sourcing_vector": vector})
        log.info("sourcing_vector_classified", campaign_id=doc_ref.id, vector=vector)
    elif bio == "CHILD_CAMPAIGN_OVERRIDE":
        effective_bio = " | ".join(filter(None, [
            data.get("campaign_focus", ""),
            data.get("pain_point", ""),
            data.get("unfair_advantage", ""),
        ]))
        # FIX (2026-06-21): Classify effective_bio through Gemini instead of
        # hardcoding "Classic B2B". The old code forced ALL child campaigns
        # into B2B regardless of actual industry (Real Estate, Dental, etc.).
        vector = classify_sourcing_vector(effective_bio, weights) if effective_bio else "B2B"
        doc_ref.update({"sourcing_vector": vector, "effective_bio": effective_bio})
        log.info("sourcing_vector_classified_child", campaign_id=doc_ref.id, vector=vector)

    # ── V26.0: Intelligence Strategy Classification ──────────────────────────
    # Determines HOW to find leads (PLATFORM_MINING, COLLOQUIAL_DISCOVERY, etc.)
    # Auto-derives competitor platforms, vocabulary notes, event types from ICP.
    _strategy_bio = data.get("effective_bio") or data.get("bio") or ""
    _heuristic_profile = infer_campaign_intelligence_profile(
        effective_bio=data.get("effective_bio") or data.get("bio") or "",
        keywords=data.get("keywords", ""),
        location=data.get("location", ""),
        campaign_name=data.get("name", ""),
        pain_point=data.get("pain_point", ""),
        sourcing_vector=data.get("sourcing_vector", vector if 'vector' in dir() else ""),
    )
    if _strategy_bio and _strategy_bio != "CHILD_CAMPAIGN_OVERRIDE":
        _strategy_bio_final = _strategy_bio
    elif _strategy_bio == "CHILD_CAMPAIGN_OVERRIDE":
        _strategy_bio_final = " | ".join(filter(None, [
            data.get("campaign_focus", ""),
            data.get("pain_point", ""),
            data.get("unfair_advantage", ""),
        ]))
    else:
        _strategy_bio_final = ""

    if _strategy_bio_final:
        try:
            _intel_strategy = classify_intelligence_strategy(
                effective_bio=_strategy_bio_final,
                keywords=data.get("keywords", ""),
                campaign_focus=data.get("campaign_focus", ""),
                pain_point=data.get("pain_point", ""),
                sourcing_vector=data.get("sourcing_vector", vector if 'vector' in dir() else "B2B"),
                location=data.get("location", ""),
            )
            _intel_strategy.setdefault("platform_targets", _heuristic_profile.get("platform_targets", []))
            _intel_strategy.setdefault("competitor_names", _heuristic_profile.get("competitor_names", []))
            _intel_strategy.setdefault("event_types", _heuristic_profile.get("event_types", []))
            _intel_strategy.setdefault("vocabulary_notes", _heuristic_profile.get("vocabulary_notes", ""))
            _intel_strategy.setdefault("decision_maker_titles", _heuristic_profile.get("decision_maker_titles", []))
            _intel_strategy.setdefault("inferred_from", _heuristic_profile.get("inferred_from", "gemini"))
            if not _intel_strategy.get("primary"):
                _intel_strategy["primary"] = _heuristic_profile.get("primary_strategy", "COLLOQUIAL_DISCOVERY")
            if not _intel_strategy.get("secondary"):
                _intel_strategy["secondary"] = _heuristic_profile.get("secondary_strategy", "NONE")
            doc_ref.update({"intelligence_strategy": _intel_strategy})
            log.info(
                "intelligence_strategy_classified",
                campaign_id=doc_ref.id,
                primary=_intel_strategy.get("primary"),
                secondary=_intel_strategy.get("secondary"),
                platform_targets=len(_intel_strategy.get("platform_targets", [])),
                competitor_names=len(_intel_strategy.get("competitor_names", [])),
            )
        except Exception as _strat_exc:
            log.warning("intelligence_strategy_classification_failed",
                        campaign_id=doc_ref.id, error=str(_strat_exc))

    # Zero-wait timestamps
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    doc_ref.update({
        "unprocessed_queue": [],
        "next_produce_due":  (now_utc + datetime.timedelta(hours=24)).isoformat(),
        "next_drip_due":     (now_utc + datetime.timedelta(minutes=drip_interval_mins)).isoformat(),
    })

    # Zero-wait direct enqueue
    enqueue_error = None
    try:
        base_url    = PIPELINE_URL.split("/dispatch")[0]
        produce_url = f"{base_url}/produce"
        queue_path  = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
        sa_email    = get_service_account_email().strip()
        jitter      = random.randint(1, 5)
        sched_ts    = timestamp_pb2.Timestamp()
        sched_ts.FromDatetime(now_utc + datetime.timedelta(seconds=jitter))
        task = _oidc_task(produce_url, {"tenant_id": tenant_id, "campaign_id": doc_ref.id}, sa_email, base_url)
        task["schedule_time"] = sched_ts
        tasks_client.create_task(request={"parent": queue_path, "task": task})
        log.info("zero_wait_enqueued", campaign_id=doc_ref.id, jitter=jitter)

        # UX-01: Also enqueue a consumer/dispatcher Cloud Task with 180s delay
        # so newly created campaigns don't wait for the next sweep cycle.
        try:
            consumer_delay = 180
            consumer_ts = timestamp_pb2.Timestamp()
            consumer_ts.FromDatetime(now_utc + datetime.timedelta(seconds=consumer_delay))
            consumer_task = _oidc_task(
                f"{base_url}/dispatch",
                {"tenant_id": tenant_id, "campaign_id": doc_ref.id},
                sa_email, base_url,
            )
            consumer_task["schedule_time"] = consumer_ts
            tasks_client.create_task(request={"parent": queue_path, "task": consumer_task})
            log.info("zero_wait_consumer_enqueued", campaign_id=doc_ref.id, delay_s=consumer_delay)
        except Exception as consumer_err:
            log.warning("zero_wait_consumer_enqueue_failed", error=str(consumer_err))

    except Exception as enq_err:
        enqueue_error = str(enq_err)
        log.warning("zero_wait_enqueue_failed", error=enqueue_error)

    # V25.3.1: Bio quality warning — lazy users get feedback instead of
    # silently producing 0 leads for weeks.
    _warnings = []
    _bio_len = len((data.get("bio") or "").strip())
    if _bio_len == 0:
        _warnings.append({
            "code": "BIO_EMPTY",
            "message": "Campaign has no bio. Lead quality will be very low. "
                       "Add a 2-3 sentence description of your product/service and target audience.",
            "severity": "high",
        })
    elif _bio_len < 50:
        _warnings.append({
            "code": "BIO_TOO_SHORT",
            "message": "Campaign bio is very short. Lead matching works best with "
                       "50+ characters describing your product and target pain points.",
            "severity": "medium",
        })

    if not (data.get("keywords") or "").strip():
        _warnings.append({
            "code": "NO_KEYWORDS",
            "message": "No keywords provided. The system will auto-generate search terms "
                       "from your bio, which may be less precise.",
            "severity": "low",
        })

    response_data = {
        "status": "success",
        "id": doc_ref.id,
        "zero_wait_enqueued": enqueue_error is None,
        "enqueue_error": enqueue_error,
    }
    if _warnings:
        response_data["warnings"] = _warnings
    return jsonify(response_data), 201


# =============================================================================
# PUT /api/campaigns/<id>
# =============================================================================
@bp.route("/api/campaigns/<string:doc_id>", methods=["PUT"])
@require_auth
def update_campaign(uid, tenant_id, user_role, doc_id):
    """Update campaign fields; re-classify sourcing vector if bio changes."""
    from google.cloud import firestore
    from core.helpers import _enqueue_bq_telemetry_task  # type: ignore[import]

    doc_ref  = db.collection("campaigns").document(doc_id)
    doc_data = doc_ref.get()

    if not doc_data.exists or doc_data.to_dict().get("tenant_id") != tenant_id:
        return jsonify({"error": "Forbidden"}), 403

    data = request.json or {}
    data.pop("tenant_id", None)
    data["updatedAt"] = firestore.SERVER_TIMESTAMP

    # UX-03: If keywords or bio changed, set next_produce_due to now for re-produce
    existing_data = doc_data.to_dict() or {}
    _keywords_changed = "keywords" in data and data.get("keywords") != existing_data.get("keywords")
    _bio_changed = "bio" in data and data.get("bio") != existing_data.get("bio")

    if "bio" in data and data["bio"]:
        weights = get_vector_weights()
        data["sourcing_vector"] = classify_sourcing_vector(data["bio"], weights)
        if data["bio"] == "CHILD_CAMPAIGN_OVERRIDE":
            campaign = doc_data.to_dict()
            data["bio"] = (
                campaign.get("effective_bio", "") or
                campaign.get("campaign_focus", "") or
                ", ".join(campaign.get("keywords", []))
            )

    # Server-side field sanitization — last line of defense
    _sanitize_campaign_fields(data)

    # V26.0.2: Auto-reclassify intelligence strategy on meaningful edits.
    # The user never picks a strategy — the AI always decides. Triggers when:
    # 1. Bio or keywords changed (ICP may have shifted)
    # 2. Legacy campaign has no strategy yet (upgrade path)
    # Any stale strategy_override from old frontend versions is silently discarded.
    data.pop("strategy_override", None)  # Strip client-side override — backend is authoritative

    _needs_reclassify = (
        _keywords_changed or _bio_changed
        or not existing_data.get("intelligence_strategy", {}).get("primary")
    )
    if _needs_reclassify:
        _reclass_bio = (
            data.get("bio") or existing_data.get("effective_bio")
            or existing_data.get("bio") or ""
        )
        _reclass_kw = data.get("keywords") or existing_data.get("keywords", "")
        _reclass_focus = existing_data.get("campaign_focus", "")
        _reclass_pain = existing_data.get("pain_point", "")
        _reclass_sv = data.get("sourcing_vector") or existing_data.get("sourcing_vector", "B2B")
        _reclass_loc = data.get("location") or existing_data.get("location", "")

        if _reclass_bio or _reclass_kw:
            try:
                _new_strategy = classify_intelligence_strategy(
                    effective_bio=_reclass_bio,
                    keywords=_reclass_kw,
                    campaign_focus=_reclass_focus,
                    pain_point=_reclass_pain,
                    sourcing_vector=_reclass_sv,
                    location=_reclass_loc,
                )
                doc_ref.update({"intelligence_strategy": _new_strategy})
                log.info("intelligence_strategy_auto_reclassified",
                         campaign_id=doc_id,
                         primary=_new_strategy.get("primary"),
                         trigger="bio_keywords_changed" if (_keywords_changed or _bio_changed) else "legacy_upgrade")
            except Exception as _strat_err:
                log.warning("intelligence_strategy_reclassify_failed",
                            campaign_id=doc_id, error=str(_strat_err))

    data = {k: v for k, v in data.items() if k in _CAMPAIGN_UPDATE_ALLOWED}

    # UX-02: Resume from paused → active: re-ignite by setting next_produce_due to now
    #        and dispatching a fresh /produce Cloud Task.
    old_status = existing_data.get("status")
    new_status = data.get("status")
    if old_status == "paused" and new_status == "active":
        _now_utc = datetime.datetime.now(datetime.timezone.utc)
        data["next_produce_due"] = _now_utc.isoformat()
        try:
            from core.config import PIPELINE_URL as _pl_url  # type: ignore[import]
            from core.helpers import get_service_account_email as _get_sa  # type: ignore[import]
            from core.clients import get_tasks_client as _get_tc  # type: ignore[import]
            from core.config import PROJECT_ID as _pid, LOCATION as _loc, QUEUE as _q  # type: ignore[import]
            from google.cloud import tasks_v2 as _tv2
            from google.protobuf import timestamp_pb2 as _ts_pb2
            import json as _json
            _base = _pl_url.split("/dispatch")[0]
            _sa = _get_sa().strip()
            _tc = _get_tc()
            _qp = _tc.queue_path(_pid, _loc, _q)
            _ts = _ts_pb2.Timestamp()
            _ts.FromDatetime(_now_utc + datetime.timedelta(seconds=random.randint(1, 5)))
            _task = {
                "schedule_time": _ts,
                "http_request": {
                    "http_method": _tv2.HttpMethod.POST,
                    "url": f"{_base}/produce",
                    "headers": {"Content-Type": "application/json"},
                    "body": _json.dumps({"tenant_id": tenant_id, "campaign_id": doc_id}).encode(),
                    "oidc_token": {"service_account_email": _sa, "audience": _base},
                },
            }
            _tc.create_task(request={"parent": _qp, "task": _task})
            log.info("campaign_resume_reignite", campaign_id=doc_id)
        except Exception as _re_err:
            log.warning("campaign_resume_reignite_failed", campaign_id=doc_id, error=str(_re_err))

    # UX-03: If keywords or bio changed, force re-produce
    if _keywords_changed or _bio_changed:
        _now_utc_ux03 = datetime.datetime.now(datetime.timezone.utc)
        data["next_produce_due"] = _now_utc_ux03.isoformat()
        log.info("campaign_reproduce_scheduled",
                 campaign_id=doc_id,
                 keywords_changed=_keywords_changed,
                 bio_changed=_bio_changed)

    doc_ref.update(data)
    _enqueue_bq_telemetry_task(tenant_id, existing_data, data.get("status") or "updated")
    return jsonify({"status": "success"}), 200


# =============================================================================
# DELETE /api/campaigns/<id>
# =============================================================================
@bp.route("/api/campaigns/<string:doc_id>", methods=["DELETE"])
@require_auth
def delete_campaign(uid, tenant_id, user_role, doc_id):
    """Delete a campaign owned by this tenant."""
    doc_ref  = db.collection("campaigns").document(doc_id)
    doc_data = doc_ref.get()
    if not doc_data.exists or doc_data.to_dict().get("tenant_id") != tenant_id:
        return jsonify({"error": "Forbidden"}), 403
    orphan_leads = get_db().collection("leads").where("campaign_id", "==", doc_id).where("tenant_id", "==", tenant_id).limit(1).get()
    if orphan_leads:
        return jsonify({"error": "Cannot delete campaign with existing leads. Pause it instead."}), 409
    doc_ref.delete()
    log.info("campaign_deleted", campaign_id=doc_id, tenant_id=tenant_id)
    return jsonify({"status": "success"}), 200


# =============================================================================
# POST /api/campaigns/<id>/ignite
# =============================================================================
@bp.route("/api/campaigns/<string:campaign_id>/ignite", methods=["POST"])
@require_auth
def ignite_campaign(uid, tenant_id, user_role, campaign_id):
    """Day-1 dual-ignition: enqueue producer + consumer in tandem."""
    camp_ref = db.collection("campaigns").document(campaign_id)
    camp_doc = camp_ref.get()

    if not camp_doc.exists or camp_doc.to_dict().get("tenant_id") != tenant_id:
        return jsonify({"error": "Forbidden"}), 403

    role = (db.collection("users").document(tenant_id).get().to_dict() or {}).get("role")
    if role != "super_admin":
        is_valid, status_code, err_msg = check_quota(tenant_id)
        if not is_valid:
            return jsonify({"error": err_msg, "ignite": False}), status_code

    try:
        sa_email    = get_service_account_email().strip()
        base_url    = PIPELINE_URL.split("/dispatch")[0]
        produce_url = f"{base_url}/produce"
        consume_url = f"{base_url}/dispatch"
        queue_path  = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
        now         = datetime.datetime.now(datetime.timezone.utc)

        def _timed_task(url, delay_s):
            ts = timestamp_pb2.Timestamp()
            ts.FromDatetime(now + datetime.timedelta(seconds=delay_s))
            t = _oidc_task(url, {"tenant_id": tenant_id, "campaign_id": campaign_id}, sa_email, base_url)
            t["schedule_time"] = ts
            return t

        prod_jitter = random.randint(3, 5)
        tasks_client.create_task(request={"parent": queue_path, "task": _timed_task(produce_url, prod_jitter)})

        CONSUMER_DELAY = 180
        tasks_client.create_task(request={"parent": queue_path, "task": _timed_task(consume_url, CONSUMER_DELAY)})
        camp_ref.update({"next_drip_due": now + datetime.timedelta(seconds=CONSUMER_DELAY - 10)})

        log.info("campaign_ignited", campaign_id=campaign_id, prod_jitter=prod_jitter)
        return jsonify({
            "status":              "dual_ignited",
            "campaign_id":         campaign_id,
            "producer_fires_in_s": prod_jitter,
            "consumer_fires_in_s": CONSUMER_DELAY,
            "ignite":              True,
        }), 200

    except Exception as e:
        log.error("ignite_failed", campaign_id=campaign_id, error=str(e))
        return jsonify({"error": str(e), "ignite": False}), 500


# =============================================================================
# POST /api/campaigns/<id>/consume
# =============================================================================
@bp.route("/api/campaigns/<string:campaign_id>/consume", methods=["POST"])
@require_auth
def consume_campaign(uid, tenant_id, user_role, campaign_id):
    """QA manual dispatch bypass — skip next_drip_due lock."""
    camp_ref = db.collection("campaigns").document(campaign_id)
    camp_doc = camp_ref.get()

    if not camp_doc.exists or camp_doc.to_dict().get("tenant_id") != tenant_id:
        return jsonify({"error": "Forbidden"}), 403

    camp_data = camp_doc.to_dict() or {}
    queue_depth = len(camp_data.get("unprocessed_queue", []))
    if queue_depth == 0:
        return jsonify({"status": "noop", "reason": "unprocessed_queue empty — run /ignite first", "queue_depth": 0}), 200

    try:
        drip_interval_mins = int(camp_data.get("drip_interval_minutes") or 240)
        if drip_interval_mins <= 0:
            drip_interval_mins = 240
    except Exception:
        drip_interval_mins = 240

    try:
        sa_email   = get_service_account_email().strip()
        base_url   = PIPELINE_URL.split("/dispatch")[0]
        queue_path = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
        now        = datetime.datetime.now(datetime.timezone.utc)
        ts         = timestamp_pb2.Timestamp()
        ts.FromDatetime(now + datetime.timedelta(seconds=2))
        t = _oidc_task(f"{base_url}/dispatch", {"tenant_id": tenant_id, "campaign_id": campaign_id}, sa_email, base_url)
        t["schedule_time"] = ts
        tasks_client.create_task(request={"parent": queue_path, "task": t})
        camp_ref.update({"next_drip_due": (now + datetime.timedelta(minutes=drip_interval_mins)).isoformat()})
        return jsonify({"status": "consume_enqueued", "campaign_id": campaign_id, "queue_depth": queue_depth, "fires_in_s": 2}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# POST /api/campaigns/<id>/run  (Epsilon-greedy router)
# =============================================================================
@bp.route("/api/campaigns/<string:campaign_id>/run", methods=["POST"])
@require_auth
def run_campaign(uid, tenant_id, user_role, campaign_id):
    """Epsilon-greedy router: exploit predictive cache + explore via Cartographer."""
    camp_ref = db.collection("campaigns").document(campaign_id)
    camp_doc = camp_ref.get()

    if not camp_doc.exists or camp_doc.to_dict().get("tenant_id") != tenant_id:
        return jsonify({"error": "Forbidden"}), 403

    camp_data         = camp_doc.to_dict()
    data              = request.json or {}
    batch_size        = int(camp_data.get("lead_target", data.get("batch_size", 10)))
    role = (db.collection("users").document(tenant_id).get().to_dict() or {}).get("role")

    router_cfg        = _get_router_config(db)
    exploit_ratio     = float(router_cfg.get("exploit_ratio", 0.10))
    autonomous_target = max(0, round(batch_size * exploit_ratio))
    cartographer_target = batch_size - autonomous_target
    cartographer_cost   = cartographer_target
    audit_trail: list[str] = []

    # Reserve credits for cartographer path
    if role != "super_admin" and cartographer_cost > 0:
        if not reserve_credits(tenant_id, cartographer_cost):
            return jsonify({"error": "Insufficient credits.", "code": "insufficient_credits"}), 402
        audit_trail.append(f"Reserved {cartographer_cost} credits atomically.")

    # Exploit: CAS pop from predictive_cache
    promoted: list = []
    if autonomous_target > 0:
        promoted = _pop_from_predictive_cache(tenant_id, db, autonomous_target)
        deficit  = autonomous_target - len(promoted)
        if deficit > 0:
            deficit_reserve_ok = (role == "super_admin") or reserve_credits(tenant_id, deficit)
            if deficit_reserve_ok:
                cartographer_target += deficit
                cartographer_cost   += deficit
                audit_trail.append(f"Cache deficit={deficit}: reallocated to Cartographer.")
            else:
                # P1-FIN-4: Deficit reservation failed — refund the original
                # cartographer_cost to prevent permanently leaked credits.
                if role != "super_admin" and cartographer_cost > 0:
                    release_reservation(tenant_id, cartographer_cost)
                audit_trail.append(f"Cache deficit={deficit}: reserve failed, {cartographer_cost} credits refunded.")
                return jsonify({
                    "error": "Insufficient credits for cache deficit reallocation.",
                    "code": "insufficient_credits",
                    "audit_trail": audit_trail,
                }), 402

    # Explore: enqueue Cartographer Cloud Task
    produce_dispatched = 0
    if cartographer_target > 0:
        try:
            sa_email   = get_service_account_email().strip()
            base_url   = PIPELINE_URL.split("/dispatch")[0]
            queue_path = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)
            jitter     = random.randint(1, 30)
            sched_t    = timestamp_pb2.Timestamp()
            sched_t.FromDatetime(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=jitter))
            task_body  = {
                "tenant_id": tenant_id, "campaign_id": campaign_id,
                "lead_target": cartographer_target, "reserved_credit_cost": cartographer_cost,
            }
            t = _oidc_task(f"{base_url}/produce", task_body, sa_email, base_url)
            t["schedule_time"] = sched_t
            tasks_client.create_task(request={"parent": queue_path, "task": t})
            produce_dispatched = 1
            audit_trail.append(f"Cartographer queued for {cartographer_target} leads (jitter={jitter}s).")
        except Exception as task_err:
            if role != "super_admin":
                release_reservation(tenant_id, cartographer_cost)
            audit_trail.append(f"Cartographer enqueue failed, {cartographer_cost} credits refunded: {task_err}")

    return jsonify({
        "status":              "router_dispatched",
        "batch_size":          batch_size,
        "exploit_ratio":       exploit_ratio,
        "autonomous_promoted": len(promoted),
        "cartographer_queued": cartographer_target,
        "producer_dispatched": produce_dispatched,
        "reserved_credits":    cartographer_cost,
        "audit_trail":         audit_trail,
    }), 200
