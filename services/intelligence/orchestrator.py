"""BC re-export of V27 IntentDomainOrchestrator.

SSOT lives in ``shared.intent_orchestrator`` (always packaged into pipeline-main
via ``COPY services/shared ./shared``). This module remains for monorepo
imports of ``intelligence.orchestrator``.
"""
from __future__ import annotations

from shared.intent_orchestrator import *  # noqa: F401,F403
from shared.intent_orchestrator import (  # noqa: F401
    INTENT_PROFILE_VERSION,
    IntentProfile,
    apply_intent_to_governance_stats,
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
