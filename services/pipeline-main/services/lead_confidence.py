"""Deterministic lead confidence scoring for MVP qualification.

The goal is to replace brittle score thresholds with an explainable confidence
score that combines evidence strength, intent, urgency, geography, and source
trust. The score is designed to be lightweight and deterministic so the app
can promote leads without depending entirely on the LLM.

V26.5.1: Adapter + hybrid promotion
  ``final_score_and_dm()`` (Serper dispatch path) and ``inline_score_signal()``
  (harvest path) emit different evaluation schemas. ``adapt_gemini_evaluation_for_confidence``
  normalizes both into the harvest-style fields expected by
  ``calculate_lead_confidence``. Hybrid promotion can still promote high Gemini
  scores when the confidence heuristic is conservative.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

# ---------------------------------------------------------------------------
# Hybrid promotion policy (configurable via env or constants)
# ---------------------------------------------------------------------------
# Promote when confidence_bundle.promotion is True OR when Gemini score clears
# a policy-aware floor and basic evidence exists.
HYBRID_PROMOTION_ENABLED: bool = os.environ.get(
    "HYBRID_PROMOTION_ENABLED", "true"
).lower() in ("1", "true", "yes")

# Gemini score floors (1–10 scale from final_score_and_dm).
HYBRID_SCORE_FLOOR_BALANCED: int = int(os.environ.get("HYBRID_SCORE_FLOOR_BALANCED", "8"))
HYBRID_SCORE_FLOOR_RECOVERY: int = int(os.environ.get("HYBRID_SCORE_FLOOR_RECOVERY", "7"))
HYBRID_SCORE_FLOOR_STRICT: int = int(os.environ.get("HYBRID_SCORE_FLOOR_STRICT", "9"))
# Thin / snippet-only payloads need a higher Gemini score for hybrid promote.
HYBRID_THIN_PAYLOAD_EXTRA: int = int(os.environ.get("HYBRID_THIN_PAYLOAD_EXTRA", "1"))

_UNKNOWN_STRINGS = frozenset({"", "unknown", "none", "n/a", "null", "undefined"})
_HARVEST_TIER_FIELDS = ("tier", "topic_coherence", "pain_summary", "contact_point")


def _coerce_int_score(raw: Any, default: int = 0) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(0, min(10, value))


def _coerce_float(raw: Any, default: float = 0.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return value


def _is_meaningful_text(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.lower() not in _UNKNOWN_STRINGS


def _first_contact_uri(evaluation: Mapping[str, Any]) -> str:
    """Extract first usable contact URI from harvest or final_score shapes."""
    direct = str(evaluation.get("contact_point") or "").strip()
    if _is_meaningful_text(direct):
        return direct

    endpoints = evaluation.get("contact_endpoints") or []
    if isinstance(endpoints, list):
        for item in endpoints:
            if not isinstance(item, Mapping):
                continue
            uri = str(item.get("uri") or item.get("value") or "").strip()
            if _is_meaningful_text(uri):
                return uri
    return ""


def harvest_fields_present(evaluation: Optional[Mapping[str, Any]]) -> bool:
    """True when evaluation already carries harvest-style confidence fields."""
    if not isinstance(evaluation, Mapping) or not evaluation:
        return False
    tier = str(evaluation.get("tier") or "").strip().upper()
    if tier in {"HIGH", "MEDIUM", "LOW"}:
        return True
    # Partial harvest payload (inline_score_signal always sets these keys).
    if "topic_coherence" in evaluation and evaluation.get("topic_coherence") is not None:
        return True
    if _is_meaningful_text(evaluation.get("pain_summary")):
        return True
    return False


def _tier_from_score_and_confidence_level(score: int, confidence_level: str) -> str:
    """Map Gemini 1–10 score + confidence_level → HIGH/MEDIUM/LOW."""
    level = (confidence_level or "").strip().upper()
    if level == "HIGH" or score >= 7:
        return "HIGH"
    if level == "MEDIUM" or score >= 4:
        return "MEDIUM"
    if level in {"SPECULATIVE", "LOW"} or score > 0:
        return "LOW"
    return "LOW"


def _coherence_from_score(score: int, confidence_level: str) -> float:
    """Derive topic_coherence ∈ [0, 1] from Gemini score and confidence_level."""
    base = max(0.0, min(1.0, score / 10.0))
    level = (confidence_level or "").strip().upper()
    if level == "HIGH":
        base = max(base, 0.75)
    elif level == "MEDIUM":
        base = max(base, 0.55)
    elif level == "SPECULATIVE":
        base = min(base, 0.45)
    return round(base, 3)


def adapt_gemini_evaluation_for_confidence(
    evaluation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalize Gemini evaluation into the schema expected by calculate_lead_confidence.

    Primary source (Serper dispatch path — ``final_score_and_dm``):
      score, confidence_level, pain_point, contact_endpoints, company_name

    Secondary / harvest source (``inline_score_signal``):
      tier, topic_coherence, pain_summary, contact_point, geo_match

    Harvest-style fields win when already present and meaningful so the harvest
    path is never degraded. Missing fields are filled from Gemini score signals.

    Returns:
        Adapted evaluation dict plus metadata keys:
          ``_adapter_used`` (bool), ``_harvest_fields_present`` (bool),
          ``_adapter_source`` (str).
    """
    raw = dict(evaluation or {}) if isinstance(evaluation, Mapping) else {}
    harvest_present = harvest_fields_present(raw)
    score = _coerce_int_score(raw.get("score"), 0)
    confidence_level = str(raw.get("confidence_level") or "").strip().upper()

    adapted: Dict[str, Any] = dict(raw)

    # --- tier ---------------------------------------------------------------
    existing_tier = str(raw.get("tier") or "").strip().upper()
    if existing_tier in {"HIGH", "MEDIUM", "LOW"}:
        adapted["tier"] = existing_tier
    else:
        adapted["tier"] = _tier_from_score_and_confidence_level(score, confidence_level)

    # If Gemini confidence_level is stronger than a weak existing tier, lift it.
    # Never downgrade an explicit HIGH harvest tier.
    mapped_tier = _tier_from_score_and_confidence_level(score, confidence_level)
    _tier_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    if _tier_rank.get(mapped_tier, 0) > _tier_rank.get(adapted["tier"], 0):
        # Only lift from score/confidence_level when score is present.
        if score > 0 or confidence_level in {"HIGH", "MEDIUM"}:
            adapted["tier"] = mapped_tier

    # --- topic_coherence ----------------------------------------------------
    coherence_raw = raw.get("topic_coherence")
    if coherence_raw is not None and str(coherence_raw).strip() != "":
        adapted["topic_coherence"] = max(0.0, min(1.0, _coerce_float(coherence_raw, 0.0)))
    else:
        adapted["topic_coherence"] = _coherence_from_score(score, confidence_level)

    # --- pain_summary -------------------------------------------------------
    if _is_meaningful_text(raw.get("pain_summary")):
        adapted["pain_summary"] = str(raw.get("pain_summary")).strip()
    elif _is_meaningful_text(raw.get("pain_point")):
        adapted["pain_summary"] = str(raw.get("pain_point")).strip()
    else:
        adapted["pain_summary"] = str(raw.get("pain_summary") or "")

    # --- contact_point ------------------------------------------------------
    contact = _first_contact_uri(raw)
    adapted["contact_point"] = contact

    # --- geo_match ----------------------------------------------------------
    # Preserve explicit harvest/dispatch values. When absent, treat a scored
    # Gemini match as geo-OK (final_score_and_dm already applies GEO rules).
    if "geo_match" in raw and raw.get("geo_match") is not None:
        adapted["geo_match"] = bool(raw.get("geo_match"))
    else:
        adapted["geo_match"] = score >= 4

    # Preserve numeric score for hybrid floor checks / observability.
    adapted["score"] = score
    if confidence_level:
        adapted["confidence_level"] = confidence_level

    adapter_filled = not harvest_present or any(
        (
            existing_tier not in {"HIGH", "MEDIUM", "LOW"} and score > 0,
            coherence_raw is None or str(coherence_raw).strip() == "",
            not _is_meaningful_text(raw.get("pain_summary"))
            and _is_meaningful_text(raw.get("pain_point")),
            not _is_meaningful_text(raw.get("contact_point")) and bool(contact),
            "geo_match" not in raw or raw.get("geo_match") is None,
        )
    )

    adapted["_adapter_used"] = bool(adapter_filled)
    adapted["_harvest_fields_present"] = harvest_present
    adapted["_adapter_source"] = (
        "harvest" if harvest_present and not adapter_filled
        else "hybrid" if harvest_present and adapter_filled
        else "gemini_score" if score > 0 or confidence_level
        else "empty"
    )
    return adapted


