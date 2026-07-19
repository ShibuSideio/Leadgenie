"""Education vertical profiles — SSOT for sub-patterns, platforms, language.

Used by:
  - pipeline-main ``services.domain_intelligence`` (preferred platforms / hints)
  - pipeline-main ``services.query_brain`` (platform-mining entity language)

Fail-open contract
------------------
Callers must catch exceptions and fall back to legacy education defaults
(``LEGACY_EDUCATION_QUERY_HINTS`` / ``LEGACY_EDUCATION_SOURCES``) so produce
never breaks if sub-pattern resolution fails.

Sub-patterns (detected from campaign text fields)
-------------------------------------------------
  study_abroad     — study abroad, MBBS/nursing overseas, medical education abroad
  coaching         — coaching, tuition, exam prep (JEE/NEET/etc.)
  online_courses   — online courses, skill education, certifications
  general_education — fallback when education family matches but no sub-pattern
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Canonical sub-patterns
# ---------------------------------------------------------------------------
EDUCATION_SUB_PATTERNS: frozenset[str] = frozenset({
    "study_abroad",
    "coaching",
    "online_courses",
    "general_education",
})

# Aliases normalised into canonical keys.
_SUB_PATTERN_ALIASES: dict[str, str] = {
    "medical_education": "study_abroad",
    "medical": "study_abroad",
    "mbbs": "study_abroad",
    "nursing": "study_abroad",
    "studyabroad": "study_abroad",
    "overseas_education": "study_abroad",
    "exam_prep": "coaching",
    "tuition": "coaching",
    "test_prep": "coaching",
    "skill_education": "online_courses",
    "skill_courses": "online_courses",
    "online_course": "online_courses",
    "edtech": "online_courses",
    "general": "general_education",
    "education": "general_education",
}

# ---------------------------------------------------------------------------
# Legacy education defaults (pre-fix). Kept for fail-open + reference.
# These were B2B-teacher / LMS skewed and must not be the B2C default.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Detection term packs (phrase → weight). Higher weight wins.
# Multi-word phrases preferred; matched as case-insensitive substrings.
# ---------------------------------------------------------------------------
_SUB_PATTERN_TERMS: dict[str, tuple[tuple[str, float], ...]] = {
    "study_abroad": (
        ("study abroad", 3.5),
        ("overseas education", 3.2),
        ("abroad education", 3.0),
        ("foreign university", 2.8),
        ("medical education", 2.8),
        ("mbbs abroad", 3.5),
        ("mbbs", 3.0),
        ("md abroad", 2.8),
        ("nursing abroad", 3.0),
        ("nursing", 1.6),
        ("admission abroad", 2.8),
        ("study in", 2.0),
        ("georgia", 1.4),  # common MBBS destination in campaign copy
        ("ukraine mbbs", 2.5),
        ("russia mbbs", 2.5),
        ("philippines mbbs", 2.5),
        ("kyrgyzstan", 1.5),
        ("kazakhstan", 1.4),
        ("yocket", 2.0),
        ("shiksha", 1.8),
        ("education consultant", 2.2),
        ("education counsellor", 2.2),
        ("education counselor", 2.2),
        ("visa counselling", 2.0),
        ("student visa", 2.0),
        ("ielts for abroad", 2.2),
        ("neet pg", 1.5),
        ("nmc", 1.4),
        ("fmge", 1.8),
        ("university admission", 1.8),
        ("college admission", 1.6),
    ),
    "coaching": (
        ("exam prep", 3.2),
        ("exam preparation", 3.2),
        ("test prep", 3.0),
        ("entrance exam", 2.8),
        ("coaching institute", 3.0),
        ("coaching class", 2.8),
        ("coaching centre", 2.8),
        ("coaching center", 2.8),
        ("tuition class", 2.8),
        ("tuition", 2.5),
        ("tutoring", 2.4),
        ("tutor", 2.0),
        ("jee", 2.5),
        ("neet coaching", 3.0),
        ("neet", 2.0),
        ("upsc", 2.2),
        ("ias coaching", 2.8),
        ("cat coaching", 2.8),
        ("gate coaching", 2.6),
        ("board exam", 2.0),
        ("competitive exam", 2.4),
        ("crash course", 1.8),
        ("home tuition", 2.6),
        ("private tutor", 2.4),
    ),
    "online_courses": (
        ("online course", 3.2),
        ("online courses", 3.2),
        ("skill course", 2.8),
        ("skill education", 2.8),
        ("upskilling", 2.6),
        ("certification course", 2.8),
        ("certificate course", 2.6),
        ("e-learning", 2.4),
        ("elearning", 2.4),
        ("online learning", 2.4),
        ("live class", 2.0),
        ("cohort course", 2.2),
        ("bootcamp", 2.0),
        ("mooc", 2.0),
        ("self paced", 1.8),
        ("skill development", 2.2),
        ("professional course", 2.0),
        ("digital marketing course", 2.4),
        ("coding course", 2.2),
        ("data science course", 2.4),
    ),
}

# ---------------------------------------------------------------------------
# Preferred platforms / sources per sub-pattern (B2C student & parent intent)
# LinkedIn is intentionally omitted from B2C packs — only added for B2B.
# ---------------------------------------------------------------------------
_B2C_QUERY_HINTS: dict[str, tuple[str, ...]] = {
    "study_abroad": (
        "site:reddit.com/r/IndiaEducation",
        "site:reddit.com/r/studytips",
        "site:quora.com",
        "site:youtube.com",
        "site:shiksha.com",
        "site:collegedunia.com",
        "site:facebook.com/groups",
        "site:yocket.com",
    ),
    "coaching": (
        "site:reddit.com/r/GetStudying",
        "site:reddit.com/r/JEENEETards",
        "site:quora.com",
        "site:youtube.com",
        "site:facebook.com/groups",
        "site:justdial.com",
        "site:sulekha.com",
    ),
    "online_courses": (
        "site:reddit.com/r/onlinelearning",
        "site:reddit.com/r/learnprogramming",
        "site:quora.com",
        "site:youtube.com",
        "site:facebook.com/groups",
        "site:trustpilot.com",
    ),
    "general_education": (
        "site:reddit.com",
        "site:quora.com",
        "site:youtube.com",
        "site:facebook.com/groups",
        "site:justdial.com",
    ),
}

# B2B education (institutions, university partnerships, corporate L&D buyers).
_B2B_QUERY_HINTS: dict[str, tuple[str, ...]] = {
    "study_abroad": (
        "site:linkedin.com",
        "site:reddit.com/r/highereducation",
        "site:timeshighereducation.com",
    ),
    "coaching": (
        "site:linkedin.com",
        "site:justdial.com",
        "site:clutch.co",
    ),
    "online_courses": (
        "site:linkedin.com",
        "site:g2.com",
        "site:capterra.com",
        "site:reddit.com/r/edtech",
    ),
    "general_education": (
        "site:linkedin.com",
        "site:reddit.com/r/highereducation",
        "site:clutch.co",
    ),
}

_B2C_PREFERRED_SOURCES: tuple[str, ...] = (
    "serper_discovery",
    "reddit",
    "consumer_forum",
    "youtube",
    "rss_feed",
)

_B2B_PREFERRED_SOURCES: tuple[str, ...] = (
    "serper_discovery",
    "reddit",
    "rss_feed",
    "job_posts",
    "google_reviews",
)

# Host-only seeds for platform-mining fallbacks (no site: prefix).
_B2C_PLATFORM_HOSTS: dict[str, tuple[str, ...]] = {
    "study_abroad": (
        "reddit.com",
        "quora.com",
        "youtube.com",
        "shiksha.com",
        "collegedunia.com",
        "facebook.com",
    ),
    "coaching": (
        "reddit.com",
        "quora.com",
        "youtube.com",
        "justdial.com",
        "sulekha.com",
        "facebook.com",
    ),
    "online_courses": (
        "reddit.com",
        "quora.com",
        "youtube.com",
        "trustpilot.com",
        "facebook.com",
    ),
    "general_education": (
        "reddit.com",
        "quora.com",
        "youtube.com",
        "facebook.com",
        "justdial.com",
    ),
}

_B2B_PLATFORM_HOSTS: dict[str, tuple[str, ...]] = {
    "study_abroad": ("linkedin.com", "reddit.com", "timeshighereducation.com"),
    "coaching": ("linkedin.com", "justdial.com", "clutch.co"),
    "online_courses": ("linkedin.com", "g2.com", "capterra.com", "reddit.com"),
    "general_education": ("linkedin.com", "reddit.com", "clutch.co"),
}

# ---------------------------------------------------------------------------
# Entity language packs (platform-mining query terms)
# Never use real-estate "agent broker" language for education.
# ---------------------------------------------------------------------------
# Each pack: ordered entity terms used in site: queries (first 2 primary).
_LANGUAGE_PACKS: dict[str, tuple[str, ...]] = {
    "study_abroad": (
        "consultant",
        "counsellor",
        "admission",
        "university",
        "college",
        "student",
    ),
    "coaching": (
        "coaching",
        "tutor",
        "institute",
        "tuition",
        "student",
        "parent",
    ),
    "online_courses": (
        "course",
        "program",
        "enrollment",
        "student",
        "instructor",
        "review",
    ),
    "general_education": (
        "admission",
        "student",
        "parent",
        "course",
        "college",
        "school",
    ),
    # B2B education decision-makers (institutions / recruiters / L&D).
    "education_b2b": (
        "partnership",
        "institution",
        "admissions",
        "recruiter",
        "university",
        "contact",
    ),
}

# Consumer discovery language for non-directory B2C platform mining
# (COLLOQUIAL / COMPETITOR paths on consumer verticals that are not RE).
CONSUMER_DISCOVERY_ENTITY_TERMS: tuple[str, ...] = (
    "looking for",
    "recommend",
    "help",
    "review",
)

# Phrases that must never appear in education platform-mining queries.
_EDUCATION_FORBIDDEN_ENTITY_PHRASES: frozenset[str] = frozenset({
    "agent broker",
    "broker listing",
    "property agent",
    "real estate agent",
})


def normalize_education_sub_pattern(value: Any) -> str:
    """Normalize a free-form sub-pattern label to a canonical key."""
    if value is None:
        return "general_education"
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if not text:
        return "general_education"
    text = _SUB_PATTERN_ALIASES.get(text, text)
    if text not in EDUCATION_SUB_PATTERNS:
        return "general_education"
    return text


def is_b2b_education_context(
    sourcing_vector: str | None = None,
    *,
    campaign: Mapping[str, Any] | None = None,
) -> bool:
    """True when education campaign targets institutions / recruiters (B2B).

    Default is B2C (students/parents). LinkedIn and B2B platform packs apply
    only when the vector is clearly B2B or the corpus strongly signals
    institutional buyers.
    """
    vector = (sourcing_vector or "").upper().strip()
    if vector in {"B2C", "D2C", "B2B2C"}:
        return False
    if vector == "B2B":
        return True

    # Ambiguous / missing vector: peek at campaign text for B2B cues.
    blob = _campaign_blob(campaign) if campaign else ""
    b2b_cues = (
        "b2b",
        "institution partnership",
        "university partnership",
        "corporate training",
        "l&d",
        "learning and development",
        "campus recruitment",
        "decision maker",
        "procurement",
        "school district",
        "edtech for schools",
    )
    return any(cue in blob for cue in b2b_cues)


def detect_education_sub_pattern(
    campaign: Mapping[str, Any] | None = None,
    *,
    text: str | None = None,
) -> tuple[str, float, list[str]]:
    """Detect education sub-pattern from campaign fields or raw text.

    Returns:
        (sub_pattern, confidence 0–1, matched_terms)
    """
    blob = (text or "").strip().lower()
    if not blob and campaign is not None:
        blob = _campaign_blob(campaign)
    if not blob:
        return "general_education", 0.0, []

    scores: dict[str, float] = {k: 0.0 for k in _SUB_PATTERN_TERMS}
    matched: dict[str, list[str]] = {k: [] for k in _SUB_PATTERN_TERMS}

    for pattern, terms in _SUB_PATTERN_TERMS.items():
        for phrase, weight in terms:
            if phrase in blob:
                scores[pattern] += weight
                if phrase not in matched[pattern]:
                    matched[pattern].append(phrase)

    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] <= 0:
        return "general_education", 0.25, []

    runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0.0
    # Confidence from margin + absolute score.
    margin = best[1] - runner_up
    conf = min(0.95, 0.35 + best[1] * 0.08 + margin * 0.05)
    return best[0], round(conf, 3), matched[best[0]][:8]


def resolve_education_profile(
    campaign: Mapping[str, Any] | None = None,
    *,
    sourcing_vector: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Resolve full education vertical profile (sub-pattern + platforms + language).

    Fail-open: never raises; on any error returns general_education with
    non-legacy B2C-safe defaults (not the teacher/Coursera/LinkedIn pack).
    """
    try:
        vector = sourcing_vector
        if vector is None and isinstance(campaign, Mapping):
            vector = str(campaign.get("sourcing_vector") or "")

        sub_pattern, conf, matched = detect_education_sub_pattern(
            campaign, text=text
        )
        b2b = is_b2b_education_context(vector, campaign=campaign)
        language_key = "education_b2b" if b2b else sub_pattern

        if b2b:
            hints = list(_B2B_QUERY_HINTS.get(sub_pattern, _B2B_QUERY_HINTS["general_education"]))
            sources = list(_B2B_PREFERRED_SOURCES)
            hosts = list(_B2B_PLATFORM_HOSTS.get(sub_pattern, _B2B_PLATFORM_HOSTS["general_education"]))
        else:
            hints = list(_B2C_QUERY_HINTS.get(sub_pattern, _B2C_QUERY_HINTS["general_education"]))
            sources = list(_B2C_PREFERRED_SOURCES)
            hosts = list(_B2C_PLATFORM_HOSTS.get(sub_pattern, _B2C_PLATFORM_HOSTS["general_education"]))

        entity_terms = list(_LANGUAGE_PACKS.get(language_key, _LANGUAGE_PACKS["general_education"]))

        return {
            "education_sub_pattern": sub_pattern,
            "education_sub_pattern_confidence": conf,
            "education_matched_terms": matched,
            "is_b2b_education": b2b,
            "language_pack": language_key,
            "entity_terms": entity_terms,
            "preferred_query_hints": hints,
            "preferred_sources": sources,
            "platform_hosts": hosts,
        }
    except Exception:  # noqa: BLE001 — fail-open for produce safety
        return {
            "education_sub_pattern": "general_education",
            "education_sub_pattern_confidence": 0.0,
            "education_matched_terms": [],
            "is_b2b_education": False,
            "language_pack": "general_education",
            "entity_terms": list(_LANGUAGE_PACKS["general_education"]),
            "preferred_query_hints": list(_B2C_QUERY_HINTS["general_education"]),
            "preferred_sources": list(_B2C_PREFERRED_SOURCES),
            "platform_hosts": list(_B2C_PLATFORM_HOSTS["general_education"]),
            "resolve_error": True,
        }


