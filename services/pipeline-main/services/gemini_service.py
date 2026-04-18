"""
Pipeline-main — Gemini/Vertex AI service.

Extracted from ``main.py`` with V23 safety amendments:
  - ``init_vertex()`` is called lazily via ``core.clients`` — never at module scope.
  - ``call_gemini_2_5()`` uses a ``concurrent.futures.ThreadPoolExecutor`` to
    enforce the 45-second wall-clock ceiling (unchanged from monolith).
  - All callers block synchronously — no fire-and-forget threads.
"""
from __future__ import annotations

import json
import concurrent.futures
from typing import Any, Optional

from tenacity import (
    retry, wait_exponential, stop_after_attempt, retry_if_exception_type,
)

from core.logging import get_logger  # type: ignore[import]
from core.clients import init_vertex  # type: ignore[import]
from core.config import GEMINI_TIMEOUT_S  # type: ignore[import]

log = get_logger("pipeline.gemini")


# ---------------------------------------------------------------------------
# Core Gemini caller
# ---------------------------------------------------------------------------

def call_gemini_2_5(
    prompt: str,
    expect_json: bool = True,
    response_schema: Optional[dict] = None,
    system_instruction: Optional[str] = None,
) -> Any:
    """Invoke Gemini 2.5 Flash with a wall-clock timeout and tenacity retry.

    Initialises Vertex AI lazily on first call (thread-safe, no import-time gRPC).

    Args:
        prompt:             Full prompt string.
        expect_json:        If True, sets ``response_mime_type="application/json"``.
        response_schema:    Gemini JSON schema dict (optional).
        system_instruction: Gemini system instruction string (optional).

    Returns:
        Parsed dict or list if ``expect_json`` is True, otherwise raw text string.

    Raises:
        TimeoutError: If Vertex AI exceeds the 45-second wall-clock ceiling.
        Exception:    Propagates on all-retries-exhausted quota errors.
    """
    # Lazy init — safe under Gunicorn pre-fork (threading.Lock in clients.py)
    init_vertex()

    from vertexai.generative_models import GenerativeModel, GenerationConfig  # type: ignore[import]
    from google.api_core.exceptions import ResourceExhausted  # type: ignore[import]

    model = GenerativeModel(
        "gemini-2.5-flash",
        system_instruction=system_instruction,
    )
    config = (
        GenerationConfig(
            response_mime_type="application/json",
            response_schema=response_schema,
        )
        if expect_json
        else None
    )

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type(ResourceExhausted),
    )
    def _invoke():
        return model.generate_content(prompt, generation_config=config)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future   = executor.submit(_invoke)
            response = future.result(timeout=GEMINI_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        log.error("gemini_timeout", timeout_s=GEMINI_TIMEOUT_S)
        raise TimeoutError("Vertex AI timeout")

    if expect_json:
        return json.loads(response.text)
    return response.text


# ---------------------------------------------------------------------------
# Pre-filter tiering gate
# ---------------------------------------------------------------------------

_TIERING_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "url":             {"type": "STRING"},
            "confidence_tier": {"type": "STRING", "enum": ["High", "Medium", "Low"]},
            "reason":          {"type": "STRING"},
        },
        "required": ["url", "confidence_tier", "reason"],
    },
}


def pre_filter_gemini(snippets: list, bio: str, location_target: str) -> dict:
    """Gemini tiering gate: classify Serper snippet URLs as High/Medium/Low.

    Args:
        snippets:        List of dicts with ``url``, ``title``, ``snippet`` keys.
        bio:             User's business bio (context for scoring).
        location_target: Geo target string.

    Returns:
        ``{"High": [url, ...], "Medium": [url, ...]}`` — Low results dropped.
        Returns ``{"High": [], "Medium": []}`` on any failure.
    """
    if not snippets:
        return {"High": [], "Medium": []}

    prompt = f"""CONFIDENCE TIERING GATE: Evaluate each URL snippet against the user's business context.

USER BIO: '{bio}'
TARGET LOCATION: '{location_target}'

# STEP 1 — PERSONA CLASSIFICATION
Classify the user as B2B Vendor or B2C Service Provider.

# STEP 2 — PERSONA-LOCKED TIERING RULES
IF B2B Vendor:
- High: Business explicitly experiencing the pain point, correct intent + geo.
- Medium: Ambiguous intent or geo, relevant industry.
- Low: Competitor, directory, aggregator, SEO blog, D2C retail.

IF B2C Service Provider:
- High: Individual EXPLICITLY expressing the pain point in their own words.
- Medium: Individual whose need is implied but not explicit.
- Low: Agency, corporate, competitor, directory, or institutional URL.

# STEP 3 — UNIVERSAL RULES
SOCIAL PLATFORM RULE: Evaluate the SPECIFIC POST intent, not the platform.
GEO RULE: Wrong region → Low.

Snippets: {json.dumps(snippets)}"""

    try:
        tiered = call_gemini_2_5(prompt, expect_json=True, response_schema=_TIERING_SCHEMA)
        if not isinstance(tiered, list):
            raise ValueError("Expected list from tiering gate")
    except Exception as exc:
        log.warning("pre_filter_gemini_failed", error=str(exc))
        return {"High": [], "Medium": []}

    output: dict[str, list] = {"High": [], "Medium": []}
    for item in tiered:
        tier = item.get("confidence_tier", "Low")
        url  = item.get("url", "").strip()
        if not url.startswith("http"):
            continue
        if tier in ("High", "Medium"):
            output[tier].append(url)

    log.info("pre_filter_complete",
             high=len(output["High"]), medium=len(output["Medium"]))
    return output


