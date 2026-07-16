"""Shared domain-aware gate helpers for intent / promotion thresholds.

Used by:
  - pipeline adaptive_policy (dispatch confidence gate pattern)
  - orchestrator Inbound Radar (intent score floor)

Keeps threshold math consistent without coupling orchestrator to pipeline-main.
"""
from __future__ import annotations

from typing import Any, Mapping

# Same damping curve as adaptive_policy for thin / low-confidence profiles.
_PROFILE_CONFIDENCE_SCALE = {
    "high": 1.0,
    "medium": 0.60,
    "low": 0.30,
}


def _coerce_float(raw: Any, default: float | None = None) -> float | None:
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return value


def profile_confidence_label(domain_profile: Mapping[str, Any] | None) -> str:
    """Return high|medium|low for a system_domain_profile (or derived fallback)."""
    if not isinstance(domain_profile, Mapping) or not domain_profile:
        return "low"
    raw = str(domain_profile.get("profile_confidence") or "").strip().lower()
    if raw in _PROFILE_CONFIDENCE_SCALE:
        return raw
    if bool(domain_profile.get("thin_campaign")) or bool(
        domain_profile.get("soft_domain_adjustments")
    ):
        return "low"
    conf = _coerce_float(domain_profile.get("confidence"), 0.0) or 0.0
    if conf >= 0.65:
        return "high"
    if conf >= 0.40:
        return "medium"
    return "low"


def confidence_scale(domain_profile: Mapping[str, Any] | None) -> float:
    """Multiplicative scale for domain adjustments (1.0 = full strength)."""
    label = profile_confidence_label(domain_profile)
    return float(_PROFILE_CONFIDENCE_SCALE.get(label, 0.30))


