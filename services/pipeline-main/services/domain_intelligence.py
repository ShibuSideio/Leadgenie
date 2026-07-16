"""Domain intelligence helpers for campaign-aware query and gate behavior."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "real_estate": ("property", "real estate", "broker", "villa", "apartment", "commercial", "rent", "lease"),
    "saas": ("saas", "crm", "automation", "workflow", "software", "platform", "api", "subscription"),
    "manufacturing": ("factory", "manufacturing", "industrial", "machine", "equipment", "supplier", "procurement"),
    "healthcare": ("clinic", "hospital", "medical", "patient", "doctor", "healthcare", "diagnostic"),
    "education": ("school", "college", "course", "admission", "education", "study", "tuition"),
    "finance": ("finance", "loan", "insurance", "mortgage", "credit", "bank", "wealth"),
}

_BLOCKED_SUBREDDITS_BY_DOMAIN: dict[str, set[str]] = {
    "real_estate": {"frugal", "buyitforlife"},
    "saas": {"buyitforlife"},
}

_PREFERRED_QUERY_HINTS: dict[str, tuple[str, ...]] = {
    "real_estate": ("site:propertyfinder", "site:bayut", "site:dubizzle", "site:olx", "site:reddit.com/r/oman", "site:reddit.com/r/expats"),
    "saas": ("site:reddit.com/r/smallbusiness", "site:g2.com", "site:capterra.com", "site:trustpilot.com"),
    "manufacturing": ("site:indiamart.com", "site:thomasnet.com", "site:linkedin.com/posts"),
    "healthcare": ("site:reddit.com/r/healthcare", "site:practo.com", "site:google.com/maps"),
}

_LOW_LIQUIDITY_MARKERS = {
    "oman",
    "qatar",
    "bahrain",
    "kuwait",
    "nepal",
    "bhutan",
}


def infer_domain_profile(campaign: dict[str, Any]) -> dict[str, Any]:
    corpus = " ".join(
        [
            str(campaign.get("name") or ""),
            str(campaign.get("bio") or ""),
            str(campaign.get("effective_bio") or ""),
            str(campaign.get("keywords") or ""),
            str(campaign.get("persona_keywords") or ""),
            str(campaign.get("pain_point") or ""),
            str(campaign.get("location") or ""),
        ]
    ).lower()
    family = "general_services"
    best_hits = 0
    for key, terms in _DOMAIN_KEYWORDS.items():
        hits = sum(1 for term in terms if term in corpus)
        if hits > best_hits:
            best_hits = hits
            family = key

    location = str(campaign.get("location") or "").lower()
    low_liquidity = any(marker in location for marker in _LOW_LIQUIDITY_MARKERS)
    blocked_subreddits = sorted(_BLOCKED_SUBREDDITS_BY_DOMAIN.get(family, set()))
    preferred_hints = list(_PREFERRED_QUERY_HINTS.get(family, ()))
    confidence = min(1.0, 0.35 + (0.12 * best_hits))

    return {
        "version": "domain-v1",
        "domain_family": family,
        "confidence": round(confidence, 3),
        "blocked_subreddits": blocked_subreddits,
        "preferred_query_hints": preferred_hints,
        "low_liquidity_market": low_liquidity,
    }


def apply_domain_query_profile(queries: list[str], domain_profile: dict[str, Any]) -> dict[str, Any]:
    blocked = {s.lower() for s in (domain_profile.get("blocked_subreddits") or [])}
    hints = [str(h).lower() for h in (domain_profile.get("preferred_query_hints") or []) if str(h).strip()]
    if not queries:
        return {"queries": [], "dropped": 0, "injected": 0}

    kept: list[str] = []
    dropped = 0
    seen: set[str] = set()
    for query in queries:
        lowered = (query or "").lower()
        blocked_hit = False
        for subreddit in blocked:
            if f"/r/{subreddit}" in lowered:
                blocked_hit = True
                break
        if blocked_hit:
            dropped += 1
            continue
        key = lowered.strip()
        if key in seen:
            continue
        seen.add(key)
        kept.append(query)

    injected = 0
    if hints:
        has_preferred = any(any(hint in (q or "").lower() for hint in hints) for q in kept)
        if not has_preferred:
            for query in queries:
                lowered = (query or "").lower()
                if any(hint in lowered for hint in hints):
                    if lowered not in seen:
                        seen.add(lowered)
                        kept.insert(0, query)
                        injected += 1
                    break
    return {"queries": kept, "dropped": dropped, "injected": injected}


def filter_tiered_urls_by_domain(tiered: dict[str, list[str]], domain_profile: dict[str, Any]) -> dict[str, Any]:
    blocked = {s.lower() for s in (domain_profile.get("blocked_subreddits") or [])}
    if not blocked:
        return {"tiered": tiered, "dropped": 0}

    dropped = 0
    filtered: dict[str, list[str]] = {"High": [], "Medium": [], "Low": list(tiered.get("Low", []))}
    for tier in ("High", "Medium"):
        for url in tiered.get(tier, []) or []:
            try:
                parsed = urlparse(url)
                parts = [p.lower() for p in parsed.path.split("/") if p]
                subreddit = parts[1] if len(parts) >= 2 and parts[0] == "r" else ""
                if subreddit and subreddit in blocked:
                    dropped += 1
                    continue
            except Exception:
                pass
            filtered[tier].append(url)
    return {"tiered": filtered, "dropped": dropped}

