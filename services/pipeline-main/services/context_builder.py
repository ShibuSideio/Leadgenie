"""
context_builder.py — V24.6.1
==============================
Single source of truth for enriched campaign context assembly.

PROBLEM (confirmed 2026-07-02):
    The pipeline cherry-picked one field (bio or persona_bio) and ignored every
    other field the user filled in. Not all users are elaborate. A lazy user
    fills in only campaign name + location. A power user fills every field.
    The pipeline must extract maximum signal from WHATEVER is available.

SOLUTION:
    build_enriched_context() aggregates ALL non-empty campaign and persona
    fields into a structured context string. Handles sparse inputs gracefully,
    layers signals by relevance, and always returns a minimal viable context.

USED BY:
    - produce.py  -> generate_smart_query(bio=build_enriched_context(campaign))
    - dispatch.py -> Gemini pre-filter and scoring context
"""

from __future__ import annotations

from core.logging import get_logger  # type: ignore[import]

log = get_logger("pipeline.context_builder")


def build_enriched_context(campaign: dict) -> str:
    """Synthesize all available campaign and persona fields into a rich ICP
    context string suitable for Gemini prompts.

    Design principles:
    1. Signal maximisation: every non-empty field contributes.
    2. Graceful degradation: works with 1 field or 20 fields.
    3. Deduplication: content already represented in a richer field is skipped.
    4. LLM-optimised structure: labeled sections Gemini parses reliably.
    5. Transparent logging: logs how many signals were assembled so operators
       can diagnose thin-context campaigns without manual log expansion.

    Args:
        campaign: Firestore campaign dict (may be sparse).

    Returns:
        Structured ICP context string. Never empty -- always returns at least
        campaign name as a minimum viable context.
    """

    def _clean(val) -> str:
        if not val:
            return ""
        return str(val).strip()

    def _is_junk(val: str) -> bool:
        """True if the string is a thin auto-generated placeholder."""
        junk_tokens = {
            "product/service:", "product:", "service:", "n/a", "none",
            "tbd", "placeholder", "undefined", "null",
            "child_campaign_override", "shadow_learner",
        }
        lowered = val.lower()
        return len(val) < 3 or any(j in lowered for j in junk_tokens)

    parts: list[str] = []
    seen_content: set[str] = set()

    def _add(label: str, value: str, max_chars: int = 600) -> bool:
        value = _clean(value)
        if not value or _is_junk(value):
            return False
        value = value[:max_chars]
        dedup_key = value.lower()[:60]
        if dedup_key in seen_content:
            return False
        seen_content.add(dedup_key)
        parts.append(f"{label}: {value}")
        return True

    # Layer 1: Product / Service Description
    # Priority: effective_bio > persona_bio > bio > campaign_focus
    _effective_bio  = _clean(campaign.get("effective_bio", ""))
    _persona_bio    = _clean(campaign.get("persona_bio", ""))
    _raw_bio        = _clean(campaign.get("bio", ""))
    _campaign_focus = _clean(campaign.get("campaign_focus", ""))
    _campaign_name  = _clean(campaign.get("name", ""))

    _product_desc = max(
        [_effective_bio, _persona_bio, _raw_bio, _campaign_focus],
        key=lambda x: len(x) if x and not _is_junk(x) else 0,
    )
    if _product_desc and not _is_junk(_product_desc):
        _add("PRODUCT/SERVICE", _product_desc, max_chars=800)
    elif _campaign_name:
        _add("PRODUCT/SERVICE", _campaign_name, max_chars=200)

    # Layer 2: Market Context
    # keywords = rich market pain context authored at campaign creation
    # persona_keywords = ICP-specific keyword signals
    _keywords         = _clean(campaign.get("keywords", ""))
    _persona_keywords = _clean(campaign.get("persona_keywords", ""))
    _add("MARKET CONTEXT", _keywords, max_chars=600)
    _add("ICP KEYWORDS", _persona_keywords, max_chars=300)

    # Layer 3: Pain Signals
    # pain_point accumulates REAL buyer language from approved leads over time.
    # For established campaigns this is the most valuable field -- actual words
    # buyers used, not the seller s internal framing.
    _pain_point = _clean(campaign.get("pain_point", ""))
    _add("BUYER PAIN (observed)", _pain_point, max_chars=400)

    # Layer 4: ICP Identity
    _persona_name = _clean(campaign.get("persona_name", ""))
    if _persona_name:
        _add("TARGET ICP", _persona_name, max_chars=150)
    if _persona_bio and _persona_bio != _product_desc:
        _add("ICP DESCRIPTION", _persona_bio, max_chars=500)

    # Layer 5: Messaging Signals
    # target_angle_hook = what message resonates with the buyer
    # unfair_advantage  = the seller s differentiator
    # Both tell Gemini WHAT KIND OF BUYER this campaign attracts
    _hook = _clean(campaign.get("target_angle_hook", ""))
    _adv  = _clean(campaign.get("target_angle_adv", ""))
    _ua   = _clean(campaign.get("unfair_advantage", ""))
    _add("BUYER HOOK", _hook, max_chars=300)
    _add("COMPETITIVE ADVANTAGE", _ua or _adv, max_chars=300)

    # Layer 6: Targeting Signals (positive intent signals only)
    _targeting = campaign.get("persona_targeting_signals") or []
    if isinstance(_targeting, list):
        _positive = [
            s for s in _targeting
            if s and not str(s).upper().startswith("NOT ")
        ][:5]
        if _positive:
            _add("INTENT SIGNALS", ", ".join(_positive), max_chars=300)

    # Layer 7: Geographic Context
    _location = _clean(campaign.get("location", ""))
    _geo      = campaign.get("geo_hierarchy", {})
    _country  = _clean(_geo.get("country", "")) if isinstance(_geo, dict) else ""
    _region   = _clean(_geo.get("region",  "")) if isinstance(_geo, dict) else ""

    _geo_str = ""
    if _location and _location.lower() not in ("all", "global", ""):
        _geo_str = _location
    elif _country and _country.lower() not in ("all", "global", ""):
        _geo_str = (
            f"{_region}, {_country}"
            if _region and _region.lower() != "all"
            else _country
        )
    if _geo_str:
        _add("GEO TARGET", _geo_str, max_chars=150)

    # Layer 8: Buyer Type
    _vector = _clean(campaign.get("sourcing_vector", ""))
    if _vector:
        _add("BUYER TYPE", _vector, max_chars=50)

    # Layer 9: Intelligence Strategy — Vocabulary Notes
    # V26 Multi-Strategy OSINT Engine: vocabulary_notes describes how the ICP
    # actually speaks (colloquial language, slang, everyday terms). Passed
    # through to query_brain for colloquial query translation.
    _intel_strategy = campaign.get("intelligence_strategy") or {}
    _vocab_notes = _clean(_intel_strategy.get("vocabulary_notes", "")) if isinstance(_intel_strategy, dict) else ""
    if _vocab_notes:
        _add("AUDIENCE VOCABULARY", _vocab_notes, max_chars=500)

    # Fallback: guarantee non-empty output
    if not parts:
        _fallback = (
            _campaign_name or _campaign_focus or _raw_bio
            or _effective_bio or _keywords or "General campaign"
        )
        log.warning(
            "context_builder_minimal_fallback",
            campaign_id=campaign.get("id", "unknown"),
            fallback_preview=_fallback[:60],
            note="No rich fields found. Returning minimal campaign name as context.",
        )
        return _fallback

    result = "\n".join(parts)
    log.info(
        "context_builder_assembled",
        campaign_id=campaign.get("id", "unknown"),
        sections=len(parts),
        total_chars=len(result),
        has_pain_point=bool(_pain_point),
        has_persona_bio=bool(_persona_bio),
        has_effective_bio=bool(_effective_bio),
        has_keywords=bool(_keywords),
        has_hook=bool(_hook),
        has_geo=bool(_geo_str),
        has_vocabulary_notes=bool(_vocab_notes),
    )
    return result