def extract_domain_meta(domain_profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Stable domain fields for signals / leads (empty when no profile)."""
    if not isinstance(domain_profile, Mapping) or not domain_profile:
        return {
            "domain_family": None,
            "domain_source": "none",
            "profile_confidence": None,
            "thin_campaign": None,
            "strictness_bias": None,
            "override_active": None,
        }
    family = domain_profile.get("domain_family")
    source = "domain_override" if domain_profile.get("override_active") else "system_domain_profile"
    if not family:
        source = "none"
    return {
        "domain_family": str(family) if family else None,
        "domain_source": source,
        "profile_confidence": profile_confidence_label(domain_profile),
        "thin_campaign": bool(domain_profile.get("thin_campaign")),
        "strictness_bias": _coerce_float(domain_profile.get("strictness_bias")),
        "override_active": bool(domain_profile.get("override_active")),
    }


def compute_intent_threshold(
    base: float,
    domain_profile: Mapping[str, Any] | None = None,
    *,
    floor: float | None = None,
    ceiling: float | None = None,
    bias_unit: float = 0.12,
) -> tuple[float, dict[str, Any]]:
    """Adjust an intent score threshold using domain strictness_bias.

    Mirrors dispatch semantics:
      - positive strictness_bias → higher bar (stricter)
      - negative strictness_bias → lower bar (more lenient)
      - low profile_confidence → attenuated adjustment

    When *domain_profile* is missing/empty, returns *base* unchanged
    (full backward compatibility).

    Args:
        base: Default threshold (e.g. 0.45 write floor, 0.30 gemini floor).
        domain_profile: Campaign system_domain_profile or None.
        floor / ceiling: Optional clamps (defaults: base±0.12 band).
        bias_unit: Multiplier so bias ∈ [-0.5, 0.5] maps to ±bias_unit/2
            at full confidence before confidence_scale damping.
            Effective delta = strictness_bias * bias_unit * confidence_scale.

    Returns:
        (effective_threshold, meta_dict for logging)
    """
    base_f = float(base)
    lo = float(floor) if floor is not None else max(0.0, base_f - 0.12)
    hi = float(ceiling) if ceiling is not None else min(1.0, base_f + 0.12)

    meta: dict[str, Any] = {
        "domain_applied": False,
        "base_threshold": round(base_f, 4),
        "effective_threshold": round(base_f, 4),
        "threshold_delta": 0.0,
        "strictness_bias": None,
        "profile_confidence": None,
        "confidence_scale": 1.0,
        "domain_family": None,
        "thin_campaign": False,
    }

    if not isinstance(domain_profile, Mapping) or not domain_profile:
        return base_f, meta

    family = domain_profile.get("domain_family")
    if not family and domain_profile.get("strictness_bias") is None:
        return base_f, meta

    bias = _coerce_float(domain_profile.get("strictness_bias"), 0.0)
    if bias is None:
        bias = 0.0
    bias = max(-0.5, min(0.5, bias))
    conf_label = profile_confidence_label(domain_profile)
    scale = float(_PROFILE_CONFIDENCE_SCALE.get(conf_label, 0.30))
    delta = bias * float(bias_unit) * scale
    effective = max(lo, min(hi, base_f + delta))

    meta.update({
        "domain_applied": abs(delta) > 1e-9 or bool(family),
        "effective_threshold": round(effective, 4),
        "threshold_delta": round(delta, 4),
        "strictness_bias": round(bias, 4),
        "profile_confidence": conf_label,
        "confidence_scale": scale,
        "domain_family": str(family) if family else None,
        "thin_campaign": bool(domain_profile.get("thin_campaign")),
    })
    return effective, meta


# ---------------------------------------------------------------------------
# Enrichment priority — actionable signal for firmographic / graph work
# ---------------------------------------------------------------------------
#
# Downstream jobs should order work by enrichment_priority and use
# enrichment_plan_for_priority() for depth (lookups, deep graph, queue).
# Sort key: ENRICHMENT_PRIORITY_RANK (lower = process first).
# ---------------------------------------------------------------------------

# B2B / firmographic-friendly verticals (IP→company enrichment is high ROI).
_HIGH_VALUE_FIRMOGRAPHIC_FAMILIES = frozenset({
    "saas",
    "manufacturing",
    "professional_services",
    "finance",
    "logistics",
    "construction",
    "hr_recruiting",
    "marketing_agency",
})

# Consumer-leaning verticals: reverse-IP often resolves to ISP, not buyer.
_LOW_VALUE_FIRMOGRAPHIC_FAMILIES = frozenset({
    "real_estate",
    "ecommerce",
    "hospitality",
})

# Sort rank for queue ordering (lower = process sooner).
ENRICHMENT_PRIORITY_RANK: dict[str, int] = {
    "high": 0,
    "medium": 1,
    "low": 2,
}

# Depth / queue policy for each priority (consumers should treat as contract).
_ENRICHMENT_PLANS: dict[str, dict[str, Any]] = {
    "high": {
        "priority": "high",
        "rank": 0,
        "queue": "realtime",
        "resolve_company": True,
        "max_lookups": 5,
        "deep_graph": True,
        "batch_size": 50,
        "skip_if_budget_tight": False,
        "notes": "Full firmographic + graph enrichment; process first.",
    },
    "medium": {
        "priority": "medium",
        "rank": 1,
        "queue": "batch",
        "resolve_company": True,
        "max_lookups": 2,
        "deep_graph": False,
        "batch_size": 100,
        "skip_if_budget_tight": False,
        "notes": "Standard company resolve; skip expensive graph hops.",
    },
    "low": {
        "priority": "low",
        "rank": 2,
        "queue": "deferred",
        "resolve_company": False,
        "max_lookups": 1,
        "deep_graph": False,
        "batch_size": 200,
        "skip_if_budget_tight": True,
        "notes": "Thin/low-confidence or weak firmographic fit; defer or skip.",
    },
}


def firmographic_value_for_family(domain_family: str | None) -> str:
    """Return high|medium|low firmographic ROI for a domain family."""
    family = str(domain_family or "").strip().lower()
    if family in _HIGH_VALUE_FIRMOGRAPHIC_FAMILIES:
        return "high"
    if family in _LOW_VALUE_FIRMOGRAPHIC_FAMILIES:
        return "low"
    if family:
        return "medium"
    return "medium"


def compute_enrichment_priority(
    domain_profile: Mapping[str, Any] | None = None,
    *,
    intent_score: float | None = None,
    sourcing_vector: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Compute enrichment_priority from domain profile (+ optional intent).

    Priority is designed for **downstream processing order and depth**, not as
    a hard reject. Typical consumers:

      plan = enrichment_plan_for_priority(priority)
      docs.sort(key=enrichment_sort_key)

    Rules (in order):
      1. No profile → ``medium`` (legacy default; writers may omit the field).
      2. Thin / low profile_confidence → ``low`` (mild exceptions below).
      3. High profile_confidence → ``high``, soft-demoted for consumer families
         without manual override.
      4. Medium confidence → boosted for B2B firmographic families, lowered
         for consumer families.
      5. Optional high intent_score (≥0.70) can promote medium → high.
      6. Consumer sourcing_vector (B2C/D2C) demotes high → medium when family
         is consumer-leaning.

    Returns:
        (priority_label, explain_meta)
    """
    meta: dict[str, Any] = {
        "enrichment_priority": "medium",
        "profile_confidence": None,
        "domain_family": None,
        "firmographic_value": "medium",
        "thin_campaign": False,
        "score": 0,
        "reasons": [],
    }

    if not isinstance(domain_profile, Mapping) or not domain_profile:
        meta["reasons"] = ["no_domain_profile_default_medium"]
        return "medium", meta

    family = str(domain_profile.get("domain_family") or "").strip().lower() or None
    conf_label = profile_confidence_label(domain_profile)
    thin = bool(domain_profile.get("thin_campaign")) or bool(
        domain_profile.get("soft_domain_adjustments")
    )
    override = bool(domain_profile.get("override_active"))
    firm_val = firmographic_value_for_family(family)
    vector = str(sourcing_vector or domain_profile.get("sourcing_vector") or "").upper()

    meta.update({
        "profile_confidence": conf_label,
        "domain_family": family,
        "firmographic_value": firm_val,
        "thin_campaign": thin,
        "override_active": override,
        "sourcing_vector": vector or None,
    })
    reasons: list[str] = []

    # Numeric score for explainability / future continuous priority.
    score = 0
    score += {"high": 40, "medium": 22, "low": 8}.get(conf_label, 8)
    score += {"high": 18, "medium": 8, "low": -6}.get(firm_val, 8)
    if thin:
        score -= 18
        reasons.append("thin_or_soft_profile")
    if override:
        score += 6
        reasons.append("manual_override_boost")
    conf_num = _coerce_float(domain_profile.get("confidence"), 0.0) or 0.0
    score += int(round(conf_num * 8))

    # Primary tier decision
    if conf_label == "low" or thin:
        priority = "low"
        reasons.append("low_confidence_or_thin")
        # Manual override on a thin campaign still gets deferred medium if B2B.
        if override and firm_val == "high" and conf_label != "low":
            priority = "medium"
            reasons.append("override_b2b_soft_promote")
        elif override and firm_val == "high":
            # Even low conf override: allow medium for high-value B2B only.
            priority = "medium"
            reasons.append("override_high_value_family_medium")
    elif conf_label == "high":
        priority = "high"
        reasons.append("high_profile_confidence")
        if firm_val == "low" and not override:
            priority = "medium"
            reasons.append("consumer_family_demote")
            score -= 10
    else:  # medium confidence
        if firm_val == "high":
            priority = "high"
            reasons.append("medium_conf_high_value_family")
        elif firm_val == "low":
            priority = "low"
            reasons.append("medium_conf_consumer_family")
        else:
            priority = "medium"
            reasons.append("medium_conf_default")

    # Intent score (inbound sentiment path) can promote medium → high.
    if intent_score is not None:
        try:
            iscore = float(intent_score)
        except (TypeError, ValueError):
            iscore = None
        if iscore is not None:
            meta["intent_score"] = round(iscore, 3)
            if iscore >= 0.70 and priority == "medium":
                priority = "high"
                reasons.append("high_intent_promote")
                score += 12
            elif iscore < 0.40 and priority == "high" and conf_label != "high":
                priority = "medium"
                reasons.append("weak_intent_demote")
                score -= 8

    # Consumer sourcing vectors: reverse-IP firmographics less useful.
    if vector in {"B2C", "D2C", "B2B2C"} and firm_val == "low" and priority == "high":
        priority = "medium"
        reasons.append("consumer_sourcing_vector_demote")
        score -= 8

    # Final safety: thin always at most medium (never burn realtime budget).
    if thin and priority == "high":
        priority = "medium"
        reasons.append("thin_cap_medium")

    meta["enrichment_priority"] = priority
    meta["score"] = int(score)
    meta["reasons"] = reasons
    meta["plan"] = dict(_ENRICHMENT_PLANS.get(priority, _ENRICHMENT_PLANS["medium"]))
    return priority, meta


def enrichment_plan_for_priority(priority: str | None) -> dict[str, Any]:
    """Return processing plan dict for a priority label (downstream contract).

    Example::

        plan = enrichment_plan_for_priority(doc.get("enrichment_priority"))
        if plan["skip_if_budget_tight"] and budget_low:
            continue
        if plan["resolve_company"]:
            resolve(doc, max_lookups=plan["max_lookups"])
    """
    key = str(priority or "medium").strip().lower()
    if key not in _ENRICHMENT_PLANS:
        key = "medium"
    return dict(_ENRICHMENT_PLANS[key])


def enrichment_sort_key(doc: Mapping[str, Any] | None) -> tuple:
    """Sort key for processing queues: high priority first, then newer intent.

    Usage::

        signals.sort(key=enrichment_sort_key)

    Missing priority sorts as medium (rank 1) for backward compatibility.
    """
    if not isinstance(doc, Mapping):
        return (1, 0.0, "")
    pr = str(doc.get("enrichment_priority") or "medium").strip().lower()
    rank = int(ENRICHMENT_PRIORITY_RANK.get(pr, 1))
    # Higher intent / score first within same priority band.
    intent = _coerce_float(doc.get("intent_score"), None)
    if intent is None:
        # Visitor beacons may store score-like fields later; default 0.
        intent = _coerce_float(doc.get("fit_score"), 0.0) or 0.0
    # Negate intent so larger scores sort earlier in ascending sort.
    return (rank, -float(intent), str(doc.get("signal_id") or doc.get("visit_hash") or ""))


def should_run_company_resolve(
    priority: str | None,
    *,
    budget_tight: bool = False,
) -> bool:
    """Convenience gate for reverse-IP / firmographic resolvers."""
    plan = enrichment_plan_for_priority(priority)
    if budget_tight and plan.get("skip_if_budget_tight"):
        return False
    return bool(plan.get("resolve_company"))
