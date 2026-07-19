"""DEPRECATED — use ``shared.domain_platform_config`` instead.

This module remains as a thin compatibility shim so any residual imports do not
break produce. All education platforms, language packs, and sub-patterns now
live as declarative data in ``domain_platform_config.py``.

Do not add new vertical-specific logic here. Add config rows in
``domain_platform_config`` instead.
"""
from __future__ import annotations

import warnings
from typing import Any, Mapping

from shared.domain_platform_config import (  # type: ignore[import]
    FORBIDDEN_WHEN_NOT_DIRECTORY,
    entity_terms_from_profile,
    get_entity_terms,
    normalize_sub_pattern,
    platform_hosts_from_profile,
    query_uses_directory_only_language,
    resolve_platform_slice,
)

warnings.warn(
    "shared.education_profiles is deprecated; use shared.domain_platform_config",
    DeprecationWarning,
    stacklevel=2,
)

# Legacy constants retained for any external references.
LEGACY_EDUCATION_QUERY_HINTS: tuple[str, ...] = (
    "site:reddit.com/r/teachers",
    "site:coursera.org",
    "site:linkedin.com",
)
LEGACY_EDUCATION_SOURCES: tuple[str, ...] = (
    "serper_discovery",
    "reddit",
    "consumer_forum",
    "rss_feed",
    "youtube",
)

EDUCATION_SUB_PATTERNS: frozenset[str] = frozenset({
    "study_abroad",
    "coaching",
    "online_courses",
    "general_education",
})


def normalize_education_sub_pattern(value: Any) -> str:
    return normalize_sub_pattern("education", value) or "general_education"


def is_b2b_education_context(
    sourcing_vector: str | None = None,
    *,
    campaign: Mapping[str, Any] | None = None,
) -> bool:
    slice_ = resolve_platform_slice(
        "education", campaign=campaign, sourcing_vector=sourcing_vector
    )
    return bool(slice_.get("is_b2b_context") or slice_.get("is_b2b_education"))


def detect_education_sub_pattern(
    campaign: Mapping[str, Any] | None = None,
    *,
    text: str | None = None,
) -> tuple[str, float, list[str]]:
    from shared.domain_platform_config import detect_sub_pattern  # type: ignore[import]

    sub, conf, matched = detect_sub_pattern("education", campaign, text=text)
    return sub or "general_education", conf, matched


def resolve_education_profile(
    campaign: Mapping[str, Any] | None = None,
    *,
    sourcing_vector: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    slice_ = resolve_platform_slice(
        "education",
        campaign=campaign,
        sourcing_vector=sourcing_vector,
        text=text,
    )
    # Map to legacy key names used by older callers.
    return {
        "education_sub_pattern": slice_.get("sub_pattern") or "general_education",
        "education_sub_pattern_confidence": slice_.get("sub_pattern_confidence"),
        "education_matched_terms": list(slice_.get("sub_pattern_matched_terms") or []),
        "is_b2b_education": bool(slice_.get("is_b2b_context")),
        "language_pack": slice_.get("entity_language_pack"),
        "entity_language_pack": slice_.get("entity_language_pack"),
        "entity_terms": list(slice_.get("entity_terms") or []),
        "preferred_query_hints": list(slice_.get("preferred_query_hints") or []),
        "preferred_sources": list(slice_.get("preferred_sources") or []),
        "platform_hosts": list(slice_.get("platform_hosts") or []),
        "platform_mining_mode": slice_.get("platform_mining_mode"),
        "sub_pattern": slice_.get("sub_pattern"),
        "resolve_error": bool(slice_.get("resolve_error")),
    }


def education_entity_terms(
    sub_pattern: str | None = None,
    *,
    is_b2b: bool = False,
) -> list[str]:
    slice_ = resolve_platform_slice(
        "education",
        sub_pattern=sub_pattern,
        sourcing_vector="B2B" if is_b2b else "B2C",
    )
    return list(slice_.get("entity_terms") or get_entity_terms("education_student"))


def education_platform_hosts(
    sub_pattern: str | None = None,
    *,
    is_b2b: bool = False,
) -> list[str]:
    slice_ = resolve_platform_slice(
        "education",
        sub_pattern=sub_pattern,
        sourcing_vector="B2B" if is_b2b else "B2C",
    )
    return list(slice_.get("platform_hosts") or [])


def contains_forbidden_education_language(query: str) -> bool:
    return query_uses_directory_only_language(query)


def education_gemini_prompt_examples(sub_pattern: str | None = None) -> str:
    from shared.domain_platform_config import gemini_platform_examples  # type: ignore[import]

    return gemini_platform_examples(
        {
            "domain_family": "education",
            "sub_pattern": normalize_education_sub_pattern(sub_pattern),
        }
    )


# Re-export for any code that imported the forbidden phrase set.
_EDUCATION_FORBIDDEN_ENTITY_PHRASES = FORBIDDEN_WHEN_NOT_DIRECTORY
CONSUMER_DISCOVERY_ENTITY_TERMS = tuple(get_entity_terms("consumer_discovery"))
