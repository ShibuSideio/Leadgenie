"""
Pipeline-Main — Shared constants module.

V24.3 (L2-2): Centralise constants that must stay in sync across multiple
service modules. Previously _CONSUMER_ARCHETYPES was duplicated in both
query_brain.py and serper_service.py with a comment saying "MUST stay in sync".
This module is the single source of truth.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Sourcing vector classification constants
# ---------------------------------------------------------------------------

# Consumer archetypes — vectors where the end buyer is an individual, not
# a corporate entity. These routes receive:
#   - Consumer-focused Gemini prompts (forum/review dorks)
#   - Temporal freshness filter (tbs=qdr:m) instead of all-time
#   - URL-path-level dedup (not domain-level)
#   - B2B jargon scrubbing on generated queries
#   - NO pain_point historical_str suffix
CONSUMER_ARCHETYPES: frozenset[str] = frozenset({"B2C", "B2B2C", "D2C"})

# D2C-specific archetypes — direct-to-consumer brands that need competitor
# product comparison signals rather than local service buyer signals.
D2C_ARCHETYPES: frozenset[str] = frozenset({"D2C"})

# B2B2C archetypes — dual ICP: institutional buyer + individual end-user.
B2B2C_ARCHETYPES: frozenset[str] = frozenset({"B2B2C"})
