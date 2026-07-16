"""Adaptive campaign policy controls for dispatch gate behavior."""
from __future__ import annotations

from typing import Any


def build_dispatch_policy(
    campaign: dict[str, Any],
    sourcing_vector: str,
    queue_depth: int,
    recent_new_count: int,
    recent_enrichment_pending_count: int,
    velocity_threshold: int,
    gate_read_failed: bool = False,
) -> dict[str, Any]:
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

    threshold_adjustment = 0.0
    if mode == "recovery":
        threshold_adjustment -= 6.0
    elif mode == "strict":
        threshold_adjustment += 2.0
    if thin_context:
        threshold_adjustment -= 2.0
    if is_consumer and mode != "strict":
        threshold_adjustment -= 1.0

    threshold_adjustment = max(-10.0, min(4.0, threshold_adjustment))

    medium_budget = 4
    if mode == "recovery":
        medium_budget = 8 if is_consumer else 6
    elif mode == "strict":
        medium_budget = 2

    degraded_medium_budget = max(1, min(10, medium_budget // 2 if mode != "recovery" else medium_budget))

    return {
        "policy_version": "adaptive-v1",
        "mode": mode,
        "pressure_ratio": round(pressure_ratio, 3),
        "starvation_score": starvation_score,
        "threshold_adjustment": threshold_adjustment,
        "medium_budget": medium_budget,
        "degraded_medium_budget": degraded_medium_budget,
        "thin_context": thin_context,
        "is_consumer": is_consumer,
    }

