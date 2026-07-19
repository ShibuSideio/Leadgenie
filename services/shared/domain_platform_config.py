"""Domain platform + entity-language configuration (declarative SSOT).

Enterprise rule
---------------
**No per-family Python modules.**  Adding a vertical or sub-pattern is a
data change in this file (or an equivalent config document), not a new
``*_profiles.py``.

Consumed by:
  - ``pipeline-main.services.domain_intelligence`` — profile contract fields
  - ``pipeline-main.services.query_brain`` — platform-mining language + hosts
  - ``shared.intent_orchestrator`` (V27) — platform_targets / query_hints

Profile contract fields populated from this config
--------------------------------------------------
  - ``sub_pattern`` (optional)
  - ``preferred_sources`` / ``preferred_query_hints`` / ``platform_hosts``
  - ``entity_language_pack``  — pack key (e.g. ``directory_listing``)
  - ``entity_terms``          — resolved terms from the pack
  - ``platform_mining_mode``  — ``consumer`` | ``professional`` | ``directory`` | ``none``

Fail-open: resolvers never raise; missing data → ``neutral_safe`` pack + mode
``consumer`` for B2C-ish families, never real-estate ``agent broker`` as a
global default.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# Platform mining modes
# ---------------------------------------------------------------------------
PLATFORM_MINING_MODES: frozenset[str] = frozenset({
    "consumer",       # student/parent/end-buyer community discovery
    "professional",   # B2B / institutional / LinkedIn-heavy
    "directory",      # portal/aggregator listing entities (agents, clinics, …)
    "none",           # do not force platform mining
})

DEFAULT_PLATFORM_MINING_MODE = "none"
SAFE_FALLBACK_LANGUAGE_PACK = "neutral_safe"

# ---------------------------------------------------------------------------
# Reusable entity language packs (data only)
# Domain families / sub-patterns *select* a pack; they do not invent terms.
# ---------------------------------------------------------------------------
ENTITY_LANGUAGE_PACKS: dict[str, tuple[str, ...]] = {
    # Neutral fail-open — never agent/broker.
    "neutral_safe": (
        "looking for",
        "recommend",
        "review",
        "contact",
        "profile",
    ),
    # Consumer pain / colloquial discovery
    "consumer_discovery": (
        "looking for",
        "recommend",
        "help",
        "review",
    ),
    # Real-estate style portal listings
    "directory_listing": (
        "agent",
        "broker",
        "listing",
        "contact",
    ),
    # B2B professional services
    "professional_service": (
        "consultant",
        "firm",
        "services",
        "contact",
    ),
    # Manufacturing / supplier directories
    "supplier_directory": (
        "supplier",
        "manufacturer",
        "RFQ",
        "contact",
        "equipment",
    ),
    # Construction / trade
    "construction_trade": (
        "contractor",
        "project",
        "tender",
        "contact",
    ),
    # Healthcare consumer
    "healthcare_consumer": (
        "clinic",
        "doctor",
        "patient",
        "review",
    ),
    # Hospitality
    "hospitality_review": (
        "hotel",
        "restaurant",
        "guest",
        "review",
    ),
    # SaaS / software review
    "saas_review": (
        "review",
        "alternative",
        "pricing",
        "integration",
    ),
    # Local consumer services
    "local_service": (
        "review",
        "contact",
        "near me",
        "service",
    ),
    # Education — student / parent intent
    "education_student": (
        "admission",
        "student",
        "parent",
        "course",
        "college",
        "school",
    ),
    "education_study_abroad": (
        "consultant",
        "counsellor",
        "admission",
        "university",
        "college",
        "student",
    ),
    "education_coaching": (
        "coaching",
        "tutor",
        "institute",
        "tuition",
        "student",
        "parent",
    ),
    "education_online": (
        "course",
        "program",
        "enrollment",
        "student",
        "instructor",
        "review",
    ),
    # Education — institutional / B2B
    "education_institution": (
        "partnership",
        "institution",
        "admissions",
        "recruiter",
        "university",
        "contact",
    ),
}

# Host substrings → optional pack override (review directories, social).
# Applied only when the profile pack is neutral / consumer_discovery so we
# do not clobber domain-specific packs (e.g. directory_listing on bayut).
HOST_LANGUAGE_OVERRIDES: dict[str, str] = {
    "g2.com": "saas_review",
    "capterra.com": "saas_review",
    "trustpilot.com": "consumer_discovery",
    "linkedin.com": "professional_service",
    "reddit.com": "consumer_discovery",
    "indiamart.com": "supplier_directory",
    "thomasnet.com": "supplier_directory",
}

# Phrases that are real-estate portal language — blocked when pack forbids them.
FORBIDDEN_WHEN_NOT_DIRECTORY: frozenset[str] = frozenset({
    "agent broker",
    "broker listing",
    "property agent",
    "real estate agent",
})

# ---------------------------------------------------------------------------
# Family-level platform defaults (no sub-pattern)
# ---------------------------------------------------------------------------
# Each entry:
#   preferred_sources, preferred_query_hints, platform_hosts,
#   entity_language_pack, platform_mining_mode
# Optional b2b_* keys override when sourcing_vector is B2B.

_FamilyCfg = dict[str, Any]

FAMILY_PLATFORM_CONFIG: dict[str, _FamilyCfg] = {
    "real_estate": {
        "preferred_sources": (
            "classified_listings",
            "serper_discovery",
            "consumer_forum",
            "reddit",
            "google_reviews",
        ),
        "preferred_query_hints": (
            "site:propertyfinder",
            "site:bayut",
            "site:dubizzle",
            "site:olx",
            "site:reddit.com/r/oman",
            "site:reddit.com/r/expats",
        ),
        "platform_hosts": (
            "bayut.com",
            "propertyfinder.com",
            "dubizzle.com",
            "olx.com",
        ),
        "entity_language_pack": "directory_listing",
        "platform_mining_mode": "directory",
    },
    "saas": {
        "preferred_sources": (
            "reddit",
            "hackernews",
            "serper_discovery",
            "google_reviews",
            "rss_feed",
        ),
        "preferred_query_hints": (
            "site:reddit.com/r/smallbusiness",
            "site:g2.com",
            "site:capterra.com",
            "site:trustpilot.com",
            "site:news.ycombinator.com",
        ),
        "platform_hosts": ("g2.com", "capterra.com", "trustpilot.com", "linkedin.com"),
        "entity_language_pack": "saas_review",
        "platform_mining_mode": "professional",
    },
    "manufacturing": {
        "preferred_sources": (
            "serper_discovery",
            "job_posts",
            "rss_feed",
            "reddit",
            "google_reviews",
        ),
        "preferred_query_hints": (
            "site:indiamart.com",
            "site:thomasnet.com",
            "site:linkedin.com/posts",
        ),
        "platform_hosts": ("indiamart.com", "thomasnet.com", "alibaba.com"),
        "entity_language_pack": "supplier_directory",
        "platform_mining_mode": "directory",
    },
    "professional_services": {
        "preferred_sources": (
            "serper_discovery",
            "reddit",
            "google_reviews",
            "rss_feed",
            "job_posts",
        ),
        "preferred_query_hints": (
            "site:linkedin.com",
            "site:clutch.co",
            "site:reddit.com/r/consulting",
        ),
        "platform_hosts": ("linkedin.com", "clutch.co", "justdial.com"),
        "entity_language_pack": "professional_service",
        "platform_mining_mode": "professional",
    },
    "healthcare": {
        "preferred_sources": (
            "google_reviews",
            "serper_discovery",
            "consumer_forum",
            "reddit",
            "rss_feed",
        ),
        "preferred_query_hints": (
            "site:reddit.com/r/healthcare",
            "site:practo.com",
            "site:google.com/maps",
        ),
        "platform_hosts": ("practo.com", "justdial.com", "sulekha.com"),
        "entity_language_pack": "healthcare_consumer",
        "platform_mining_mode": "directory",
    },
    "education": {
        # Family-level fallback = general_education B2C (sub-patterns override).
        "preferred_sources": (
            "serper_discovery",
            "reddit",
            "consumer_forum",
            "youtube",
            "rss_feed",
        ),
        "preferred_query_hints": (
            "site:reddit.com",
            "site:quora.com",
            "site:youtube.com",
            "site:facebook.com/groups",
            "site:justdial.com",
        ),
        "platform_hosts": (
            "reddit.com",
            "quora.com",
            "youtube.com",
            "facebook.com",
            "justdial.com",
        ),
        "entity_language_pack": "education_student",
        "platform_mining_mode": "consumer",
        "b2b_entity_language_pack": "education_institution",
        "b2b_platform_mining_mode": "professional",
        "b2b_preferred_query_hints": (
            "site:linkedin.com",
            "site:reddit.com/r/highereducation",
            "site:clutch.co",
        ),
        "b2b_platform_hosts": ("linkedin.com", "reddit.com", "clutch.co"),
        "b2b_preferred_sources": (
            "serper_discovery",
            "reddit",
            "rss_feed",
            "job_posts",
            "google_reviews",
        ),
    },
    "finance": {
        "preferred_sources": (
            "serper_discovery",
            "reddit",
            "google_reviews",
            "rss_feed",
            "job_posts",
        ),
        "preferred_query_hints": (
            "site:reddit.com/r/personalfinance",
            "site:linkedin.com",
            "site:trustpilot.com",
        ),
        "platform_hosts": ("reddit.com", "trustpilot.com", "linkedin.com"),
        "entity_language_pack": "professional_service",
        "platform_mining_mode": "professional",
    },
    "ecommerce": {
        "preferred_sources": (
            "consumer_forum",
            "reddit",
            "serper_discovery",
            "youtube",
            "google_reviews",
        ),
        "preferred_query_hints": (
            "site:reddit.com/r/ecommerce",
            "site:trustpilot.com",
            "site:amazon.com",
        ),
        "platform_hosts": ("amazon.com", "trustpilot.com", "reddit.com"),
        "entity_language_pack": "consumer_discovery",
        "platform_mining_mode": "consumer",
    },
    "hospitality": {
        "preferred_sources": (
            "google_reviews",
            "serper_discovery",
            "reddit",
            "consumer_forum",
            "youtube",
        ),
        "preferred_query_hints": (
            "site:tripadvisor.com",
            "site:google.com/maps",
            "site:reddit.com",
        ),
        "platform_hosts": ("tripadvisor.com", "yelp.com", "trustpilot.com"),
        "entity_language_pack": "hospitality_review",
        "platform_mining_mode": "directory",
    },
    "logistics": {
        "preferred_sources": (
            "serper_discovery",
            "reddit",
            "job_posts",
            "rss_feed",
            "google_reviews",
        ),
        "preferred_query_hints": (
            "site:linkedin.com",
            "site:reddit.com/r/logistics",
            "site:freightos.com",
        ),
        "platform_hosts": ("linkedin.com", "reddit.com", "indiamart.com"),
        "entity_language_pack": "professional_service",
        "platform_mining_mode": "professional",
    },
    "construction": {
        "preferred_sources": (
            "serper_discovery",
            "google_reviews",
            "reddit",
            "job_posts",
            "rss_feed",
        ),
        "preferred_query_hints": (
            "site:linkedin.com",
            "site:houzz.com",
            "site:reddit.com",
        ),
        "platform_hosts": ("indiamart.com", "justdial.com", "sulekha.com"),
        "entity_language_pack": "construction_trade",
        "platform_mining_mode": "directory",
    },
    "hr_recruiting": {
        "preferred_sources": (
            "serper_discovery",
            "job_posts",
            "reddit",
            "rss_feed",
            "google_reviews",
        ),
        "preferred_query_hints": (
            "site:linkedin.com",
            "site:indeed.com",
            "site:glassdoor.com",
        ),
        "platform_hosts": ("linkedin.com", "indeed.com", "glassdoor.com"),
        "entity_language_pack": "professional_service",
        "platform_mining_mode": "professional",
    },
    "marketing_agency": {
        "preferred_sources": (
            "reddit",
            "serper_discovery",
            "google_reviews",
            "hackernews",
            "rss_feed",
        ),
        "preferred_query_hints": (
            "site:reddit.com/r/agency",
            "site:clutch.co",
            "site:g2.com",
        ),
        "platform_hosts": ("clutch.co", "linkedin.com", "reddit.com"),
        "entity_language_pack": "professional_service",
        "platform_mining_mode": "professional",
    },
    "general_services": {
        "preferred_sources": (
            "serper_discovery",
            "reddit",
            "google_reviews",
            "consumer_forum",
        ),
        "preferred_query_hints": (
            "site:reddit.com",
            "site:google.com/maps",
            "site:trustpilot.com",
        ),
        "platform_hosts": ("reddit.com", "trustpilot.com", "google.com"),
        "entity_language_pack": "local_service",
        "platform_mining_mode": "consumer",
    },
}

# ---------------------------------------------------------------------------
# Sub-patterns (family → pattern → config).  Detection is data-driven.
# ---------------------------------------------------------------------------
# detection_terms: (phrase, weight)
# default_sub_pattern: when no terms match
# aliases: free-form label → canonical sub_pattern
# variants: optional b2c / b2b overlays (hints, hosts, pack, mode)

SUB_PATTERN_CONFIG: dict[str, dict[str, Any]] = {
    "education": {
        "default_sub_pattern": "general_education",
        "aliases": {
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
        },
        "patterns": {
            "study_abroad": {
                "detection_terms": (
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
                    ("georgia", 1.4),
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
                "entity_language_pack": "education_study_abroad",
                "platform_mining_mode": "consumer",
                "preferred_query_hints": (
                    "site:reddit.com/r/IndiaEducation",
                    "site:reddit.com/r/studytips",
                    "site:quora.com",
                    "site:youtube.com",
                    "site:shiksha.com",
                    "site:collegedunia.com",
                    "site:facebook.com/groups",
                    "site:yocket.com",
                ),
                "platform_hosts": (
                    "reddit.com",
                    "quora.com",
                    "youtube.com",
                    "shiksha.com",
                    "collegedunia.com",
                    "facebook.com",
                ),
                "preferred_sources": (
                    "serper_discovery",
                    "reddit",
                    "consumer_forum",
                    "youtube",
                    "rss_feed",
                ),
                "b2b_entity_language_pack": "education_institution",
                "b2b_platform_mining_mode": "professional",
                "b2b_preferred_query_hints": (
                    "site:linkedin.com",
                    "site:reddit.com/r/highereducation",
                    "site:timeshighereducation.com",
                ),
                "b2b_platform_hosts": (
                    "linkedin.com",
                    "reddit.com",
                    "timeshighereducation.com",
                ),
                "gemini_examples": (
                    "  Education: site:shiksha.com consultant MBBS abroad\n"
                    "  Education: site:reddit.com looking for study abroad counsellor\n"
                    "  Education: site:quora.com university admission consultant\n"
                    "  Education: site:youtube.com study abroad guidance student\n"
                ),
            },
            "coaching": {
                "detection_terms": (
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
                "entity_language_pack": "education_coaching",
                "platform_mining_mode": "consumer",
                "preferred_query_hints": (
                    "site:reddit.com/r/GetStudying",
                    "site:reddit.com/r/JEENEETards",
                    "site:quora.com",
                    "site:youtube.com",
                    "site:facebook.com/groups",
                    "site:justdial.com",
                    "site:sulekha.com",
                ),
                "platform_hosts": (
                    "reddit.com",
                    "quora.com",
                    "youtube.com",
                    "justdial.com",
                    "sulekha.com",
                    "facebook.com",
                ),
                "preferred_sources": (
                    "serper_discovery",
                    "reddit",
                    "consumer_forum",
                    "youtube",
                    "rss_feed",
                ),
                "b2b_entity_language_pack": "education_institution",
                "b2b_platform_mining_mode": "professional",
                "b2b_preferred_query_hints": (
                    "site:linkedin.com",
                    "site:justdial.com",
                    "site:clutch.co",
                ),
                "b2b_platform_hosts": ("linkedin.com", "justdial.com", "clutch.co"),
                "gemini_examples": (
                    "  Education: site:youtube.com coaching institute review\n"
                    "  Education: site:quora.com best tuition for JEE\n"
                    "  Education: site:reddit.com looking for NEET coaching\n"
                    "  Education: site:justdial.com coaching centre contact\n"
                ),
            },
            "online_courses": {
                "detection_terms": (
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
                "entity_language_pack": "education_online",
                "platform_mining_mode": "consumer",
                "preferred_query_hints": (
                    "site:reddit.com/r/onlinelearning",
                    "site:reddit.com/r/learnprogramming",
                    "site:quora.com",
                    "site:youtube.com",
                    "site:facebook.com/groups",
                    "site:trustpilot.com",
                ),
                "platform_hosts": (
                    "reddit.com",
                    "quora.com",
                    "youtube.com",
                    "trustpilot.com",
                    "facebook.com",
                ),
                "preferred_sources": (
                    "serper_discovery",
                    "reddit",
                    "consumer_forum",
                    "youtube",
                    "rss_feed",
                ),
                "b2b_entity_language_pack": "education_institution",
                "b2b_platform_mining_mode": "professional",
                "b2b_preferred_query_hints": (
                    "site:linkedin.com",
                    "site:g2.com",
                    "site:capterra.com",
                    "site:reddit.com/r/edtech",
                ),
                "b2b_platform_hosts": (
                    "linkedin.com",
                    "g2.com",
                    "capterra.com",
                    "reddit.com",
                ),
                "gemini_examples": (
                    "  Education: site:reddit.com recommend online course\n"
                    "  Education: site:quora.com best skill course review\n"
                    "  Education: site:youtube.com course review student\n"
                    "  Education: site:trustpilot.com online learning program\n"
                ),
            },
            "general_education": {
                "detection_terms": (),
                "entity_language_pack": "education_student",
                "platform_mining_mode": "consumer",
                "preferred_query_hints": (
                    "site:reddit.com",
                    "site:quora.com",
                    "site:youtube.com",
                    "site:facebook.com/groups",
                    "site:justdial.com",
                ),
                "platform_hosts": (
                    "reddit.com",
                    "quora.com",
                    "youtube.com",
                    "facebook.com",
                    "justdial.com",
                ),
                "preferred_sources": (
                    "serper_discovery",
                    "reddit",
                    "consumer_forum",
                    "youtube",
                    "rss_feed",
                ),
                "b2b_entity_language_pack": "education_institution",
                "b2b_platform_mining_mode": "professional",
                "b2b_preferred_query_hints": (
                    "site:linkedin.com",
                    "site:reddit.com/r/highereducation",
                    "site:clutch.co",
                ),
                "b2b_platform_hosts": ("linkedin.com", "reddit.com", "clutch.co"),
                "gemini_examples": (
                    "  Education: site:reddit.com looking for admission help\n"
                    "  Education: site:quora.com college course student parent\n"
                    "  Education: site:youtube.com school admission guidance\n"
                ),
            },
        },
        # Optional B2B cues when vector is ambiguous (not B2C/B2B explicit).
        "b2b_cues": (
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
        ),
    },
    # Example: a new vertical needs ONLY config here — no new Python module.
    # "pet_services" would be added under FAMILY_PLATFORM_CONFIG + optional
    # SUB_PATTERN_CONFIG["pet_services"] with patterns like grooming / training.
}

# Campaign text fields used for sub-pattern detection (generic).
_CAMPAIGN_TEXT_KEYS: tuple[str, ...] = (
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
)


# ---------------------------------------------------------------------------
# Generic resolvers (table-driven — no per-family branches required)
# ---------------------------------------------------------------------------

def get_entity_terms(pack_name: str | None) -> list[str]:
    """Return entity terms for a language pack key (fail-open to neutral_safe)."""
    key = str(pack_name or "").strip() or SAFE_FALLBACK_LANGUAGE_PACK
    terms = ENTITY_LANGUAGE_PACKS.get(key) or ENTITY_LANGUAGE_PACKS[SAFE_FALLBACK_LANGUAGE_PACK]
    return list(terms)


def normalize_sub_pattern(family: str, value: Any) -> str | None:
    """Normalize a free-form sub-pattern for *family*; None if family has none."""
    fam = str(family or "").strip().lower()
    fam_cfg = SUB_PATTERN_CONFIG.get(fam)
    if not fam_cfg:
        return None
    default = str(fam_cfg.get("default_sub_pattern") or "default")
    if value is None:
        return default
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if not text:
        return default
    aliases = fam_cfg.get("aliases") or {}
    text = aliases.get(text, text)
    patterns = fam_cfg.get("patterns") or {}
    if text not in patterns:
        return default
    return text


def campaign_text_blob(campaign: Mapping[str, Any] | None) -> str:
    """Flatten campaign ICP fields into one lowercase string for detection."""
    if not isinstance(campaign, Mapping):
        return ""
    parts: list[str] = []
    for key in _CAMPAIGN_TEXT_KEYS:
        val = campaign.get(key)
        if val is None:
            continue
        if isinstance(val, (list, tuple, set)):
            parts.append(" ".join(str(x) for x in val if x))
        else:
            parts.append(str(val))
    signals = campaign.get("persona_targeting_signals")
    if isinstance(signals, (list, tuple)):
        parts.extend(str(s) for s in signals if s)
    intel = campaign.get("intelligence_strategy")
    if isinstance(intel, Mapping):
        if intel.get("vocabulary_notes"):
            parts.append(str(intel["vocabulary_notes"]))
        if intel.get("primary"):
            parts.append(str(intel["primary"]))
    return " ".join(parts).lower()


def is_b2b_context(
    family: str,
    sourcing_vector: str | None = None,
    *,
    campaign: Mapping[str, Any] | None = None,
) -> bool:
    """True when campaign is institutional / B2B for platform pack selection."""
    vector = (sourcing_vector or "").upper().strip()
    if vector in {"B2C", "D2C", "B2B2C"}:
        return False
    if vector == "B2B":
        return True
    fam_cfg = SUB_PATTERN_CONFIG.get(str(family or "").strip().lower()) or {}
    cues = fam_cfg.get("b2b_cues") or ()
    if not cues:
        return False
    blob = campaign_text_blob(campaign)
    return any(cue in blob for cue in cues)


def detect_sub_pattern(
    family: str,
    campaign: Mapping[str, Any] | None = None,
    *,
    text: str | None = None,
) -> tuple[str | None, float, list[str]]:
    """Detect sub-pattern for *family* from config detection_terms.

    Returns:
        (sub_pattern_or_None, confidence, matched_terms)
        None sub_pattern when family has no SUB_PATTERN_CONFIG entry.
    """
    fam = str(family or "").strip().lower()
    fam_cfg = SUB_PATTERN_CONFIG.get(fam)
    if not fam_cfg:
        return None, 0.0, []

    default = str(fam_cfg.get("default_sub_pattern") or "default")
    patterns: dict[str, Any] = fam_cfg.get("patterns") or {}
    blob = (text or "").strip().lower()
    if not blob and campaign is not None:
        blob = campaign_text_blob(campaign)
    if not blob:
        return default, 0.0, []

    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for name, pcfg in patterns.items():
        terms = pcfg.get("detection_terms") or ()
        if not terms:
            continue
        scores[name] = 0.0
        matched[name] = []
        for phrase, weight in terms:
            if phrase in blob:
                scores[name] += float(weight)
                if phrase not in matched[name]:
                    matched[name].append(phrase)

    if not scores or max(scores.values()) <= 0:
        return default, 0.25, []

    best_name = max(scores.items(), key=lambda kv: kv[1])[0]
    best_score = scores[best_name]
    runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0.0
    margin = best_score - runner_up
    conf = min(0.95, 0.35 + best_score * 0.08 + margin * 0.05)
    return best_name, round(conf, 3), matched.get(best_name, [])[:8]


def _slice_variant(base: Mapping[str, Any], *, is_b2b: bool) -> dict[str, Any]:
    """Pick B2C or B2B overlay fields from a pattern/family config dict."""
    if is_b2b:
        pack = base.get("b2b_entity_language_pack") or base.get("entity_language_pack")
        mode = base.get("b2b_platform_mining_mode") or base.get("platform_mining_mode")
        hints = base.get("b2b_preferred_query_hints") or base.get("preferred_query_hints")
        hosts = base.get("b2b_platform_hosts") or base.get("platform_hosts")
        sources = base.get("b2b_preferred_sources") or base.get("preferred_sources")
    else:
        pack = base.get("entity_language_pack")
        mode = base.get("platform_mining_mode")
        hints = base.get("preferred_query_hints")
        hosts = base.get("platform_hosts")
        sources = base.get("preferred_sources")
    return {
        "entity_language_pack": str(pack or SAFE_FALLBACK_LANGUAGE_PACK),
        "platform_mining_mode": str(mode or DEFAULT_PLATFORM_MINING_MODE),
        "preferred_query_hints": list(hints or ()),
        "platform_hosts": list(hosts or ()),
        "preferred_sources": list(sources or ()),
        "gemini_examples": str(base.get("gemini_examples") or ""),
    }


def resolve_platform_slice(
    family: str,
    *,
    campaign: Mapping[str, Any] | None = None,
    sourcing_vector: str | None = None,
    text: str | None = None,
    sub_pattern: str | None = None,
) -> dict[str, Any]:
    """Resolve full platform + language slice for a domain family.

    Fail-open: never raises. Always returns a dict with the profile contract
    platform fields populated.
    """
    try:
        fam = str(family or "").strip().lower() or "general_services"
        vector = sourcing_vector
        if vector is None and isinstance(campaign, Mapping):
            vector = str(campaign.get("sourcing_vector") or "") or None

        is_b2b = is_b2b_context(fam, vector, campaign=campaign)

        # Family base
        fam_base = FAMILY_PLATFORM_CONFIG.get(fam) or FAMILY_PLATFORM_CONFIG.get(
            "general_services"
        ) or {}
        slice_ = _slice_variant(fam_base, is_b2b=is_b2b)

        detected: str | None = None
        conf = 0.0
        matched: list[str] = []

        if fam in SUB_PATTERN_CONFIG:
            if sub_pattern:
                detected = normalize_sub_pattern(fam, sub_pattern)
                conf = 1.0
            else:
                detected, conf, matched = detect_sub_pattern(fam, campaign, text=text)
            patterns = (SUB_PATTERN_CONFIG[fam].get("patterns") or {})
            pcfg = patterns.get(detected or "") or patterns.get(
                SUB_PATTERN_CONFIG[fam].get("default_sub_pattern") or ""
            ) or {}
            if pcfg:
                slice_ = _slice_variant(pcfg, is_b2b=is_b2b)

        pack = slice_["entity_language_pack"]
        if pack not in ENTITY_LANGUAGE_PACKS:
            pack = SAFE_FALLBACK_LANGUAGE_PACK
        mode = slice_["platform_mining_mode"]
        if mode not in PLATFORM_MINING_MODES:
            mode = DEFAULT_PLATFORM_MINING_MODE

        entity_terms = get_entity_terms(pack)

        return {
            "domain_family": fam,
            "sub_pattern": detected,
            "sub_pattern_confidence": conf,
            "sub_pattern_matched_terms": matched,
            "is_b2b_context": is_b2b,
            "entity_language_pack": pack,
            "entity_terms": entity_terms,
            "platform_mining_mode": mode,
            "preferred_query_hints": slice_["preferred_query_hints"][:12],
            "preferred_sources": slice_["preferred_sources"][:10],
            "platform_hosts": slice_["platform_hosts"][:8],
            "gemini_examples": slice_.get("gemini_examples") or "",
            # Backward-compat aliases (education path / older logs)
            "language_pack": pack,
            "education_sub_pattern": detected if fam == "education" else None,
            "is_b2b_education": is_b2b if fam == "education" else False,
        }
    except Exception:  # noqa: BLE001 — produce must never break
        return {
            "domain_family": str(family or "general_services"),
            "sub_pattern": None,
            "sub_pattern_confidence": 0.0,
            "sub_pattern_matched_terms": [],
            "is_b2b_context": False,
            "entity_language_pack": SAFE_FALLBACK_LANGUAGE_PACK,
            "entity_terms": get_entity_terms(SAFE_FALLBACK_LANGUAGE_PACK),
            "platform_mining_mode": DEFAULT_PLATFORM_MINING_MODE,
            "preferred_query_hints": list(
                (FAMILY_PLATFORM_CONFIG.get("general_services") or {}).get(
                    "preferred_query_hints"
                )
                or ()
            ),
            "preferred_sources": list(
                (FAMILY_PLATFORM_CONFIG.get("general_services") or {}).get(
                    "preferred_sources"
                )
                or ()
            ),
            "platform_hosts": ["reddit.com", "trustpilot.com"],
            "gemini_examples": "",
            "language_pack": SAFE_FALLBACK_LANGUAGE_PACK,
            "education_sub_pattern": None,
            "is_b2b_education": False,
            "resolve_error": True,
        }


def entity_terms_from_profile(domain_profile: Mapping[str, Any] | None) -> list[str]:
    """Extract entity terms from a domain profile; fail-open to neutral_safe."""
    if not isinstance(domain_profile, Mapping):
        return get_entity_terms(SAFE_FALLBACK_LANGUAGE_PACK)
    # Explicit terms on profile win.
    for key in ("entity_terms", "platform_entity_terms"):
        raw = domain_profile.get(key)
        if isinstance(raw, (list, tuple)) and raw:
            cleaned = [str(t).strip() for t in raw if str(t).strip()]
            if cleaned:
                return cleaned
    pack = (
        domain_profile.get("entity_language_pack")
        or domain_profile.get("language_pack")
    )
    return get_entity_terms(str(pack) if pack else SAFE_FALLBACK_LANGUAGE_PACK)


def language_pack_from_profile(domain_profile: Mapping[str, Any] | None) -> str:
    if not isinstance(domain_profile, Mapping):
        return SAFE_FALLBACK_LANGUAGE_PACK
    pack = (
        domain_profile.get("entity_language_pack")
        or domain_profile.get("language_pack")
        or SAFE_FALLBACK_LANGUAGE_PACK
    )
    pack = str(pack).strip()
    if pack not in ENTITY_LANGUAGE_PACKS:
        return SAFE_FALLBACK_LANGUAGE_PACK
    return pack


def platform_mining_mode_from_profile(domain_profile: Mapping[str, Any] | None) -> str:
    if not isinstance(domain_profile, Mapping):
        return DEFAULT_PLATFORM_MINING_MODE
    mode = str(domain_profile.get("platform_mining_mode") or "").strip().lower()
    if mode in PLATFORM_MINING_MODES:
        return mode
    return DEFAULT_PLATFORM_MINING_MODE


def platform_hosts_from_profile(
    domain_profile: Mapping[str, Any] | None,
    *,
    family: str | None = None,
) -> list[str]:
    """Hosts for deterministic platform mining (profile → family config)."""
    hosts: list[str] = []
    if isinstance(domain_profile, Mapping):
        for h in domain_profile.get("platform_hosts") or []:
            if h:
                hosts.append(str(h).strip().lower().replace("www.", ""))
        if not hosts:
            # Derive from preferred_query_hints site: operators
            for hint in domain_profile.get("preferred_query_hints") or []:
                lower = str(hint).lower()
                for part in lower.split():
                    if part.startswith("site:"):
                        host = part[5:].split("/")[0].strip()
                        if host:
                            hosts.append(host)
    if not hosts:
        fam = (family or "").strip().lower()
        if isinstance(domain_profile, Mapping) and not fam:
            fam = str(domain_profile.get("domain_family") or "").strip().lower()
        cfg = FAMILY_PLATFORM_CONFIG.get(fam) or {}
        hosts = [str(h).lower() for h in (cfg.get("platform_hosts") or ())]
    # Dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for h in hosts:
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out[:8]


def family_platform_hosts(family: str) -> list[str]:
    cfg = FAMILY_PLATFORM_CONFIG.get(str(family or "").strip().lower()) or {}
    return [str(h).lower() for h in (cfg.get("platform_hosts") or ())]


def family_query_hints(family: str) -> list[str]:
    cfg = FAMILY_PLATFORM_CONFIG.get(str(family or "").strip().lower()) or {}
    return list(cfg.get("preferred_query_hints") or ())


def family_preferred_sources(family: str) -> list[str]:
    cfg = FAMILY_PLATFORM_CONFIG.get(str(family or "").strip().lower()) or {}
    return list(cfg.get("preferred_sources") or ())


def gemini_platform_examples(domain_profile: Mapping[str, Any] | None) -> str:
    """Few-shot lines for Gemini platform-mining prompt from profile/config."""
    if not isinstance(domain_profile, Mapping):
        return ""
    examples = domain_profile.get("gemini_examples")
    if examples:
        return str(examples)
    fam = str(domain_profile.get("domain_family") or "").strip().lower()
    sub = domain_profile.get("sub_pattern") or domain_profile.get("education_sub_pattern")
    fam_cfg = SUB_PATTERN_CONFIG.get(fam) or {}
    patterns = fam_cfg.get("patterns") or {}
    pcfg = patterns.get(str(sub or "")) or {}
    return str(pcfg.get("gemini_examples") or "")


def query_uses_directory_only_language(query: str) -> bool:
    """True if query has real-estate agent+broker co-occurrence."""
    q = (query or "").lower()
    if not q:
        return False
    if any(p in q for p in FORBIDDEN_WHEN_NOT_DIRECTORY):
        return True
    tokens = set(q.replace(":", " ").split())
    return "agent" in tokens and "broker" in tokens


def pack_allows_directory_language(pack_name: str | None) -> bool:
    return str(pack_name or "") == "directory_listing"


def host_pack_override(host: str, current_pack: str) -> str:
    """Optional host-based pack override for neutral/consumer packs only."""
    host_l = (host or "").strip().lower()
    if not host_l:
        return current_pack
    # Never override directory_listing (RE portals) or education packs.
    if current_pack in {
        "directory_listing",
        "education_student",
        "education_study_abroad",
        "education_coaching",
        "education_online",
        "education_institution",
        "supplier_directory",
        "construction_trade",
        "healthcare_consumer",
        "hospitality_review",
        "saas_review",
        "professional_service",
    }:
        return current_pack
    for needle, pack in HOST_LANGUAGE_OVERRIDES.items():
        if needle in host_l:
            return pack
    return current_pack
