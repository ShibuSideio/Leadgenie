"""Query governance layer for production-safe Serper query portfolios.

V26.8.1 — negative -site: caps, low-liquidity awareness, forced PLATFORM_MINING.
"""
from __future__ import annotations

import hashlib
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

# Prefer keeping high-value noise exclusions when we must trim -site: lists.
_HIGH_VALUE_SITE_EXCLUSIONS = (
    "upwork.com",
    "fiverr.com",
    "freelancer.com",
    "behance.net",
    "zoominfo.com",
    "amazon.com",
    "ibm.com",
    "wikipedia.org",
    "wiki",
)

# Never exclude these via -site: when the query positively targets them.
_PROTECTED_POSITIVE_SITES = frozenset(
    {
        "reddit.com",
        "quora.com",
        "linkedin.com",
        "facebook.com",
        "trustpilot.com",
        "g2.com",
        "capterra.com",
        "yelp.com",
        "bayut.com",
        "propertyfinder.com",
        "dubizzle.com",
        "olx.com",
    }
)

_DEFAULT_MAX_SITE_EXCLUSIONS = 6
_LOW_LIQUIDITY_MAX_SITE_EXCLUSIONS = 4
_PLATFORM_MINING_MIN_QUERIES = 3
_PLATFORM_MINING_TARGET_QUERIES = 4


def _normalize_space(value: str) -> str:
    return re.sub(r"\s{2,}", " ", (value or "").strip())


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = _normalize_space(item)
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


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
        m.group(1).lower().rstrip(")/,\"'")
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


def _site_exclusion_priority(token: str) -> int:
    """Lower score = keep first when trimming. High-value noise keeps better rank."""
    domain = token.lower().replace("-site:", "").replace("www.", "")
    for i, high in enumerate(_HIGH_VALUE_SITE_EXCLUSIONS):
        if high in domain:
            return i
    # Longer/obscure personal domains are low value — drop first
    return 100 + min(len(domain), 50)


def _deconflict_site_exclusions(query: str) -> tuple[str, int]:
    """Remove -site:X when the query already has a positive site:X (or parent).

    Never add -site:reddit.com / -site:quora.com against deliberate platform
    mining of those communities.
    """
    positives = _positive_site_targets(query)
    if not positives and not any(
        p in (query or "").lower() for p in _PROTECTED_POSITIVE_SITES
    ):
        # Still strip protected sites if they appear as -site: without positive?
        # Only strip when conflicting with a positive target, OR when protected
        # site is a positive target in the query body.
        pass

    tokens = _normalize_space(query).split(" ")
    kept: list[str] = []
    dropped = 0
    for tok in tokens:
        if not tok.startswith("-site:"):
            kept.append(tok)
            continue
        excl = tok[6:].lower().replace("www.", "")
        conflict = False
        for pos in positives:
            pos_clean = pos.replace("www.", "")
            if excl == pos_clean or excl.endswith("." + pos_clean) or pos_clean.endswith("." + excl) or excl in pos_clean or pos_clean in excl:
                conflict = True
                break
            # Protected community sites: never -site: when positively targeted
            for protected in _PROTECTED_POSITIVE_SITES:
                if protected in pos_clean and protected in excl:
                    conflict = True
                    break
            if conflict:
                break
        if conflict:
            dropped += 1
            continue
        kept.append(tok)
    return _normalize_space(" ".join(kept)), dropped


def _cap_blacklist_sites(query: str, max_sites: int = 6) -> tuple[str, int]:
    """Cap -site: exclusions with priority-aware trimming.

    High-value noise domains (freelancer marketplaces, wiki, zoominfo) are
    retained preferentially. Low-value / long-tail competitor exclusions drop
    first so queries stay non-sterile.
    """
    query, deconflicted = _deconflict_site_exclusions(query)
    tokens = _normalize_space(query).split(" ")
    body: list[str] = []
    site_tokens: list[str] = []
    for tok in tokens:
        if tok.startswith("-site:"):
            site_tokens.append(tok)
        else:
            body.append(tok)

    trimmed = deconflicted
    if len(site_tokens) > max_sites:
        ranked = sorted(site_tokens, key=_site_exclusion_priority)
        kept_sites = ranked[:max_sites]
        # Preserve original relative order of kept sites for readability
        kept_set = set(kept_sites)
        ordered_kept = [t for t in site_tokens if t in kept_set]
        trimmed += len(site_tokens) - len(ordered_kept)
        site_tokens = ordered_kept

    # Platform site: queries get a lighter negative tail — drop non-high-value
    # exclusions beyond 3 when the query is deliberately platform-targeted.
    if _is_platform_query(query) and len(site_tokens) > 3:
        high = [t for t in site_tokens if _site_exclusion_priority(t) < 100]
        low = [t for t in site_tokens if _site_exclusion_priority(t) >= 100]
        keep_low = max(0, 3 - len(high))
        new_sites = high + low[:keep_low]
        if len(new_sites) < len(site_tokens):
            trimmed += len(site_tokens) - len(new_sites)
            site_tokens = new_sites

    return _normalize_space(" ".join(body + site_tokens)), trimmed