def resolve_hybrid_score_floor(
    policy_mode: str = "balanced",
    is_thin_payload: bool = False,
    *,
    enabled: Optional[bool] = None,
) -> int:
    """Return Gemini score floor for hybrid promotion under the given policy."""
    if enabled is None:
        enabled = HYBRID_PROMOTION_ENABLED
    if not enabled:
        return 11  # unreachable on 0–10 scale → hybrid never fires

    mode = str(policy_mode or "balanced").strip().lower()
    if mode == "recovery":
        floor = HYBRID_SCORE_FLOOR_RECOVERY
    elif mode == "strict":
        floor = HYBRID_SCORE_FLOOR_STRICT
    else:
        floor = HYBRID_SCORE_FLOOR_BALANCED

    floor = max(1, min(10, int(floor)))
    if is_thin_payload:
        floor = min(10, floor + max(0, HYBRID_THIN_PAYLOAD_EXTRA))
    return floor


def has_basic_hybrid_signals(
    adapted_evaluation: Optional[Mapping[str, Any]] = None,
    *,
    gemini_score: int = 0,
) -> bool:
    """Require minimal evidence beyond a raw score for hybrid promotion.

    Accepts any of: meaningful pain, contact, company name, or tier MEDIUM+.
    Score alone is insufficient (avoids promoting empty/failed evaluations).
    """
    if gemini_score <= 0:
        return False
    eval_data = adapted_evaluation if isinstance(adapted_evaluation, Mapping) else {}
    if _is_meaningful_text(eval_data.get("pain_summary")):
        return True
    if _is_meaningful_text(eval_data.get("contact_point")):
        return True
    if _is_meaningful_text(eval_data.get("company_name")):
        return True
    if _is_meaningful_text(eval_data.get("pain_point")):
        return True
    tier = str(eval_data.get("tier") or "").strip().upper()
    if tier in {"HIGH", "MEDIUM"} and gemini_score >= 4:
        return True
    # Explicit Gemini confidence is a basic signal.
    level = str(eval_data.get("confidence_level") or "").strip().upper()
    if level in {"HIGH", "MEDIUM"} and gemini_score >= 4:
        return True
    return False


