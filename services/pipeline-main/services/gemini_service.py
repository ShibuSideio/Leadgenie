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
import os
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

_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
_FALLBACK_MODEL = "gemini-2.0-flash"


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
    from google.api_core.exceptions import (  # type: ignore[import]
        ResourceExhausted,
        ServiceUnavailable,
        DeadlineExceeded,
        NotFound,
    )

    def _build_and_invoke(model_name: str):
        """Build a GenerativeModel for *model_name* and invoke it with retries."""
        _model = GenerativeModel(
            model_name,
            system_instruction=system_instruction,
        )
        _config = (
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
            retry=retry_if_exception_type(
                (ResourceExhausted, ServiceUnavailable, DeadlineExceeded, ConnectionError)
            ),
        )
        def _invoke():
            return _model.generate_content(prompt, generation_config=_config)

        return _invoke()

    def _safe_extract(response):
        """P0-5: Guard response.text — return safe fallback on empty candidates."""
        if not response.candidates:
            log.warning(
                "gemini_empty_candidates",
                note="Gemini returned no candidates (possible safety block).",
            )
            if expect_json:
                return {}
            return ""
        if expect_json:
            return json.loads(response.text)
        return response.text

    # P2-EXT-2: Model fallback chain — primary → fallback on model-specific errors
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future   = executor.submit(_build_and_invoke, _GEMINI_MODEL)
            response = future.result(timeout=GEMINI_TIMEOUT_S)
        return _safe_extract(response)
    except concurrent.futures.TimeoutError:
        log.error("gemini_timeout", timeout_s=GEMINI_TIMEOUT_S)
        raise TimeoutError("Vertex AI timeout")
    except (NotFound, ResourceExhausted) as fallback_exc:
        if _GEMINI_MODEL == _FALLBACK_MODEL:
            raise  # Already on fallback; don't loop
        log.warning(
            "gemini_model_fallback",
            primary_model=_GEMINI_MODEL,
            fallback_model=_FALLBACK_MODEL,
            error=str(fallback_exc),
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future   = executor.submit(_build_and_invoke, _FALLBACK_MODEL)
                response = future.result(timeout=GEMINI_TIMEOUT_S)
            return _safe_extract(response)
        except concurrent.futures.TimeoutError:
            log.error("gemini_fallback_timeout", timeout_s=GEMINI_TIMEOUT_S)
            raise TimeoutError("Vertex AI fallback timeout")


# ---------------------------------------------------------------------------
# Topic coherence gate (Phase 1B)
# ---------------------------------------------------------------------------


def check_topic_coherence(
    title: str,
    snippet: str,
    campaign_topic: str,
) -> bool:
    """Cheap Gemini Flash gate: is this content *primarily* about the campaign topic?

    Uses temperature=0.1 for near-deterministic YES/NO classification.
    Fail-open on any exception (returns True) so the pipeline never blocks
    on a transient Gemini error.

    Cost: ~$0.0001 per call (minimal prompt, no schema enforcement).

    Args:
        title:          Content title (headline, post title, etc.).
        snippet:        First ~300 chars of the content body.
        campaign_topic: The campaign's core topic string.

    Returns:
        True if the content is primarily about the campaign topic (or on
        any error), False if the content merely mentions it in passing.
    """
    if not campaign_topic or not campaign_topic.strip():
        log.debug("topic_coherence_skip", reason="empty_campaign_topic")
        return True

    coherence_prompt = (
        f"Is this content PRIMARILY about {campaign_topic}? "
        f"Title: {title}. "
        f"Snippet: {snippet[:300]}. "
        "Answer only YES or NO."
    )

    try:
        # Lazy init — safe under Gunicorn pre-fork
        init_vertex()

        from vertexai.generative_models import (  # type: ignore[import]
            GenerativeModel,
            GenerationConfig,
        )

        model = GenerativeModel(_GEMINI_MODEL)
        config = GenerationConfig(
            temperature=0.1,
            max_output_tokens=8,
        )
        response = model.generate_content(coherence_prompt, generation_config=config)
        # P0-5: Guard response.text access — fail-open on empty candidates
        if not response.candidates:
            log.warning(
                "topic_coherence_empty_candidates",
                campaign_topic=campaign_topic,
                title=title[:80],
                note="Gemini returned no candidates. Failing open.",
            )
            return True
        answer = response.text.strip().upper()
        is_coherent = answer.startswith("YES")

        log.info(
            "topic_coherence_result",
            campaign_topic=campaign_topic,
            title=title[:80],
            answer=answer,
            is_coherent=is_coherent,
        )
        return is_coherent

    except Exception as exc:
        # Fail-open: never block the pipeline on a coherence check failure
        log.warning(
            "topic_coherence_error_failopen",
            campaign_topic=campaign_topic,
            title=title[:80],
            error=str(exc),
        )
        return True


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

    prompt = f"""CONFIDENCE TIERING GATE: Evaluate each URL snippet as an investigative OSINT engine against the user's business context.
USER BIO: '{bio}'
TARGET LOCATION: '{location_target}'

# STEP 1 — OSINT SCORING MATRIX
Evaluate the snippets purely on RAW INTENT and SYMPTOMS, ignoring corporate polish.

High Confidence: Raw, unpolished footprints. This includes niche forum complaints, municipal PDFs, unoptimized local business pages, or direct expressions of pain/need that match the USER BIO. "Ugly" is good if the intent is strong. Also includes community posts, Reddit threads, Slack/Discord exports, LinkedIn comments — where a PRACTITIONER is actively venting a problem they are experiencing RIGHT NOW.

Medium Confidence: Ambiguous intent, but highly relevant industry or location. Includes: news articles about company challenges, job posts implying a capability gap, product reviews expressing frustration.

Low Confidence: SEO-optimised listicles, "Top 10" posts, how-to guides, directories (Yelp, G2, etc.), generic educational articles, or clear competitors/vendors SELLING the same service as in the USER BIO.

# STEP 2 — UNIVERSAL RULES
SOCIAL PLATFORM RULE: Evaluate the SPECIFIC POST intent, not the platform's general purpose.
GEO RULE: Wrong region → Low.
COMPETITOR RULE: If the snippet belongs to a vendor SELLING the same service as the USER BIO, classify it as Low.

# STEP 3 — CRITICAL: B2B BUYER FORUM EXCEPTION (V24.5.3)
MARKETING BLOG vs BUYER FORUM: Do NOT classify as Low merely because a URL domain is marketing-related.
A practitioner COMPLAINING about a marketing problem is a HIGH-CONFIDENCE BUYER, not a blog.
These are HIGH regardless of domain:
- "We've been through 3 brand agencies and still can't get consistent messaging" (Reddit/forum)
- "Our attribution data has been completely wrong since the iOS update" (LinkedIn comment/community)
- "Fed up with our marketing automation — the lead scoring is broken" (forum post)
- "We tried HubSpot and Marketo and neither solved our ROI tracking problem" (community)
These speakers are experiencing pain with budget to solve it.

These are LOW (look similar but are NOT buyer signals):
- "5 ways to improve your brand narrative in 2024" (listicle — no buyer present)
- "How to fix attribution tracking" (how-to guide — educational, not a buyer complaint)
- "Marketing automation compared: HubSpot vs Marketo" (vendor comparison article)

# STEP 4 — CONTEXT-AWARE INFERENCE (B2C/D2C)
For B2C or D2C campaigns, the snippet field may contain text prepended with 'Query: <the triggering search query>'. Use the triggering query context to reverse-engineer the thread state. If the query contains dialog dorks (like "pm me" or "still available"), analyze whether the snippet contains replies suggesting active consumer/buyer intent. Do not automatically classify forum posts or social media snippets as Low if the query context indicates an active B2C/D2C discussion thread.

Snippets: {json.dumps(snippets)}"""


    try:
        tiered = call_gemini_2_5(prompt, expect_json=True, response_schema=_TIERING_SCHEMA)
        if not isinstance(tiered, list):
            raise ValueError("Expected list from tiering gate")
    except Exception as exc:
        # SF-010 FIX Hardening: Local heuristic fallback to prevent listicle/directory spills.
        log.warning(
            "pre_filter_gemini_failed_local_fallback",
            error=str(exc),
            url_count=len(snippets),
            action="Running local heuristic fallback filter to drop obvious noise.",
        )
        fallback_high = []
        noise_signatures = {
            "yelp.com", "expertise.com", "g2.com", "capterra.com", "upwork.com",
            "glassdoor.com", "indeed.com", "linkedin.com/jobs", "quora.com",
            "wikipedia.org", "amazon.com", "zoominfo.com", "crunchbase.com",
            "/blog/", "/article/", "/post/", "/best-", "/top-", "/vs/", "/compare/"
        }
        for s in snippets:
            link = s.get("link", s.get("url", ""))
            if link and link.startswith("http"):
                link_lower = link.lower()
                if not any(sig in link_lower for sig in noise_signatures):
                    fallback_high.append(link)
        return {"High": fallback_high, "Medium": []}

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
# Inline signal scorer (V25.1.0 — Signal Harvest pipeline)
# ---------------------------------------------------------------------------
#
# pre_filter_gemini() scores Serper snippets (140 chars) → high rejection rate
# because there is insufficient content for intent classification.
#
# inline_score_signal() scores FULL content from Reddit/HN/RSS/PRISM — the
# actual words the buyer wrote, not a search-engine summary of them. This is
# the correct design: Gemini sees what the buyer said, not what Google said
# about what the buyer said.
#
# Used by signal_harvest.py exclusively. dispatch.py still uses pre_filter_gemini
# for backward compatibility with the Serper pathway.
# ---------------------------------------------------------------------------

_INLINE_SCORE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tier": {
            "type":        "STRING",
            "enum":        ["HIGH", "MEDIUM", "LOW"],
            "description": "Intent confidence tier.",
        },
        "pain_summary": {
            "type":        "STRING",
            "description": "1-2 sentence summary of the specific pain or need expressed.",
        },
        "contact_point": {
            "type":        "STRING",
            "description": "How to reach this person: username, profile URL, email, or empty if not determinable.",
        },
        "buyer_language_quote": {
            "type":        "STRING",
            "description": "Exact quote from the signal text that most strongly indicates buying intent.",
        },
        "geo_match": {
            "type":        "BOOLEAN",
            "description": "True if the signal's geographic context matches the campaign's geo target.",
        },
        "archetype_match": {
            "type":        "STRING",
            "description": "Which archetype this signal best matches: B2B, B2C, D2C, B2B2C, or NONE.",
        },
        "rejection_reason": {
            "type":        "STRING",
            "description": "If tier is LOW, explain why. Empty for HIGH/MEDIUM.",
        },
        "topic_coherence": {
            "type":        "NUMBER",
            "description": "0.0-1.0 — how strongly the content is PRIMARILY about the campaign topic vs. mentioning it incidentally.",
        },
    },
    "required": [
        "tier", "pain_summary", "contact_point",
        "buyer_language_quote", "geo_match", "archetype_match",
    ],
}