def _is_low_liquidity(domain_profile: dict[str, Any] | None) -> bool:
    if not isinstance(domain_profile, dict):
        return False
    if domain_profile.get("low_liquidity_market"):
        return True
    return str(domain_profile.get("liquidity_level") or "").lower() == "low"


def _primary_strategy(campaign: dict[str, Any]) -> str:
    strategy_info = campaign.get("intelligence_strategy") or {}
    if isinstance(strategy_info, dict):
        return str(strategy_info.get("primary") or "").upper().strip()
    return ""


def _domain_wants_platform_mining(domain_profile: dict[str, Any] | None) -> bool:
    if not isinstance(domain_profile, dict):
        return False
    family = str(domain_profile.get("domain_family") or "").strip().lower()
    return family in {
        "real_estate",
        "manufacturing",
        "construction",
        "healthcare",
        "professional_services",
        "hospitality",
        "marketing_agency",
    }


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

    # Domain-family defaults for real estate when campaign lacks platform_targets
    if not domains:
        profile = campaign.get("system_domain_profile") or {}
        family = str(profile.get("domain_family") or "").lower() if isinstance(profile, dict) else ""
        if family == "real_estate" or "property" in str(campaign.get("keywords") or "").lower():
            domains = ["bayut.com", "propertyfinder.com", "dubizzle.com", "olx.com"]

    keywords_raw = (campaign.get("persona_keywords") or campaign.get("keywords") or "").strip()
    primary_hint = ""
    if keywords_raw:
        primary_hint = keywords_raw.split(",")[0].strip().strip('"')

    generated: list[str] = []
    for domain in domains:
        # Light negatives only — heavy -site: lists sterilize platform dorks.
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
    domain_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Govern candidate queries for diversity, precision, and cost safety.

    Args:
        candidate_queries: Raw queries from query_brain / domain shaping.
        campaign: Full campaign dict (strategy, keywords, platform_targets).
        sourcing_vector: B2B / B2C / …
        location: Campaign location string.
        domain_profile: Optional system_domain_profile for liquidity / family.
    """
    # Prefer explicit domain_profile; fall back to campaign snapshot.
    if domain_profile is None and isinstance(campaign.get("system_domain_profile"), dict):
        domain_profile = campaign.get("system_domain_profile")

    low_liq = _is_low_liquidity(domain_profile)
    max_sites = (
        _LOW_LIQUIDITY_MAX_SITE_EXCLUSIONS if low_liq else _DEFAULT_MAX_SITE_EXCLUSIONS
    )

    normalized: list[str] = []
    seen: set[str] = set()
    blacklist_trimmed = 0
    for query in candidate_queries or []:
        query = _normalize_space(query)
        if not query:
            continue
        query, trimmed = _cap_blacklist_sites(query, max_sites=max_sites)
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
                "platform_count": 0,
                "blacklist_sites_trimmed": blacklist_trimmed,
                "max_site_exclusions": max_sites,
                "trim_reason": "empty_after_normalize",
                "low_liquidity": low_liq,
            },
        }

    primary_strategy = _primary_strategy(campaign)
    force_platform = (
        primary_strategy == "PLATFORM_MINING"
        or _domain_wants_platform_mining(domain_profile)
    )

    portfolio_size = min(12, max(6, len(normalized)))
    vector_upper = (sourcing_vector or "").upper().strip()
    if primary_strategy == "PLATFORM_MINING":
        negative_cap_ratio = 0.25
    elif vector_upper in {"B2C", "D2C", "B2B2C"}:
        negative_cap_ratio = 0.30
    else:
        negative_cap_ratio = 0.35
    if low_liq:
        negative_cap_ratio = min(negative_cap_ratio, 0.25)
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
    if force_platform:
        platform_count = sum(1 for query in selected if _is_platform_query(query))
        min_needed = _PLATFORM_MINING_TARGET_QUERIES if primary_strategy == "PLATFORM_MINING" else _PLATFORM_MINING_MIN_QUERIES
        if platform_count < min_needed:
            needed = min_needed - platform_count
            for query in _build_platform_templates(
                campaign, needed=needed, location_token=location_token
            ):
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

    # Prioritize platform site: queries at the front of execution order.
    platform_first = [q for q in selected if _is_platform_query(q)]
    others = [q for q in selected if not _is_platform_query(q)]
    ordered = platform_first + others

    # Final stable dedup + cap.
    final_queries: list[str] = []
    final_seen: set[str] = set()
    for query in ordered:
        key = query.lower()
        if key in final_seen:
            continue
        final_seen.add(key)
        final_queries.append(query)
        if len(final_queries) >= portfolio_size:
            break

    platform_count_final = sum(1 for q in final_queries if _is_platform_query(q))
    trim_reason = "ok"
    if blacklist_trimmed:
        trim_reason = "low_liquidity_cap" if low_liq else "site_exclusion_cap"

    return {
        "queries": final_queries,
        "stats": {
            "original_count": len(candidate_queries or []),
            "final_count": len(final_queries),
            "negative_dropped": negative_dropped,
            "platform_injected": platform_injected,
            "platform_count": platform_count_final,
            "blacklist_sites_trimmed": blacklist_trimmed,
            "max_site_exclusions": max_sites,
            "trim_reason": trim_reason,
            "low_liquidity": low_liq,
            "primary_strategy": primary_strategy or "UNKNOWN",
            "sourcing_vector": (sourcing_vector or "").upper().strip() or "UNKNOWN",
            "force_platform_mining": force_platform,
        },
    }


def query_signature(query: str) -> str:
    normalized = _normalize_space(query).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def filter_queries_against_memory(
    candidate_queries: list[str],
    prior_signatures: list[str],
    keep_minimum: int = 2,
) -> dict[str, Any]:
    prior = {str(sig).strip() for sig in (prior_signatures or []) if str(sig).strip()}
    kept: list[str] = []
    dropped = 0
    for query in candidate_queries or []:
        sig = query_signature(query)
        if sig in prior:
            dropped += 1
            continue
        kept.append(query)
    if not kept and candidate_queries:
        kept = list(candidate_queries[:max(1, keep_minimum)])
    return {
        "queries": kept,
        "dropped": dropped,
        "kept": len(kept),
    }


def build_exhaustion_escalation_queries(
    campaign: dict[str, Any],
    location: str,
    level: int,
) -> list[str]:
    if level <= 0:
        return []

    strategy = campaign.get("intelligence_strategy") or {}
    primary = str(strategy.get("primary") or "").upper().strip()
    location_token = _main_location_token(location)
    location_phrase = f' "{location_token}"' if location_token else ""
    keywords_raw = (campaign.get("persona_keywords") or campaign.get("keywords") or "").strip()
    keyword_hint = ""
    if keywords_raw:
        keyword_hint = keywords_raw.split(",")[0].strip().strip('"')
    keyword_phrase = f' "{keyword_hint}"' if keyword_hint else ""

    templates: list[str] = []
    if primary == "PLATFORM_MINING":
        templates.extend(_build_platform_templates(campaign, needed=4, location_token=location_token))
        templates.extend(
            [
                _normalize_space(f'site:google.com/maps ("agent" OR "broker"){location_phrase}{keyword_phrase}'),
                _normalize_space(f'site:facebook.com ("property agent" OR "real estate broker"){location_phrase}{keyword_phrase}'),
            ]
        )
    else:
        templates.extend(
            [
                _normalize_space(f'site:reddit.com/r/smallbusiness ("need help" OR "recommend"){location_phrase}{keyword_phrase}'),
                _normalize_space(f'site:quora.com ("looking for" OR "alternatives"){location_phrase}{keyword_phrase}'),
                _normalize_space(f'site:trustpilot.com ("review" OR "experience"){location_phrase}{keyword_phrase}'),
            ]
        )

    if level >= 2:
        templates.extend(
            [
                _normalize_space(f'site:linkedin.com/posts ("evaluating" OR "looking for"){location_phrase}{keyword_phrase}'),
                _normalize_space(f'site:youtube.com ("review" OR "comparison"){location_phrase}{keyword_phrase}'),
            ]
        )
    if level >= 3:
        templates.extend(
            [
                _normalize_space(f'site:news.google.com ("announced" OR "expansion"){location_phrase}{keyword_phrase}'),
                _normalize_space(f'site:indiamart.com ("supplier" OR "vendor"){location_phrase}{keyword_phrase}'),
            ]
        )

    return _dedupe_preserve([q for q in templates if q])[:8]