# ---------------------------------------------------------------------------
# Final score + DM generator
# ---------------------------------------------------------------------------

_SCORE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "matched_campaigns": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "campaign_id": {"type": "STRING"},
                    "raw_score":  {"type": "INTEGER"},
                },
                "required": ["campaign_id", "raw_score"],
            },
        },
        "dm":                           {"type": "STRING"},
        "pain_point":                   {"type": "STRING"},
        "icebreaker_angle":             {"type": "STRING"},
        "intent_signal":                {"type": "STRING"},
        "hiring_intent_found":          {"type": "STRING", "enum": ["Yes", "No"]},
        "tech_stack_found":             {"type": "ARRAY", "items": {"type": "STRING"}},
        "contact_endpoints": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "platform": {
                        "type": "STRING",
                        "enum": ["instagram", "reddit", "whatsapp", "gmb",
                                 "email", "linkedin", "facebook", "other"],
                    },
                    "uri": {"type": "STRING"},
                },
                "required": ["platform", "uri"],
            },
        },
        "decision_maker_name":          {"type": "STRING"},
        "decision_maker_title":         {"type": "STRING"},
        "company_size_tier":            {"type": "STRING"},
        "primary_objection_hypothesis": {"type": "STRING"},
        "company_name":                 {"type": "STRING"},
    },
    "required": [
        "matched_campaigns", "dm", "pain_point", "icebreaker_angle",
        "intent_signal", "hiring_intent_found", "tech_stack_found",
        "contact_endpoints", "decision_maker_name", "decision_maker_title",
        "company_size_tier", "primary_objection_hypothesis",
    ],
}