def inline_score_signal(
    signal_text: str,
    icp_context: str,
    source_url: str,
    source_type: str,
    geo_target: str,
    archetype: str,
    buyer_language_context: str = "",
) -> dict:
    """Score full signal content against campaign ICP.

    Unlike pre_filter_gemini which uses Serper snippets (140 chars),
    this function receives the FULL text of a signal — the complete
    Reddit post body, Hacker News story, RSS article, or job description.

    Args:
        signal_text:            Full text content of the signal (post/article/job).
        icp_context:            Campaign ICP context from context_builder.
        source_url:             Original signal URL (for context).
        source_type:            Source identifier ("reddit", "hackernews", etc.).
        geo_target:             Campaign geographic target (may be empty for global).
        archetype:              Campaign archetype (B2B, B2C, D2C, B2B2C).
        buyer_language_context: Optional Gemini-derived buyer language summary
                                from source_router for extra context.

    Returns:
        Dict with keys: tier, pain_summary, contact_point, buyer_language_quote,
        geo_match, archetype_match, rejection_reason.
        Returns LOW tier dict on any error.
    """
    if not signal_text or not signal_text.strip():
        return {
            "tier":                "LOW",
            "pain_summary":        "",
            "contact_point":       "",
            "buyer_language_quote": "",
            "geo_match":           False,
            "archetype_match":     "NONE",
            "rejection_reason":    "Empty signal text",
        }

    # Truncate to avoid context window overflow (Gemini 2.5 Flash = 1M tokens,
    # but we budget 6000 chars per signal to keep batch costs reasonable)
    truncated = signal_text[:6000]
    if len(signal_text) > 6000:
        truncated += "\n... [SIGNAL TRUNCATED — only first 6000 chars shown]"

    geo_instruction = (
        f"Target geography: {geo_target}. "
        f"If the signal is not relevant to {geo_target}, set geo_match=false and tier=LOW."
        if geo_target
        else "Geography: Global — geo_match=true unless signal is clearly irrelevant to a specific locale that contradicts the ICP."
    )

    buyer_language_hint = (
        f"\n\nBUYER LANGUAGE CONTEXT (from source router):\n{buyer_language_context}"
        if buyer_language_context
        else ""
    )

    prompt = f"""You are an OSINT intent analyst for a lead generation platform.

CAMPAIGN ICP (Ideal Customer Profile):
{icp_context}

CAMPAIGN ARCHETYPE: {archetype}
{geo_instruction}

SOURCE TYPE: {source_type.upper()} — {source_url[:120]}
{buyer_language_hint}

FULL SIGNAL CONTENT:
{truncated}

TASK:
Analyze the full signal content above. Determine whether this signal represents an
active buyer who matches the ICP and is currently experiencing a pain that the
campaign can address.

SCORING RULES:
HIGH tier: The signal contains explicit buyer intent — someone asking for a vendor
  recommendation, expressing frustration with a current solution, announcing a budget
  or project that matches the ICP, or describing a pain that is directly addressable.
  The person/company is identifiable and reachable.
  ALSO HIGH: A Google Maps review where the REVIEWER describes their own use case,
  pain, or project in a way that matches the ICP — the reviewer IS a proven buyer
  in this category (they already spent money on a competitor).

MEDIUM tier: The signal shows strong contextual relevance to the ICP but lacks explicit
  buying intent. Could be a decision influencer, a company going through a relevant
  change, or a topic discussion from the ICP audience.
  ALSO MEDIUM: A positive review on a competitor where the reviewer mentions their
  business type, use case, or project context that matches the ICP, even without
  explicit dissatisfaction.

LOW tier: The signal is a generic article, listicle, corporate announcement unrelated
  to buying, spam, or clearly irrelevant to the ICP.
  A review is LOW only if: (a) it contains no useful buyer context (e.g. just "Great
  service" with no detail), (b) the reviewer is clearly outside the ICP geo/category,
  or (c) the review is from a competitor/seller, not a buyer.

SCORING CALIBRATION (use these anchors when estimating the topic_coherence field):
- 0.9-1.0: Person explicitly states intent to buy/hire within 30 days
- 0.7-0.8: Actively researching, comparing options, asking for recommendations
- 0.5-0.6: Mentions topic but no clear buying signal
- 0.3-0.4: Tangentially related, no individual buyer
- 0.1-0.2: News, editorial, academic, or irrelevant
If content is news/megathread/editorial mentioning topic in passing, score topic_coherence 0.1-0.3.

TOPIC COHERENCE FIELD:
Set topic_coherence (0.0-1.0) to reflect how PRIMARILY the content is about the
campaign topic. 1.0 = entirely focused on the campaign topic with clear buyer intent.
0.0 = campaign topic is not present at all. Content that merely name-drops the topic
in a news roundup or editorial listicle should receive 0.1-0.3.

GEO RULE: If a geo_target is set and the signal is clearly from/about a different
  geography, set geo_match=false and tier=LOW.

SELLER EXCLUSION: If the signal author/company is a direct competitor (sells the same
  product/service described in the ICP), set tier=LOW.

Return a single JSON object matching the schema."""

    try:
        result = call_gemini_2_5(
            prompt,
            expect_json=True,
            response_schema=_INLINE_SCORE_SCHEMA,
        )
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response type: {type(result)}")

        # Normalise tier to uppercase
        result["tier"] = str(result.get("tier", "LOW")).upper()
        if result["tier"] not in ("HIGH", "MEDIUM", "LOW"):
            result["tier"] = "LOW"

        log.info(
            "inline_score_complete",
            tier=result["tier"],
            source_type=source_type,
            url=source_url[:80],
            geo_match=result.get("geo_match", False),
        )
        return result

    except Exception as exc:
        log.warning(
            "inline_score_failed",
            source_url=source_url[:80],
            source_type=source_type,
            error=str(exc),
        )
        return {
            "tier":                "LOW",
            "pain_summary":        "",
            "contact_point":       "",
            "buyer_language_quote": "",
            "geo_match":           False,
            "archetype_match":     "NONE",
            "rejection_reason":    f"Scoring error: {exc}",
            "topic_coherence":     0.0,
        }


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
        "score_reasoning":              {"type": "STRING"},
        "confidence_level":             {"type": "STRING", "enum": ["HIGH", "MEDIUM", "SPECULATIVE"]},
        "evidence_chain": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "signal_type": {
                        "type": "STRING",
                        "enum": ["PAIN_EXPRESSION", "HIRING_INTENT", "COMPETITOR_CHURN",
                                 "TECH_STACK_MATCH", "COMMUNITY_MENTION", "REVIEW_SIGNAL",
                                 "FUNDING_EVENT", "GENERAL_FIT"],
                    },
                    "evidence": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["signal_type", "evidence", "confidence"],
            },
        },
    },
    "required": [
        "matched_campaigns", "dm", "pain_point", "icebreaker_angle",
        "intent_signal", "hiring_intent_found", "tech_stack_found",
        "contact_endpoints", "decision_maker_name", "decision_maker_title",
        "company_size_tier", "primary_objection_hypothesis",
        "score_reasoning", "confidence_level", "evidence_chain",
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

SELLER EXCLUSION RULE: If the target company sells or advertises B2B lead generation, cold email marketing, B2B databases/data scraping, or outbound agency/sales services themselves, they are a competitor. You MUST score them 0 (or <4) for any campaign, excluding them.

GENERIC B2B RULE: Simply presenting standard services publicly (e.g. software development, IT services, consulting, agency work) does not indicate active buying intent. Grade them strictly as GENERAL_FIT with a score <= 3 (which will exclude them), unless there is a specific active intent signal (e.g., job postings for SDRs/sales, or active complaints).

# STEP 2 — OUTREACH COPILOT DRAFT
Identify the campaign with the HIGHEST match score.
{'OSINT Community tone: Ultra-casual, empathetic, observational. Acknowledge their specific situation or complaint. Max 3 sentences, end with a soft, low-friction question.' if is_social else 'OSINT Discovery tone: Direct, highly contextual, and observant. Reference the specific operational footprint or symptom you found on their site. Speak operator-to-operator. Zero generic corporate fluff.'}

# STEP 3 — EXTRACTION RULES
- hiring_intent_found: ONLY 'Yes' or 'No'.
- contact_endpoints: Only explicitly present contacts. URI must have full protocol prefix (https://).
- PHONE DEDUPLICATION: Max 2 numbers.
{social_uri_rule}

# STEP 4 — EVIDENCE DOSSIER
For each piece of evidence you used to score this lead, create an evidence_chain entry:
- signal_type: classify as PAIN_EXPRESSION, HIRING_INTENT, COMPETITOR_CHURN, TECH_STACK_MATCH, COMMUNITY_MENTION, REVIEW_SIGNAL, FUNDING_EVENT, or GENERAL_FIT
- evidence: the exact quote or fact from the text (max 100 chars)
- confidence: 0.0-1.0 how confident you are this signal is real
Also provide:
- score_reasoning: 1-2 sentences explaining WHY this lead scored the way it did
- confidence_level: HIGH (multiple converging signals), MEDIUM (clear single signal), SPECULATIVE (weak/indirect signals)

CONTEXTUAL DORKING DATA:
{context_payload}

DETECTED TECH STACK:
{', '.join(tech_stack) if tech_stack else 'None extracted'}
"""
    if historical_dms:
        prompt += f"\nPast successful converted messages (match tone): {historical_dms}\n"
    # Token limit protection: truncate raw web text to prevent context window or credit overflow
    max_chars = int(os.environ.get("MAX_SCRAPED_TEXT_CHARS", 50000))
    truncated_text = text if len(text) <= max_chars else text[:max_chars] + "\n... [TRUNCATED DUE TO SIZE LIMIT] ..."
    prompt += f"\nText DOM:\n{truncated_text}"

    sys_inst = (
        "You are a Dynamic Intent Analyzer and OSINT Lead Profiler. "
        "\n\nCRITICAL RULE — CONTEXTUAL DISCOVERY: "
        "You are evaluating leads that were likely found via raw web footprints "
        "(PDFs, forums, unoptimized sites). Score purely on the intensity of the "
        "pain point or operational signal."
        "\n\nOUTREACH RULE: "
        "Draft the DM to sound like a natural, serendipitous discovery. "
        "Acknowledge the specific context of where/how you found them without "
        "sounding intrusive. Never hallucinate contacts."
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
                "score_reasoning": data.get("score_reasoning", ""),
                "confidence_level": data.get("confidence_level", "SPECULATIVE"),
                "evidence_chain": data.get("evidence_chain", []),
            }

        matched.sort(key=lambda x: x.get("raw_score", 0), reverse=True)
        base_score       = float(matched[0].get("raw_score", 0))
        highest_campaign = matched[0].get("campaign_id", "Unknown")
        matched_ids      = [str(c.get("campaign_id")) for c in matched]

        # Postmortem Fix #10: reduced multiplier table.
        # Old table {2: 1.3, else: 1.6} inflated base-6 leads → 9.6 (hot-lead alert).
        # New table caps at 1.3× for 4+ campaigns. A base-6 lead scores max 7.8 → 7,
        # staying below the WhatsApp trigger (>=8). Genuine 9+ leads still reach 10.
        multiplier  = {1: 1.0, 2: 1.05, 3: 1.1}.get(len(matched), 1.15)
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
            "score_reasoning": data.get("score_reasoning", ""),
            "confidence_level": data.get("confidence_level", "SPECULATIVE"),
            "evidence_chain": data.get("evidence_chain", []),
        }

    except Exception as exc:
        raise ValueError(f"LLM Parsing Failure: {exc}") from exc
