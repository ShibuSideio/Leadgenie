"""Education sub-pattern profiles + platform-mining language (domain-v3).

Covers:
  - study_abroad / medical / MBBS education → student/parent platforms
  - coaching / tuition / exam_prep pattern
  - generic education fallback
  - non-education (real_estate) unchanged: still agent/broker
  - agent broker language never appears for education B2C
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main")
)
for path in (ROOT, PIPELINE_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from shared.education_profiles import (  # noqa: E402
    contains_forbidden_education_language,
    detect_education_sub_pattern,
    resolve_education_profile,
)
from services.domain_intelligence import (  # noqa: E402
    DOMAIN_PROFILE_VERSION,
    _build_query_from_hint,
    infer_domain_profile,
)
from services.query_brain import (  # noqa: E402
    _platform_mining_deterministic_fallback,
    _resolve_platform_entity_language,
    _generate_platform_mining_queries,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MBBS_GEORGIA_CAMPAIGN = {
    "name": "MBBS Georgia — Kerala Student Outreach",
    "bio": (
        "Help Indian students find trusted education consultants for MBBS "
        "abroad in Georgia. Study abroad medical education counselling."
    ),
    "effective_bio": "Study abroad MBBS Georgia admission consultants for Kerala students.",
    "pain_point": "Parents struggle to find genuine overseas education consultants.",
    "keywords": "MBBS, study abroad, Georgia, education consultant, admission, nursing",
    "persona_name": "Kerala parents of NEET students",
    "persona_bio": "Parents seeking MBBS seats abroad for their children.",
    "persona_targeting_signals": [
        "looking for MBBS abroad consultant",
        "study in Georgia medical college",
    ],
    "location": "Kerala, India, Asia",
    "sourcing_vector": "B2C",
    "intelligence_strategy": {"primary": "COLLOQUIAL_DISCOVERY"},
}

COACHING_CAMPAIGN = {
    "name": "JEE NEET Coaching Leads — Delhi NCR",
    "bio": "Acquire student leads for entrance exam coaching and tuition classes.",
    "keywords": "JEE coaching, NEET coaching, exam prep, tuition, test prep, institute",
    "pain_point": "Students searching for best coaching institute near me.",
    "location": "Delhi, India",
    "sourcing_vector": "B2C",
    "intelligence_strategy": {"primary": "COLLOQUIAL_DISCOVERY"},
}

GENERIC_EDU_CAMPAIGN = {
    "name": "Local School Admissions Outreach",
    "bio": "Help families find school admission guidance and education services.",
    "keywords": "school, education, admission, student, parent",
    "location": "Bangalore, India",
    "sourcing_vector": "B2C",
}

REAL_ESTATE_CAMPAIGN = {
    "name": "Oman Realty Prospecting",
    "bio": "Help buyers find trusted property brokers for apartments and villas.",
    "keywords": "real estate, property agent, apartment, villa",
    "location": "Muscat, Oman",
    "sourcing_vector": "B2C",
}


def _hints_blob(profile: dict) -> str:
    return " ".join(str(h).lower() for h in (profile.get("preferred_query_hints") or []))


# ---------------------------------------------------------------------------
# education_profiles SSOT
# ---------------------------------------------------------------------------

def test_detect_study_abroad_from_mbbs_georgia():
    pattern, conf, matched = detect_education_sub_pattern(MBBS_GEORGIA_CAMPAIGN)
    assert pattern == "study_abroad"
    assert conf > 0.4
    assert any("mbbs" in m or "study abroad" in m for m in matched)


def test_detect_coaching_pattern():
    pattern, conf, matched = detect_education_sub_pattern(COACHING_CAMPAIGN)
    assert pattern == "coaching"
    assert conf > 0.4
    assert matched


def test_detect_general_education_fallback():
    pattern, conf, _ = detect_education_sub_pattern(GENERIC_EDU_CAMPAIGN)
    assert pattern == "general_education"
    # Weak/no sub-pattern terms → low-ish confidence is OK
    assert conf >= 0.0


def test_resolve_education_b2c_excludes_linkedin_and_teachers():
    edu = resolve_education_profile(MBBS_GEORGIA_CAMPAIGN, sourcing_vector="B2C")
    assert edu["education_sub_pattern"] == "study_abroad"
    assert edu["is_b2b_education"] is False
    hints = " ".join(edu["preferred_query_hints"]).lower()
    assert "linkedin.com" not in hints
    assert "r/teachers" not in hints
    assert "coursera.org" not in hints
    # Student/parent appropriate surfaces present
    assert "reddit" in hints or "quora" in hints or "shiksha" in hints
    terms = " ".join(edu["entity_terms"]).lower()
    assert "broker" not in terms
    assert any(t in terms for t in ("consultant", "counsellor", "admission", "student"))


def test_resolve_education_b2b_keeps_linkedin():
    camp = {
        **MBBS_GEORGIA_CAMPAIGN,
        "sourcing_vector": "B2B",
        "bio": "University partnership and institutional recruitment for medical colleges.",
        "keywords": "university partnership, institution, campus recruitment",
    }
    edu = resolve_education_profile(camp, sourcing_vector="B2B")
    assert edu["is_b2b_education"] is True
    hints = " ".join(edu["preferred_query_hints"]).lower()
    assert "linkedin.com" in hints
    assert edu["language_pack"] == "education_b2b"


def test_forbidden_education_language_detector():
    assert contains_forbidden_education_language(
        "site:reddit.com agent broker Kerala India Asia"
    )
    assert not contains_forbidden_education_language(
        "site:shiksha.com consultant admission Georgia"
    )


# ---------------------------------------------------------------------------
# domain_intelligence integration
# ---------------------------------------------------------------------------

def test_infer_education_study_abroad_profile():
    profile = infer_domain_profile(MBBS_GEORGIA_CAMPAIGN)
    assert profile["domain_family"] == "education"
    assert profile["version"] == DOMAIN_PROFILE_VERSION
    assert profile.get("education_sub_pattern") == "study_abroad"
    blob = _hints_blob(profile)
    assert "r/teachers" not in blob
    assert "coursera.org" not in blob
    # LinkedIn only for B2B education
    assert "linkedin.com" not in blob
    assert any(
        s in blob
        for s in ("reddit", "quora", "youtube", "shiksha", "collegedunia", "facebook")
    )
    entity_blob = " ".join(str(t).lower() for t in (profile.get("entity_terms") or []))
    assert "broker" not in entity_blob
    assert "agent" not in entity_blob or "consultant" in entity_blob


def test_infer_education_coaching_profile():
    profile = infer_domain_profile(COACHING_CAMPAIGN)
    assert profile["domain_family"] == "education"
    assert profile.get("education_sub_pattern") == "coaching"
    blob = _hints_blob(profile)
    assert "r/teachers" not in blob
    assert "coursera.org" not in blob
    terms = " ".join(str(t).lower() for t in (profile.get("entity_terms") or []))
    assert any(t in terms for t in ("coaching", "tutor", "tuition", "institute", "student"))


def test_infer_education_generic_fallback():
    profile = infer_domain_profile(GENERIC_EDU_CAMPAIGN)
    assert profile["domain_family"] == "education"
    assert profile.get("education_sub_pattern") in {
        "general_education",
        "study_abroad",  # "admission" alone may not flip; general expected
        "coaching",
    }
    # Whatever sub-pattern, platforms must not be legacy teacher pack.
    blob = _hints_blob(profile)
    assert "r/teachers" not in blob
    assert "coursera.org" not in blob


def test_non_education_real_estate_unchanged():
    profile = infer_domain_profile(REAL_ESTATE_CAMPAIGN)
    assert profile["domain_family"] == "real_estate"
    blob = _hints_blob(profile)
    assert "bayut" in blob or "propertyfinder" in blob or "dubizzle" in blob
    # No education fields forced onto non-education profiles
    assert not profile.get("education_sub_pattern")


def test_build_query_from_hint_education_no_agent_broker():
    profile = infer_domain_profile(MBBS_GEORGIA_CAMPAIGN)
    q = _build_query_from_hint(
        "site:reddit.com",
        family="education",
        location="Kerala, India",
        keywords="MBBS, study abroad",
        domain_profile=profile,
    )
    q_l = q.lower()
    assert "site:reddit.com" in q_l
    assert not ("agent" in q_l.split() and "broker" in q_l.split())
    assert "broker" not in q_l


def test_build_query_from_hint_real_estate_keeps_agent_broker():
    q = _build_query_from_hint(
        "site:bayut.com",
        family="real_estate",
        location="Muscat, Oman",
        keywords="villa, apartment",
        domain_profile={"domain_family": "real_estate"},
    )
    q_l = q.lower()
    assert "bayut" in q_l
    assert "agent" in q_l or "broker" in q_l


# ---------------------------------------------------------------------------
# query_brain platform mining language
# ---------------------------------------------------------------------------

def test_resolve_language_education_never_agent_broker():
    profile = infer_domain_profile(MBBS_GEORGIA_CAMPAIGN)
    terms, pack = _resolve_platform_entity_language(
        domain_family="education",
        domain_profile=profile,
        host="reddit.com",
        sourcing_vector="B2C",
        primary_strategy="COLLOQUIAL_DISCOVERY",
    )
    joined = " ".join(terms).lower()
    assert "broker" not in joined
    assert "agent" not in joined or pack.startswith("education")
    # Education pack should not be legacy_default
    assert pack != "legacy_default"
    assert any(
        t in joined
        for t in ("consultant", "counsellor", "admission", "student", "looking for")
    )


def test_resolve_language_real_estate_still_agent_broker():
    terms, pack = _resolve_platform_entity_language(
        domain_family="real_estate",
        domain_profile={"domain_family": "real_estate"},
        host="bayut.com",
        sourcing_vector="B2C",
        primary_strategy="PLATFORM_MINING",
    )
    assert pack == "real_estate"
    assert "agent" in terms
    assert "broker" in terms


def test_deterministic_fallback_education_no_agent_broker():
    profile = infer_domain_profile(MBBS_GEORGIA_CAMPAIGN)
    queries = _platform_mining_deterministic_fallback(
        campaign_id="QzqnAG4fiKYOmP7UOOLG",
        strategy_plan={
            "geo_terms": ["Kerala", "India", "Asia"],
            "primary_strategy": "COLLOQUIAL_DISCOVERY",
            "sourcing_vector": "B2C",
        },
        domain_profile=profile,
        domain_targets=[],
        domain_family="education",
        reason="unit_test",
        sourcing_vector="B2C",
        primary_strategy="COLLOQUIAL_DISCOVERY",
    )
    assert len(queries) >= 1
    for q in queries:
        assert "site:" in q
        assert not contains_forbidden_education_language(q), q
        tokens = set(q.lower().replace(":", " ").split())
        assert not ("agent" in tokens and "broker" in tokens), q
    # Must not seed Coursera/teachers as sole surfaces
    joined = " ".join(queries).lower()
    assert "coursera.org" not in joined
    assert "r/teachers" not in joined


def test_deterministic_fallback_real_estate_unchanged():
    queries = _platform_mining_deterministic_fallback(
        campaign_id="re-1",
        strategy_plan={"geo_terms": ["Muscat", "Oman"], "platform_targets": ["bayut.com"]},
        domain_profile={
            "domain_family": "real_estate",
            "preferred_query_hints": ["site:bayut.com"],
        },
        domain_targets=["bayut.com", "propertyfinder.com"],
        domain_family="real_estate",
        reason="unit_test",
        sourcing_vector="B2C",
        primary_strategy="PLATFORM_MINING",
    )
    assert len(queries) >= 1
    joined = " ".join(queries).lower()
    assert "bayut.com" in joined or "propertyfinder.com" in joined
    assert "agent" in joined or "broker" in joined


def test_generate_platform_mining_education_gemini_forbidden_stripped(monkeypatch):
    """Gemini outputs with agent broker for education are dropped; fallback safe."""
    profile = infer_domain_profile(MBBS_GEORGIA_CAMPAIGN)
    ctx = SimpleNamespace(
        campaign_id="edu-gemini",
        sourcing_vector="B2C",
        strategy="COLLOQUIAL_DISCOVERY",
    )

    def _fake_gemini(*_a, **_k):
        return {
            "platform_queries": [
                "site:reddit.com agent broker Kerala India Asia",
                "site:shiksha.com consultant MBBS Georgia",
            ]
        }

    import services.gemini_service as gemini_mod

    monkeypatch.setattr(gemini_mod, "call_gemini_2_5", _fake_gemini)
    queries = _generate_platform_mining_queries(
        ctx=ctx,
        bio=MBBS_GEORGIA_CAMPAIGN["bio"],
        kw_str=MBBS_GEORGIA_CAMPAIGN["keywords"],
        vector_label="B2C",
        blacklist="",
        strategy_plan={
            "primary_strategy": "COLLOQUIAL_DISCOVERY",
            "geo_terms": ["Kerala", "India"],
        },
        domain_profile=profile,
    )
    assert len(queries) >= 1
    for q in queries:
        assert not contains_forbidden_education_language(q), q
        tokens = set(q.lower().replace(":", " ").split())
        assert not ("agent" in tokens and "broker" in tokens), q