def education_entity_terms(
    sub_pattern: str | None = None,
    *,
    is_b2b: bool = False,
) -> list[str]:
    """Return entity language terms for platform-mining queries."""
    if is_b2b:
        return list(_LANGUAGE_PACKS["education_b2b"])
    key = normalize_education_sub_pattern(sub_pattern)
    return list(_LANGUAGE_PACKS.get(key, _LANGUAGE_PACKS["general_education"]))


def education_platform_hosts(
    sub_pattern: str | None = None,
    *,
    is_b2b: bool = False,
) -> list[str]:
    """Return host seeds for deterministic platform mining."""
    key = normalize_education_sub_pattern(sub_pattern)
    if is_b2b:
        return list(_B2B_PLATFORM_HOSTS.get(key, _B2B_PLATFORM_HOSTS["general_education"]))
    return list(_B2C_PLATFORM_HOSTS.get(key, _B2C_PLATFORM_HOSTS["general_education"]))


def contains_forbidden_education_language(query: str) -> bool:
    """True if *query* contains real-estate agent/broker phrasing."""
    q = (query or "").lower()
    if not q:
        return False
    if any(p in q for p in _EDUCATION_FORBIDDEN_ENTITY_PHRASES):
        return True
    # Standalone "agent broker" co-occurrence in short platform dorks.
    tokens = set(q.replace(":", " ").split())
    if "agent" in tokens and "broker" in tokens:
        return True
    return False


