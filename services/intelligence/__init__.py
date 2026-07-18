"""V27 Intent Domain Intelligence — BC package.

Canonical implementation: ``shared.intent_orchestrator`` (pipeline Docker image).
This package re-exports the same API for monorepo-style imports.
"""
from __future__ import annotations

from shared.intent_orchestrator import (  # noqa: F401
    INTENT_PROFILE_VERSION,
    IntentProfile,
    build_intent_profile,
    channel_is_admissible,
    env_v27_flag,
    funnel_snapshot,
    is_v27_orchestrator_enabled,
    merge_intent_into_campaign,
    nourish_plan_for_profile,
    should_hard_drop_result,
    v27_flag_diagnostics,
)

__all__ = [
    "IntentProfile",
    "build_intent_profile",
    "is_v27_orchestrator_enabled",
    "env_v27_flag",
    "v27_flag_diagnostics",
    "channel_is_admissible",
    "should_hard_drop_result",
    "nourish_plan_for_profile",
    "funnel_snapshot",
    "INTENT_PROFILE_VERSION",
]
