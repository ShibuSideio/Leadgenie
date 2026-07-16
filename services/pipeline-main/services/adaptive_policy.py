"""Adaptive campaign policy controls for dispatch gate behavior.

Combines queue health, velocity pressure, campaign context, and domain profile
signals (strictness_bias, liquidity) into a single threshold_adjustment used by
calculate_lead_confidence() for promotion decisions.
"""
from __future__ import annotations

from typing import Any, Mapping


# Maps domain strictness_bias ∈ [-0.5, +0.5] onto confidence-threshold points
# (same units as threshold_adjustment: added to base ~56–68 gate).
# Max impact ≈ ±4.0 points — meaningful without overwhelming recovery (-6).
_STRICTNESS_BIAS_SCALE = 8.0

# Dampen domain threshold impact when profile_confidence is not high
# (thin / uncertain campaigns should not swing the gate aggressively).
_PROFILE_CONFIDENCE_SCALE = {
    "high": 1.0,
    "medium": 0.60,
    "low": 0.30,
}

# Clamp bounds for the aggregate threshold adjustment (legacy envelope).
_ADJ_MIN = -10.0
_ADJ_MAX = 4.0


def _coerce_strictness_bias(raw: Any) -> float | None:
    """Parse strictness_bias if present; return None when absent/unusable."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return max(-0.5, min(0.5, value))


def _profile_confidence_label(profile: Mapping[str, Any]) -> str:
    raw = str(profile.get("profile_confidence") or "").strip().lower()
    if raw in _PROFILE_CONFIDENCE_SCALE:
        return raw
    # Backward compatible: derive from thin_campaign / numeric confidence.
    if bool(profile.get("thin_campaign")) or bool(profile.get("soft_domain_adjustments")):
        return "low"
    try:
        conf = float(profile.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf >= 0.65:
        return "high"
    if conf >= 0.40:
        return "medium"
    return "low"


def _domain_threshold_delta(
    domain_profile: Mapping[str, Any] | None,
    mode: str,
) -> tuple[float, dict[str, Any]]:
    """Compute domain contribution to threshold_adjustment.

    Preferred path (domain-v2+): use ``strictness_bias`` scaled into threshold
    points. Negative bias → easier promotion; positive → stricter.

    Low ``profile_confidence`` damps the scale so thin campaigns do not
    over-correct the promotion gate.

    Legacy path (no strictness_bias): preserve pre-v3 heuristics based on
    ``low_liquidity_market`` and hard-coded domain families.

    Returns:
        (delta, meta) where meta describes what was applied for logging.
    """
    profile = domain_profile if isinstance(domain_profile, Mapping) else {}
    domain_family = str(profile.get("domain_family") or "general_services")
    liquidity_level = str(profile.get("liquidity_level") or "").lower().strip()
    low_liquidity = bool(profile.get("low_liquidity_market"))
    if not low_liquidity and liquidity_level == "low":
        low_liquidity = True
    profile_confidence = _profile_confidence_label(profile) if profile else "low"
    conf_scale = float(_PROFILE_CONFIDENCE_SCALE.get(profile_confidence, 0.30))

    bias = _coerce_strictness_bias(profile.get("strictness_bias"))
    meta: dict[str, Any] = {
        "domain_family": domain_family,
        "liquidity_level": liquidity_level or ("low" if low_liquidity else ""),
        "low_liquidity_market": low_liquidity,
        "strictness_bias": bias,
        "profile_confidence": profile_confidence,
        "thin_campaign": bool(profile.get("thin_campaign")),
        "strictness_applied": False,
        "legacy_domain_heuristics": False,
        "delta": 0.0,
        "confidence_scale": conf_scale,
    }

    if not profile:
        return 0.0, meta

    # --- domain-v2 primary signal ---
    if bias is not None:
        # Positive bias raises the bar; negative bias lowers it.
        # Damped by profile_confidence so low-quality detection is conservative.
        effective_scale = _STRICTNESS_BIAS_SCALE * conf_scale
        delta = round(bias * effective_scale, 3)
        meta["strictness_applied"] = True
        meta["strictness_scale"] = round(effective_scale, 3)
        meta["delta"] = delta
        return delta, meta

    # --- legacy fallback (domain-v1 or incomplete profiles) ---
    delta = 0.0
    if low_liquidity and mode != "strict":
        delta -= 1.5 * conf_scale
    if domain_family in {"real_estate", "manufacturing"} and mode == "recovery":
        delta -= 1.0 * conf_scale
    meta["legacy_domain_heuristics"] = delta != 0.0
    meta["delta"] = round(delta, 3)
    return round(delta, 3), meta


def build_dispatch_policy(
    campaign: dict[str, Any],
    sourcing_vector: str,
    queue_depth: int,
    recent_new_count: int,
    recent_enrichment_pending_count: int,
    velocity_threshold: int,
    domain_profile: dict[str, Any] | None = None,
    gate_read_failed: bool = False,
) -> dict[str, Any]:
    """Build adaptive dispatch policy including domain-aware promotion bias.

    Args:
        campaign: Firestore campaign document.
        sourcing_vector: B2B / B2C / D2C / B2B2C.
        queue_depth: Current unprocessed queue size.
        recent_new_count: Leads created in the velocity window.
        recent_enrichment_pending_count: Enrichment backlog count.
        velocity_threshold: Medium-tier intake cap baseline.
        domain_profile: Full ``system_domain_profile`` (domain-v2 preferred).
            When provided, ``strictness_bias`` adjusts the confidence gate.
            Other fields (e.g. ``liquidity_level``) are echoed for future use.
        gate_read_failed: True when velocity Firestore reads failed.

    Returns:
        Policy dict. Consumers should use ``threshold_adjustment`` as the
        single value passed to ``calculate_lead_confidence``. Domain-specific
        contribution is also exposed as ``domain_threshold_delta`` for logs.
    """
    recent_count = max(0, int(recent_new_count or 0) + int(recent_enrichment_pending_count or 0))
    threshold = max(1, int(velocity_threshold or 1))
    pressure_ratio = min(3.0, recent_count / float(threshold))

    exhaustion_zeros = int(campaign.get("_query_exhaustion_consecutive_zeros") or 0)
    exhaustion_level = int(campaign.get("_query_exhaustion_escalation_level") or 0)
    thin_context = len((campaign.get("effective_bio") or campaign.get("bio") or "").strip()) < 50
    is_consumer = str(sourcing_vector or "").upper() in {"B2C", "D2C", "B2B2C"}

    starvation_score = 0
    if queue_depth <= 2:
        starvation_score += 2
    if exhaustion_zeros >= 2:
        starvation_score += 1
    if exhaustion_level >= 1:
        starvation_score += 2
    if recent_new_count <= 0:
        starvation_score += 1
    if gate_read_failed:
        starvation_score += 1

    mode = "balanced"
    if starvation_score >= 4:
        mode = "recovery"
    elif pressure_ratio > 1.15 and recent_new_count > 0:
        mode = "strict"

    # Base operational adjustments (queue health / context).
    threshold_adjustment = 0.0
    if mode == "recovery":
        threshold_adjustment -= 6.0
    elif mode == "strict":
        threshold_adjustment += 2.0
    if thin_context:
        threshold_adjustment -= 2.0
    if is_consumer and mode != "strict":
        threshold_adjustment -= 1.0

    # Domain profile contribution (strictness_bias preferred).
    domain_delta, domain_meta = _domain_threshold_delta(domain_profile, mode)
    threshold_adjustment += domain_delta

    threshold_adjustment = max(_ADJ_MIN, min(_ADJ_MAX, threshold_adjustment))

    medium_budget = 4
    if mode == "recovery":
        medium_budget = 8 if is_consumer else 6
    elif mode == "strict":
        medium_budget = 2

    # Mild medium-budget lift for low-liquidity markets in recovery so sparse
    # verticals can still feed the funnel. Does not affect the score gate.
    profile = domain_profile if isinstance(domain_profile, Mapping) else {}
    liquidity_level = str(
        domain_meta.get("liquidity_level")
        or profile.get("liquidity_level")
        or ""
    ).lower()
    if mode == "recovery" and (
        liquidity_level == "low" or bool(domain_meta.get("low_liquidity_market"))
    ):
        medium_budget = min(10, medium_budget + 1)

    degraded_medium_budget = max(
        1,
        min(10, medium_budget // 2 if mode != "recovery" else medium_budget),
    )

    domain_family = str(domain_meta.get("domain_family") or "general_services")

    return {
        "policy_version": "adaptive-v3",
        "mode": mode,
        "pressure_ratio": round(pressure_ratio, 3),
        "starvation_score": starvation_score,
        "threshold_adjustment": round(threshold_adjustment, 3),
        # Domain explainability — used by dispatch logging / scored_out docs.
        "domain_threshold_delta": round(float(domain_meta.get("delta") or 0.0), 3),
        "domain_strictness_bias": domain_meta.get("strictness_bias"),
        "domain_strictness_applied": bool(domain_meta.get("strictness_applied")),
        "domain_legacy_heuristics": bool(domain_meta.get("legacy_domain_heuristics")),
        "profile_confidence": domain_meta.get("profile_confidence"),
        "thin_campaign": bool(domain_meta.get("thin_campaign")),
        "domain_confidence_scale": domain_meta.get("confidence_scale"),
        "medium_budget": medium_budget,
        "degraded_medium_budget": degraded_medium_budget,
        "thin_context": thin_context,
        "is_consumer": is_consumer,
        "domain_family": domain_family,
        "liquidity_level": liquidity_level or None,
        "low_liquidity_market": bool(domain_meta.get("low_liquidity_market")),
        # Full profile reference for future policy knobs (preferred_sources, etc.).
        "domain_profile": dict(profile) if profile else None,
    }
