"""Deterministic campaign auto-enrichment for sparse user-authored inputs."""
from __future__ import annotations

import datetime
import re
from urllib.parse import urlparse

from shared.intelligence_profile import build_intelligence_strategy_plan

_ENRICHMENT_VERSION = "2026-07-15-auto-enrichment-v1"
_BROAD_LOCATION_TOKENS = frozenset(
    {
        "all",
        "global",
        "worldwide",
        "asia",
        "africa",
        "europe",
        "north america",
        "south america",
        "oceania",
        "middle east",
    }
)
_PLATFORM_DOMAIN_MAP = {
    "property finder": "propertyfinder.com",
    "bayut": "bayut.com",
    "dubizzle": "dubizzle.com",
    "olx": "olx.com",
    "realtor": "realtor.com",
    "zillow": "zillow.com",
    "rightmove": "rightmove.co.uk",
    "99acres": "99acres.com",
    "magicbricks": "magicbricks.com",
    "housing": "housing.com",
    "g2": "g2.com",
    "capterra": "capterra.com",
    "trustpilot": "trustpilot.com",
    "yelp": "yelp.com",
    "linkedin": "linkedin.com",
    "glassdoor": "glassdoor.com",
}
_INTENT_WORDS = frozenset(
    {
        "buy",
        "rent",
        "lease",
        "sale",
        "find",
        "looking",
        "need",
        "hire",
        "vendor",
        "software",
        "provider",
        "compare",
        "review",
        "investment",
        "property",
        "apartment",
        "villa",
        "office",
    }
)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = _clean(item)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _split_phrases(raw: str) -> list[str]:
    if not raw:
        return []
    return [
        part.strip()
        for part in re.split(r"[,;\n]+", raw)
        if part and part.strip()
    ]


def _extract_domain(target: str) -> str:
    raw = _clean(target).lower()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = urlparse(raw).netloc.lower()
    raw = raw.strip().strip("/")
    raw = raw.removeprefix("www.")
    return raw


def _canonicalize_platform_targets(raw_targets: list[str], inferred_targets: list[str]) -> list[str]:
    resolved: list[str] = []
    for target in _dedupe_preserve(list(raw_targets or []) + list(inferred_targets or [])):
        domain = _extract_domain(target)
        if "." in domain and " " not in domain:
            resolved.append(domain)
            continue

        target_lower = target.lower()
        matched_domain = ""
        for brand, known_domain in _PLATFORM_DOMAIN_MAP.items():
            if brand in target_lower:
                matched_domain = known_domain
                break
        if matched_domain:
            resolved.append(matched_domain)
    return _dedupe_preserve(resolved)[:5]


def _normalize_location(location: str, geo_hierarchy: dict | None) -> tuple[str, str]:
    parts = [
        part.strip()
        for part in re.split(r"[,/|]+", _clean(location))
        if part and part.strip()
    ]
    narrow_parts = [
        part for part in parts
        if part.lower() not in _BROAD_LOCATION_TOKENS
    ]

    geo = geo_hierarchy or {}
    region = _clean(geo.get("region", ""))
    country = _clean(geo.get("country", ""))

    if narrow_parts:
        normalized = ", ".join(_dedupe_preserve(narrow_parts[:3]))
    elif region and region.lower() not in _BROAD_LOCATION_TOKENS and country:
        normalized = f"{region}, {country}"
    else:
        normalized = country or region

    if not normalized:
        return "", "unknown"
    if "," in normalized:
        return normalized, "regional"
    if region and normalized.lower() == region.lower():
        return normalized, "regional"
    return normalized, "country"


def _is_broad_location(location: str) -> bool:
    parts = [
        part.strip().lower()
        for part in re.split(r"[,/|]+", _clean(location))
        if part and part.strip()
    ]
    if not parts:
        return True
    return any(part in _BROAD_LOCATION_TOKENS for part in parts)


def _infer_sector_text(campaign: dict) -> str:
    return " ".join(
        [
            _clean(campaign.get("name")),
            _clean(campaign.get("effective_bio")),
            _clean(campaign.get("bio")),
            _clean(campaign.get("keywords")),
            _clean(campaign.get("pain_point")),
            _clean(campaign.get("persona_bio")),
        ]
    ).lower()