def _campaign_blob(campaign: Mapping[str, Any] | None) -> str:
    if not isinstance(campaign, Mapping):
        return ""
    parts: list[str] = []
    for key in (
        "name",
        "bio",
        "effective_bio",
        "pain_point",
        "keywords",
        "persona_keywords",
        "persona_bio",
        "persona_name",
        "campaign_focus",
        "target_angle_hook",
        "location",
    ):
        val = campaign.get(key)
        if val is None:
            continue
        if isinstance(val, (list, tuple, set)):
            parts.append(" ".join(str(x) for x in val if x))
        else:
            parts.append(str(val))
    # Persona targeting signals (list of strings)
    signals = campaign.get("persona_targeting_signals")
    if isinstance(signals, (list, tuple)):
        parts.extend(str(s) for s in signals if s)
    # Intelligence strategy vocabulary notes
    intel = campaign.get("intelligence_strategy")
    if isinstance(intel, Mapping):
        notes = intel.get("vocabulary_notes")
        if notes:
            parts.append(str(notes))
        primary = intel.get("primary")
        if primary:
            parts.append(str(primary))
    return " ".join(parts).lower()


def education_gemini_prompt_examples(sub_pattern: str | None = None) -> str:
    """Few-shot examples for Gemini platform-mining prompts (education)."""
    key = normalize_education_sub_pattern(sub_pattern)
    examples = {
        "study_abroad": (
            "  Education: site:shiksha.com consultant MBBS abroad\n"
            "  Education: site:reddit.com looking for study abroad counsellor\n"
            "  Education: site:quora.com university admission consultant\n"
            "  Education: site:youtube.com study abroad guidance student\n"
        ),
        "coaching": (
            "  Education: site:youtube.com coaching institute review\n"
            "  Education: site:quora.com best tuition for JEE\n"
            "  Education: site:reddit.com looking for NEET coaching\n"
            "  Education: site:justdial.com coaching centre contact\n"
        ),
        "online_courses": (
            "  Education: site:reddit.com recommend online course\n"
            "  Education: site:quora.com best skill course review\n"
            "  Education: site:youtube.com course review student\n"
            "  Education: site:trustpilot.com online learning program\n"
        ),
        "general_education": (
            "  Education: site:reddit.com looking for admission help\n"
            "  Education: site:quora.com college course student parent\n"
            "  Education: site:youtube.com school admission guidance\n"
        ),
    }
    return examples.get(key, examples["general_education"])