def final_score_and_dm(
    text: str,
    active_campaigns: list,
    context_payload: str,
    tech_stack: list,
    historical_dms: Optional[list] = None,
    source_url: Optional[str] = None,
) -> dict:
    """Score a lead against all active campaigns and draft an outreach message.

    Args:
        text:             DOM text / snippet text of the lead page.
        active_campaigns: List of campaign dicts with ``id``, ``bio``, ``keywords``.
        context_payload:  Contextual enrichment string (GMB, social, hiring).
        tech_stack:       List of detected tech stack strings.
        historical_dms:   Past successful DM strings (RLHF feedback loop).
        source_url:       Original source URL (used for social platform detection).

    Returns:
        Dict with ``score``, ``dm``, ``pain_point``, ``matched_campaign_ids``, etc.

    Raises:
        ValueError: On LLM parse failure.
    """
    social_domains_check = [
        "reddit.com", "quora.com", "facebook.com",
        "linkedin.com", "instagram.com",
    ]
    is_social = source_url and any(d in source_url.lower() for d in social_domains_check)
    platform  = "other"
    if source_url:
        for kw, name in [
            ("reddit.com",    "reddit"),
            ("quora.com",     "other"),
            ("facebook.com",  "facebook"),
            ("linkedin.com",  "linkedin"),
            ("instagram.com", "instagram"),
        ]:
            if kw in source_url:
                platform = name
                break

    social_uri_rule = ""
    if is_social:
        social_uri_rule = (
            f"\nSOCIAL PROFILE URI RULE (MANDATORY): "
            f"The source URL '{source_url}' is from a social platform. "
            f"Extract the original poster's user profile URL using platform enum ('{platform}'). "
            "Do NOT return empty contact_endpoints if a profile link is present."
        )

    def _resolve_bio(c: dict) -> str:
        if c.get("persona_id") and c.get("persona_bio"):
            return c["persona_bio"]
        raw = c.get("bio", "")
        if raw == "CHILD_CAMPAIGN_OVERRIDE":
            return (
                c.get("effective_bio") or c.get("campaign_focus") or c.get("pain_point") or ""
            )
        return raw

    campaigns_str = json.dumps([{
        "campaign_id": c.get("id", c.get("name")),
        "bio":         _resolve_bio(c),
        "keywords":    c.get("persona_keywords") or c.get("keywords", ""),
    } for c in active_campaigns], indent=2)

    prompt = f"""You are a Dynamic Intent Analyzer evaluating a lead against multiple campaigns.
SOURCE TYPE: {'SOCIAL/FORUM POST' if is_social else 'COMPANY WEBSITE/FORMAL PAGE'}
PLATFORM: {platform.upper()}

# STEP 1 — CROSS-POLLINATION EVALUATION MATRIX
Evaluate the text DOM against EACH campaign below. Score 1-10. Return only campaigns where score >= 4.
{campaigns_str}

# STEP 2 — OUTREACH COPILOT DRAFT
Identify the campaign with the HIGHEST match score.
{'Community-native tone: empathetic, casual, 3 sentences max, question-first.' if is_social else 'B2B tone: Spear & Shield pitch, warm, confident, peer-to-peer.'}

# STEP 3 — EXTRACTION RULES
- hiring_intent_found: ONLY 'Yes' or 'No'.
- contact_endpoints: Only explicitly present contacts. URI must have full protocol prefix (https://).
- PHONE DEDUPLICATION: Max 2 numbers.
{social_uri_rule}

CONTEXTUAL DORKING DATA:
{context_payload}

DETECTED TECH STACK:
{', '.join(tech_stack) if tech_stack else 'None extracted'}
"""
    if historical_dms:
        prompt += f"\nPast successful converted messages (match tone): {historical_dms}\n"
    prompt += f"\nText DOM:\n{text}"

    sys_inst = (
        "You are a Dynamic Intent Analyzer with adaptive persona intelligence. "
        "For COMPANY WEBSITE: elite B2B profiler — demand formal signals, extract decision-makers. "
        "For SOCIAL/FORUM POST: community intelligence analyst — score purely on pain intensity, "
        "draft empathetic conversation-starters. Never hallucinate contacts."
    )

    try:
        data = call_gemini_2_5(
            prompt,
            expect_json=True,
            response_schema=_SCORE_SCHEMA,
            system_instruction=sys_inst,
        )
        if not isinstance(data, dict):
            raise ValueError("Parsed JSON is not a dict")

        matched = data.get("matched_campaigns", [])
        if not matched:
            return {
                "score": 0, "matched_campaign_ids": [], "trend_mapped": False,
                "highest_campaign_id": "Unknown",
                "pain_point": data.get("pain_point", "Unknown"),
                "hiring_intent_found": data.get("hiring_intent_found", "No"),
                "tech_stack_found": data.get("tech_stack_found", []),
                "icebreaker_angle": data.get("icebreaker_angle", ""),
                "intent_signal":    data.get("intent_signal", ""),
                "dm": data.get("dm", "Failed to generate DM"),
                "contact_endpoints": data.get("contact_endpoints", []),
                "decision_maker_name":          data.get("decision_maker_name", "Unknown"),
                "decision_maker_title":         data.get("decision_maker_title", "Unknown"),
                "company_size_tier":            data.get("company_size_tier", "Unknown"),
                "primary_objection_hypothesis": data.get("primary_objection_hypothesis", "Unknown"),
                "company_name": data.get("company_name") or None,
            }

        matched.sort(key=lambda x: x.get("raw_score", 0), reverse=True)
        base_score       = float(matched[0].get("raw_score", 0))
        highest_campaign = matched[0].get("campaign_id", "Unknown")
        matched_ids      = [str(c.get("campaign_id")) for c in matched]
        multiplier = {1: 1.0, 2: 1.3}.get(len(matched), 1.6)
        final_score = int(min(base_score * multiplier, 10.0))

        return {
            "score":                        final_score,
            "matched_campaign_ids":         matched_ids,
            "trend_mapped":                 len(matched) >= 3,
            "highest_campaign_id":          highest_campaign,
            "pain_point":                   data.get("pain_point", "Unknown"),
            "hiring_intent_found":          data.get("hiring_intent_found", "No"),
            "tech_stack_found":             data.get("tech_stack_found", []),
            "icebreaker_angle":             data.get("icebreaker_angle", ""),
            "intent_signal":                data.get("intent_signal", ""),
            "dm":                           data.get("dm", "Failed to generate DM"),
            "contact_endpoints":            data.get("contact_endpoints", []),
            "decision_maker_name":          data.get("decision_maker_name", "Unknown"),
            "decision_maker_title":         data.get("decision_maker_title", "Unknown"),
            "company_size_tier":            data.get("company_size_tier", "Unknown"),
            "primary_objection_hypothesis": data.get("primary_objection_hypothesis", "Unknown"),
            "company_name":                 data.get("company_name") or None,
        }

    except Exception as exc:
        raise ValueError(f"LLM Parsing Failure: {exc}") from exc