def _derive_persona_keywords(campaign: dict, normalized_location: str, sourcing_vector: str) -> str:
    phrases = _split_phrases(_clean(campaign.get("keywords", "")))
    scored: list[tuple[int, str]] = []
    for phrase in phrases:
        lowered = phrase.lower()
        score = sum(1 for word in _INTENT_WORDS if word in lowered)
        if normalized_location and normalized_location.lower() in lowered:
            score += 1
        scored.append((score, phrase))
    scored.sort(key=lambda item: (-item[0], len(item[1])))
    chosen = [phrase for _, phrase in scored[:6]]

    sector_text = _infer_sector_text(campaign)
    location_tail = normalized_location or _clean(campaign.get("location", ""))
    if "property" in sector_text or "real estate" in sector_text or "villa" in sector_text:
        chosen.extend(
            [
                f"verified property listings {location_tail}".strip(),
                f"buy property {location_tail}".strip(),
                f"rent apartment {location_tail}".strip(),
                f"property investment {location_tail}".strip(),
            ]
        )
    elif sourcing_vector.upper().strip() == "B2B":
        chosen.extend(
            [
                "evaluating vendors",
                "switching providers",
                "implementation support",
                "roi improvement",
            ]
        )
    elif sourcing_vector.upper().strip() == "D2C":
        chosen.extend(
            [
                "best value options",
                "trusted reviews",
                "shipping and pricing",
            ]
        )

    cleaned = []
    for phrase in _dedupe_preserve(chosen):
        phrase = re.sub(r"\s{2,}", " ", phrase).strip(" ,.-")
        if len(phrase) >= 4:
            cleaned.append(phrase)
    return ", ".join(cleaned[:8])


def _derive_targeting_signals(campaign: dict, normalized_location: str, sourcing_vector: str) -> list[str]:
    sector_text = _infer_sector_text(campaign)
    location_hint = normalized_location or _clean(campaign.get("location", ""))
    signals: list[str] = []

    if "property" in sector_text or "real estate" in sector_text or "villa" in sector_text:
        signals.extend(
            [
                "looking to buy property",
                "looking to rent apartment",
                "verified listings only",
                "property investment research",
            ]
        )
        if location_hint:
            signals.append(f"relocating to {location_hint}")
        signals.extend(["NOT jobs", "NOT careers", "NOT real estate training", "NOT brokers offering services"])
    elif sourcing_vector.upper().strip() == "B2B":
        signals.extend(
            [
                "evaluating vendors",
                "implementation pain",
                "switching providers",
                "requesting recommendations",
                "NOT jobs",
                "NOT recruiters",
                "NOT agencies selling services",
            ]
        )
    else:
        signals.extend(
            [
                "looking for recommendations",
                "price comparison intent",
                "needs help choosing",
                "NOT jobs",
                "NOT careers",
            ]
        )

    return _dedupe_preserve(signals)[:8]


def _derive_target_angle_hook(campaign: dict, normalized_location: str) -> str:
    sector_text = _infer_sector_text(campaign)
    location_hint = normalized_location or "your market"
    pain_points = _split_phrases(_clean(campaign.get("pain_point", "")))
    primary_pain = pain_points[0].lower() if pain_points else ""

    if "property" in sector_text or "real estate" in sector_text:
        return (
            f"Find verified property options in {location_hint} faster without wasting time on "
            f"unreliable listings or unclear pricing."
        )
    if primary_pain:
        return f"Solve {primary_pain} faster with a simpler and more trustworthy buying process."
    return f"Help buyers in {location_hint} discover a more reliable option faster."


def _derive_unfair_advantage(campaign: dict, normalized_location: str) -> str:
    effective_bio = _clean(campaign.get("effective_bio", "")) or _clean(campaign.get("bio", ""))
    sector_text = _infer_sector_text(campaign)
    location_hint = normalized_location or "the target market"

    if "property" in sector_text or "real estate" in sector_text:
        return (
            f"Verified listings, transparent pricing, and local coverage across {location_hint} "
            f"for buyers who need trusted property options."
        )
    if effective_bio:
        first_sentence = re.split(r"(?<=[.!?])\s+", effective_bio)[0].strip()
        if first_sentence:
            return first_sentence[:220]
    return f"Sharper local context and a lower-friction buying journey in {location_hint}."


def _confidence_from_inputs(campaign: dict) -> str:
    signal_count = sum(
        1
        for field in (
            "effective_bio",
            "keywords",
            "pain_point",
            "persona_bio",
            "location",
        )
        if _clean(campaign.get(field, ""))
    )
    if signal_count >= 4:
        return "high"
    if signal_count >= 2:
        return "medium"
    return "low"


