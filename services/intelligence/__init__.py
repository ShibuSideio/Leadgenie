"""V27 Intent Domain Intelligence — shared single-brain package.

Canonical entrypoint: ``intelligence.orchestrator``.
Packaged into pipeline-main as ``./intelligence`` (see Dockerfile).
"""
from __future__ import annotations

from intelligence.orchestrator import (  # noqa: F401
    IntentProfile,
    build_intent_profile,
    is_v27_orchestrator_enabled,
    channel_is_admissible,
    should_hard_drop_result,
    nourish_plan_for_profile,
    funnel_snapshot,
    INTENT_PROFILE_VERSION,
)

__all__ = [
    "IntentProfile",
    "build_intent_profile",
    "is_v27_orchestrator_enabled",
    "channel_is_admissible",
    "should_hard_drop_result",
    "nourish_plan_for_profile",
    "funnel_snapshot",
    "INTENT_PROFILE_VERSION",
]
