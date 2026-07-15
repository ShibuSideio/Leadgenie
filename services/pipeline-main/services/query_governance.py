"""Query governance layer for production-safe Serper query portfolios."""
from __future__ import annotations

import re
from typing import Any

_NEGATIVE_INTENT_TERMS = frozenset(
    {
        "scam",
        "fake",
        "fraud",
        "hidden fees",
        "misleading",
        "unreliable",
        "bad experience",
        "expensive",
        "charging too much",
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
    "glassdoor": "glassdoor.com",
    "linkedin": "linkedin.com",
}
_COMMUNITY_HINTS = ("reddit.com", "quora.com", "forum.", "community.")
_REVIEW_HINTS = ("trustpilot.com", "g2.com", "capterra.com", "yelp.com", "google.com/maps")


def _normalize_space(value: str) -> str:
    return re.sub(r"\s{2,}", " ", (value or "").strip())


def _main_location_token(location: str) -> str:
    if not location:
        return ""
    parts = [part.strip() for part in re.split(r"[,/|]+", location) if part and part.strip()]
    for part in parts:
        lowered = part.lower()
        if lowered not in {"all", "global", "worldwide", "asia"}:
            return part
    return ""


def _extract_domain(target: str) -> str:
    raw = (target or "").strip().lower().replace("https://", "").replace("http://", "")
    raw = raw.replace("www.", "").split("/")[0]
    if "." in raw and " " not in raw:
        return raw
    for brand, domain in _PLATFORM_DOMAIN_MAP.items():
        if brand in raw:
            return domain
    return ""


def _positive_site_targets(query: str) -> list[str]:
    return [
        m.group(1).lower()
        for m in re.finditer(r"(?<![-\w])site:([^\s)]+)", query or "")
    ]


def _is_negative_intent(query: str) -> bool:
    lowered = (query or "").lower()
    return any(term in lowered for term in _NEGATIVE_INTENT_TERMS)


def _is_platform_query(query: str) -> bool:
    return bool(_positive_site_targets(query))


def _is_community_query(query: str) -> bool:
    lowered = (query or "").lower()
    return any(hint in lowered for hint in _COMMUNITY_HINTS)


def _is_review_query(query: str) -> bool:
    lowered = (query or "").lower()
    return any(hint in lowered for hint in _REVIEW_HINTS)


def _cap_blacklist_sites(query: str, max_sites: int = 6) -> tuple[str, int]:
    tokens = _normalize_space(query).split(" ")
    kept: list[str] = []
    site_seen = 0
    trimmed = 0
    for tok in tokens:
        if tok.startswith("-site:"):
            site_seen += 1
            if site_seen > max_sites:
                trimmed += 1
                continue
        kept.append(tok)
    return _normalize_space(" ".join(kept)), trimmed


def _build_platform_templates(campaign: dict[str, Any], needed: int, location_token: str) -> list[str]:
    strategy = campaign.get("intelligence_strategy") or {}
    platform_targets = strategy.get("platform_targets") or []
    if not isinstance(platform_targets, list):
        platform_targets = []

    domains: list[str] = []
    for target in platform_targets:
        domain = _extract_domain(str(target))
        if domain and domain not in domains:
            domains.append(domain)

    keywords_raw = (campaign.get("persona_keywords") or campaign.get("keywords") or "").strip()
    primary_hint = ""
    if keywords_raw:
        primary_hint = keywords_raw.split(",")[0].strip().strip('"')

    generated: list[str] = []
    for domain in domains:
        base = f'site:{domain} ("agent" OR "broker" OR "contact" OR "listing")'
        if location_token:
            base += f' "{location_token}"'
        if primary_hint:
            base += f' "{primary_hint}"'
        base += " -jobs -careers -wiki"
        generated.append(_normalize_space(base))
        if len(generated) >= needed:
            break
    return generated


def govern_query_portfolio(
    candidate_queries: list[str],
    campaign: dict[str, Any],
    sourcing_vector: str,
    location: str,
) -> dict[str, Any]:
    """Govern candidate queries for diversity, precision, and cost safety."""
    normalized: list[str] = []
    seen: set[str] = set()
    blacklist_trimmed = 0
    for query in candidate_queries or []:
        query = _normalize_space(query)
        if not query:
            continue
        query, trimmed = _cap_blacklist_sites(query, max_sites=6)
        blacklist_trimmed += trimmed
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(query)

    if not normalized:
        return {
            "queries": [],
            "stats": {
                "original_count": len(candidate_queries or []),
                "final_count": 0,
                "negative_dropped": 0,
                "platform_injected": 0,
                "blacklist_sites_trimmed": blacklist_trimmed,
            },
        }

    primary_strategy = ""
    strategy_info = campaign.get("intelligence_strategy") or {}
    if isinstance(strategy_info, dict):
        primary_strategy = str(strategy_info.get("primary") or "").upper().strip()

    portfolio_size = min(12, max(6, len(normalized)))
    vector_upper = (sourcing_vector or "").upper().strip()
    if primary_strategy == "PLATFORM_MINING":
        negative_cap_ratio = 0.30
    elif vector_upper in {"B2C", "D2C", "B2B2C"}:
        negative_cap_ratio = 0.30
    else:
        negative_cap_ratio = 0.40
    max_negative = max(1, int(portfolio_size * negative_cap_ratio))

    negatives: list[str] = []
    non_negatives: list[str] = []
    for query in normalized:
        if _is_negative_intent(query):
            negatives.append(query)
        else:
            non_negatives.append(query)

    selected = list(non_negatives)
    allowed_negatives = negatives[:max_negative]
    selected.extend(allowed_negatives)
    negative_dropped = max(0, len(negatives) - len(allowed_negatives))

    location_token = _main_location_token(location)

    platform_injected = 0
    if primary_strategy == "PLATFORM_MINING":
        platform_count = sum(1 for query in selected if _is_platform_query(query))
        if platform_count < 2:
            needed = 2 - platform_count
            for query in _build_platform_templates(campaign, needed=needed, location_token=location_token):
                if query.lower() not in {q.lower() for q in selected}:
                    selected.append(query)
                    platform_injected += 1

    # Ensure minimal source diversity for non-platform strategies.
    if primary_strategy != "PLATFORM_MINING":
        has_community = any(_is_community_query(q) for q in selected)
        has_review = any(_is_review_query(q) for q in selected)
        if not has_community:
            selected.extend([q for q in normalized if _is_community_query(q)][:1])
        if not has_review:
            selected.extend([q for q in normalized if _is_review_query(q)][:1])

    # Final stable dedup + cap.
    final_queries: list[str] = []
    final_seen: set[str] = set()
    for query in selected:
        key = query.lower()
        if key in final_seen:
            continue
        final_seen.add(key)
        final_queries.append(query)
        if len(final_queries) >= portfolio_size:
            break

    return {
        "queries": final_queries,
        "stats": {
            "original_count": len(candidate_queries or []),
            "final_count": len(final_queries),
            "negative_dropped": negative_dropped,
            "platform_injected": platform_injected,
            "blacklist_sites_trimmed": blacklist_trimmed,
            "primary_strategy": primary_strategy or "UNKNOWN",
            "sourcing_vector": (sourcing_vector or "").upper().strip() or "UNKNOWN",
        },
    }