def derive_campaign_enrichment(campaign: dict) -> dict:
    """Derive machine-usable enrichment from sparse campaign input.

    Returns a Firestore update payload containing:
      - safe top-level backfills for fields the runtime already consumes
      - normalized intelligence strategy targets
      - a system_enrichment object for auditability and future evolution
    """
    current_strategy = campaign.get("intelligence_strategy") or {}
    if not isinstance(current_strategy, dict):
        current_strategy = {}

    strategy_plan = build_intelligence_strategy_plan(campaign)
    normalized_location, geo_precision = _normalize_location(
        _clean(campaign.get("location", "")),
        campaign.get("geo_hierarchy") if isinstance(campaign.get("geo_hierarchy"), dict) else {},
    )
    canonical_targets = _canonicalize_platform_targets(
        current_strategy.get("platform_targets", []) or [],
        strategy_plan.get("platform_targets", []) or [],
    )

    sourcing_vector = (
        _clean(campaign.get("sourcing_vector", ""))
        or _clean(strategy_plan.get("sourcing_vector", ""))
        or "B2B"
    )
    derived_persona_keywords = _derive_persona_keywords(campaign, normalized_location, sourcing_vector)
    derived_targeting_signals = _derive_targeting_signals(campaign, normalized_location, sourcing_vector)
    derived_hook = _derive_target_angle_hook(campaign, normalized_location)
    derived_advantage = _derive_unfair_advantage(campaign, normalized_location)

    merged_strategy = dict(current_strategy)
    merged_strategy["primary"] = merged_strategy.get("primary") or strategy_plan.get("primary_strategy") or "COLLOQUIAL_DISCOVERY"
    merged_strategy["secondary"] = merged_strategy.get("secondary") or strategy_plan.get("secondary_strategy") or "NONE"
    if canonical_targets:
        merged_strategy["platform_targets"] = canonical_targets
    if not merged_strategy.get("competitor_names"):
        merged_strategy["competitor_names"] = strategy_plan.get("competitor_names", [])
    if not merged_strategy.get("event_types"):
        merged_strategy["event_types"] = current_strategy.get("event_types", []) or []
    if not merged_strategy.get("decision_maker_titles"):
        merged_strategy["decision_maker_titles"] = strategy_plan.get("decision_maker_titles", [])
    if not merged_strategy.get("vocabulary_notes"):
        merged_strategy["vocabulary_notes"] = strategy_plan.get("vocabulary_notes", "")
    if not merged_strategy.get("classified_at"):
        merged_strategy["classified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    system_enrichment = {
        "normalized_location": normalized_location,
        "geo_precision": geo_precision,
        "canonical_platform_targets": canonical_targets,
        "derived_persona_keywords": derived_persona_keywords,
        "derived_targeting_signals": derived_targeting_signals,
        "derived_target_angle_hook": derived_hook,
        "derived_unfair_advantage": derived_advantage,
        "source_priorities": strategy_plan.get("source_priorities", []),
        "query_style": strategy_plan.get("query_style", "business"),
        "buyer_language_profile": merged_strategy.get("vocabulary_notes", ""),
        "enrichment_confidence": _confidence_from_inputs(campaign),
        "enrichment_version": _ENRICHMENT_VERSION,
        "enriched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    updates: dict = {
        "intelligence_strategy": merged_strategy,
        "system_enrichment": system_enrichment,
    }
    if normalized_location and (_is_broad_location(_clean(campaign.get("location", ""))) or not _clean(campaign.get("location", ""))):
        updates["location"] = normalized_location
    if not _clean(campaign.get("persona_keywords", "")) and derived_persona_keywords:
        updates["persona_keywords"] = derived_persona_keywords
    if not (campaign.get("persona_targeting_signals") or []) and derived_targeting_signals:
        updates["persona_targeting_signals"] = derived_targeting_signals
    if not _clean(campaign.get("target_angle_hook", "")) and derived_hook:
        updates["target_angle_hook"] = derived_hook
    if not _clean(campaign.get("unfair_advantage", "")) and derived_advantage:
        updates["unfair_advantage"] = derived_advantage
    if not _clean(campaign.get("target_angle_adv", "")) and derived_advantage:
        updates["target_angle_adv"] = derived_advantage
    return updates