def apply_hybrid_promotion(
    confidence_bundle: Dict[str, Any],
    *,
    gemini_score: int,
    policy_mode: str = "balanced",
    is_thin_payload: bool = False,
    adapted_evaluation: Optional[Mapping[str, Any]] = None,
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Merge confidence promotion with optional Gemini score floor (hybrid rule).

    Promotion is True if either:
      1. ``confidence_bundle["promotion"]`` is already True, or
      2. Hybrid is enabled, Gemini score >= policy floor, and basic signals exist.

    Returns a new confidence_bundle dict (does not mutate the input) with extra
    observability keys: hybrid_promotion_triggered, hybrid_score_floor,
    hybrid_eligible, promotion_path.
    """
    if enabled is None:
        enabled = HYBRID_PROMOTION_ENABLED

    bundle = dict(confidence_bundle or {})
    score = _coerce_int_score(gemini_score, 0)
    floor = resolve_hybrid_score_floor(
        policy_mode, is_thin_payload=is_thin_payload, enabled=enabled
    )
    basic_ok = has_basic_hybrid_signals(adapted_evaluation, gemini_score=score)
    hybrid_eligible = bool(enabled) and score >= floor and basic_ok
    confidence_promoted = bool(bundle.get("promotion"))

    hybrid_triggered = False
    if confidence_promoted:
        promotion_path = "confidence"
    elif hybrid_eligible:
        hybrid_triggered = True
        promotion_path = "hybrid_score"
        bundle["promotion"] = True
        # Enrich reason for observability without erasing confidence diagnostics.
        prior_reason = str(bundle.get("reason") or "weak-evidence fallback")
        bundle["reason"] = (
            f"hybrid-score-floor (gemini_score={score} >= {floor}; "
            f"prior={prior_reason})"
        )
    else:
        promotion_path = "none"
        bundle["promotion"] = False

    bundle["hybrid_promotion_enabled"] = bool(enabled)
    bundle["hybrid_promotion_triggered"] = hybrid_triggered
    bundle["hybrid_score_floor"] = floor
    bundle["hybrid_eligible"] = hybrid_eligible
    bundle["hybrid_basic_signals"] = basic_ok
    bundle["promotion_path"] = promotion_path
    bundle["gemini_score"] = score
    return bundle


def calculate_lead_confidence(
    evaluation: Optional[Dict[str, Any]] = None,
    text: str = "",
    url: str = "",
    source_tier: str = "High",
    is_harvest_lead: bool = False,
    is_thin_payload: bool = False,
    is_thin_bio: bool = False,
    campaign: Optional[Dict[str, Any]] = None,
    threshold_adjustment: float = 0.0,
) -> Dict[str, Any]:
    """Return a deterministic confidence bundle and promotion decision.

    ``evaluation`` may be harvest-shaped or final_score_and_dm-shaped. Callers
    on the Serper path should prefer ``adapt_gemini_evaluation_for_confidence``
    first so Gemini score/confidence_level feed evidence_strength.
    """
    eval_data = evaluation or {}
    score = _coerce_int_score(eval_data.get("score"), 0)
    tier = str(eval_data.get("tier", "LOW")).upper()
    if tier not in {"HIGH", "MEDIUM", "LOW"}:
        tier = "LOW"
    topic_coherence = max(0.0, min(1.0, _coerce_float(eval_data.get("topic_coherence"), 0.0)))
    pain_summary = str(eval_data.get("pain_summary", "") or "")
    if not _is_meaningful_text(pain_summary):
        pain_summary = ""
    contact_point = str(eval_data.get("contact_point", "") or "")
    if not _is_meaningful_text(contact_point):
        contact_point = ""
    geo_match = bool(eval_data.get("geo_match", False))

    lowered_text = (text or "").lower()
    lowered_url = (url or "").lower()

    evidence_strength = 0.0
    if tier == "HIGH":
        evidence_strength += 0.45
    elif tier == "MEDIUM":
        evidence_strength += 0.25

    if topic_coherence >= 0.8:
        evidence_strength += 0.25
    elif topic_coherence >= 0.6:
        evidence_strength += 0.15
    elif topic_coherence >= 0.3:
        evidence_strength += 0.05

    if pain_summary:
        evidence_strength += 0.1
    if contact_point:
        evidence_strength += 0.1
    if geo_match:
        evidence_strength += 0.05

    # Soft contribution from raw Gemini score when present (does not replace tier).
    # Caps at +0.10 so harvest path behaviour stays dominant when fields are rich.
    if score >= 8:
        evidence_strength += 0.10
    elif score >= 6:
        evidence_strength += 0.05

    intent_signal = 0.0
    if any(term in lowered_text for term in ["looking for", "need help", "urgent", "broken", "problem", "hire", "buy", "recommend", "budget", "switch"]):
        intent_signal += 0.3
    if any(term in lowered_url for term in ["reddit", "forum", "community", "review", "jobs", "careers"]):
        intent_signal += 0.1
    if is_harvest_lead:
        intent_signal += 0.2

    urgency = 0.0
    if any(term in lowered_text for term in ["urgently", "immediately", "today", "now", "need", "soon", "budget"]):
        urgency += 0.2
    if any(term in lowered_text for term in ["looking for", "need help", "recommend"]):
        urgency += 0.1

    source_bonus = 0.0
    if source_tier == "High":
        source_bonus += 0.1
    elif source_tier == "Medium":
        source_bonus += 0.05

    if is_thin_payload:
        source_bonus -= 0.1
    if is_thin_bio:
        source_bonus -= 0.05

    confidence = min(1.0, max(0.0, evidence_strength + intent_signal + urgency + source_bonus))
    confidence_score = round(confidence * 100, 1)

    # Promotion logic: promotion threshold is intentionally explained and tunable.
    threshold = 62.0
    if is_harvest_lead:
        threshold = 56.0
    if is_thin_payload:
        threshold = 68.0
    if is_thin_bio:
        threshold = 64.0

    threshold = max(45.0, min(78.0, threshold + float(threshold_adjustment or 0.0)))
    promotion = confidence_score >= threshold
    return {
        "confidence_score": confidence_score,
        "confidence_threshold": threshold,
        "promotion": promotion,
        "reason": (
            "high-intent evidence" if confidence_score >= 80 else
            "medium-intent evidence" if confidence_score >= 65 else
            "weak-evidence fallback"
        ),
        "evidence_strength": round(evidence_strength, 3),
        "intent_signal": round(intent_signal, 3),
        "urgency": round(urgency, 3),
        "source_bonus": round(source_bonus, 3),
        "source_tier": source_tier,
        "is_harvest_lead": is_harvest_lead,
        "is_thin_payload": is_thin_payload,
        "is_thin_bio": is_thin_bio,
        "threshold_adjustment": round(float(threshold_adjustment or 0.0), 2),
        "gemini_score": score,
        "adapted_tier": tier,
        "adapted_topic_coherence": topic_coherence,
    }
