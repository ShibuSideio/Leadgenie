"""
Orchestrator — Inbound Sentiment Service.

Detects inbound sales signals from across the public web using:
  - Serper API (organic Google Search — no platform OAuth required)
  - Gemini Flash (intent classification + scoring)

Query strategy:
  - ZERO site: restrictions — Google ranks the best source anywhere on the web
  - 7-mode round-robin rotation (one mode per day of week)
  - Each mode targets a different buying signal type
  - Negative keyword filter strips known garbage (ad copy, legal pages)
  - Gemini drops results with intent_score < 0.30 (Layer 2 garbage filter)

V23.5 — added 2026-06-08
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime
from typing import Optional

import httpx

from core.clients import get_secret_manager_client  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]
from core.config import PROJECT_ID  # type: ignore[import]

log = get_logger("orchestrator.inbound_sentiment")

# ---------------------------------------------------------------------------
# Serper key — fetched from Secret Manager once, cached per process
# Same secret name as pipeline-main uses: serper_api_key
# ---------------------------------------------------------------------------
_serper_key_cache: Optional[str] = None
_serper_key_lock = threading.Lock()

SERPER_URL = "https://google.serper.dev/search"


def _get_serper_key() -> str:
    global _serper_key_cache
    if _serper_key_cache:
        return _serper_key_cache
    with _serper_key_lock:
        if _serper_key_cache:
            return _serper_key_cache
        secret_name = (
            os.environ.get("SERPER_API_KEY_SECRET")
            or f"projects/{PROJECT_ID}/secrets/serper_api_key/versions/latest"
        )
        try:
            sm = get_secret_manager_client()
            resp = sm.access_secret_version(request={"name": secret_name})
            _serper_key_cache = resp.payload.data.decode("UTF-8").strip()
            return _serper_key_cache
        except Exception as exc:
            raise RuntimeError(f"Cannot fetch Serper key from Secret Manager: {exc}") from exc


# ---------------------------------------------------------------------------
# Platform detection (for signal metadata — NOT for query filtering)
# ---------------------------------------------------------------------------
_PLATFORM_RE: list[tuple[str, re.Pattern]] = [
    ("reddit",    re.compile(r"reddit\.com",   re.I)),
    ("linkedin",  re.compile(r"linkedin\.com", re.I)),
    ("quora",     re.compile(r"quora\.com",    re.I)),
    ("g2",        re.compile(r"g2\.com",       re.I)),
    ("capterra",  re.compile(r"capterra\.com", re.I)),
    ("glassdoor", re.compile(r"glassdoor\.com",re.I)),
    ("hn",        re.compile(r"news\.ycombinator\.com", re.I)),
    ("news",      re.compile(
        r"(techcrunch|businesswire|prnewswire|forbes|venturebeat|theregister)",
        re.I,
    )),
]


def _detect_platform(url: str) -> str:
    for name, pat in _PLATFORM_RE:
        if pat.search(url):
            return name
    return "web"


def _clean_query_syntax(raw: str) -> str:
    """Optimize spacing and sanitize wildcard domain operators in queries.

    Ensures proper space separation before opening parentheses and replaces
    unsupported wildcard domains (site:*.org -> site:.org).
    """
    if not raw:
        return ""
    # 1. Strip wildcard domain prefix site:*. -> site:.
    res = re.sub(r'(?<!\w)site:\*\.', 'site:.', raw)
    
    # 2. Insert missing space between quotes and opening parenthesis: "abc"(xyz) -> "abc" (xyz)
    res = re.sub(r'(?<=\")\(', ' (', res)

    # 3. Insert missing space between alphanumeric/dots/hyphens and opening parenthesis: net(xyz) -> net (xyz)
    res = re.sub(r'([a-zA-Z0-9\.\-_])\(', r'\1 (', res)
    
    # 4. Insert missing space between closing and opening parenthesis: )( -> ) (
    res = re.sub(r'\)(?=\()', ') ', res)
    
    return res


# ---------------------------------------------------------------------------
# 7-Mode signal rotation — one mode selected by day_of_week (0=Mon … 6=Sun)
# All templates are platform-agnostic (no site: operators)
# ---------------------------------------------------------------------------
SIGNAL_MODES: dict[int, dict] = {
    0: {
        "name": "active_intent",
        "templates": [
            'site:reddit' + '.com/r/sales OR site:reddit' + '.com/r/startups "{pain_keyword}" "looking for" OR "recommend" OR "any thoughts"',
            'site:reddit' + '.com OR site:quora' + '.com "{pain_keyword}" "looking for tool" OR "software suggestion" OR "what do you use"',
            '"{pain_keyword}" inurl:forum OR inurl:community "help" OR "RFP" OR "vendor"',
            'site:reddit' + '.com OR site:quora' + '.com "{industry}" "we need" OR "best tool for"',
        ],
    },
    1: {
        "name": "pain_expression",
        "templates": [
            'site:reddit' + '.com OR site:quora' + '.com "{pain_keyword}" "struggling" OR "frustrated" OR "nightmare" OR "broken process"',
            'site:reddit' + '.com OR site:quora' + '.com "{pain_keyword}" "wasting time" OR "inefficient" OR "no visibility"',
            '"{pain_keyword}" inurl:forum OR inurl:community "manually" OR "spreadsheet" OR "failing"',
            'site:reddit' + '.com OR site:quora' + '.com "{industry}" "problem" OR "issue" OR "failing"',
        ],
    },
    2: {
        "name": "competitor_churn",
        "templates": [
            'site:reddit' + '.com OR site:quora' + '.com OR site:twitter' + '.com "{competitor}" "alternative" OR "switch" OR "better than"',
            'site:reddit' + '.com OR site:quora' + '.com OR site:twitter' + '.com "{competitor}" "cancel" OR "leaving" OR "scam" OR "disappointed"',
            'site:trustpilot' + '.com/review/ OR site:g2' + '.com/products/*/reviews "{competitor}" "worst" OR "bad" OR "useless" OR "billing"',
            'site:reddit' + '.com OR site:quora' + '.com "{industry}" "looking for alternative to" OR "fed up with"',
        ],
    },
    3: {
        "name": "hiring_signals",
        "templates": [
            '"{industry}" "hiring" "{icp_job_title}"',
            '"{company_type}" "Series A" OR "Series B" OR "raised" "{industry}"',
            '"{industry}" "expanding" OR "growing team" OR "new office"',
            '"{pain_keyword}" "scale" OR "growing pains" OR "outgrown"',
        ],
    },
    4: {
        "name": "review_signals",
        "templates": [
            'site:trustpilot' + '.com/review/ OR site:g2' + '.com/products/*/reviews "{pain_keyword}" "wish it had" OR "missing feature" OR "not satisfied"',
            'site:trustpilot' + '.com/review/ OR site:g2' + '.com/products/*/reviews "{pain_keyword}" "worst experience" OR "terrible support" OR "regret"',
            'site:mouthshut' + '.com OR site:consumercomplaints' + '.in "{pain_keyword}" "complaint" OR "scam" OR "waste of money"',
            'site:g2' + '.com/products/*/reviews OR site:capterra' + '.com "{pain_keyword}" "alternatives" OR "compare" OR "wish"',
        ],
    },
    5: {
        "name": "trend_signals",
        "templates": [
            'site:reddit' + '.com OR site:quora' + '.com "{industry}" "digital transformation" OR "modernize" OR "automate"',
            '"{pain_keyword}" trend 2025 OR 2026',
            'site:reddit' + '.com OR site:quora' + '.com "{industry}" "cost reduction" OR "efficiency" OR "ROI"',
            '"{industry}" "new regulation" OR "compliance" OR "mandate"',
        ],
    },
    6: {
        "name": "community_signals",
        "templates": [
            'site:reddit' + '.com/r/sales OR site:reddit' + '.com/r/marketing "{pain_keyword}" "how do you" OR "our stack"',
            'site:reddit' + '.com/r/startups OR site:reddit' + '.com/r/SaaS "{pain_keyword}" "what tools" OR "recommendation"',
            '"{pain_keyword}" inurl:forum OR inurl:community "help" OR "discussion"',
            '"{industry}" association OR conference OR "best practice" 2025',
        ],
    },
}

# Appended to EVERY query — strips known garbage before results hit Gemini
GLOBAL_NEGATIVE = (
    ' -directory -listicle -"top 10" -"best" -wiki -jobs -careers -support -"login" '
    '-"buy now" -"click here" -"sign up free" -"privacy policy"'
)


# ---------------------------------------------------------------------------
# Gemini scoring
# ---------------------------------------------------------------------------

def _score_with_gemini(title: str, snippet: str, url: str, query: str, icp_description: str) -> Optional[dict]:
    """
    Call Gemini Flash to classify the intent of a single search result.
    Returns a scored dict or None if intent_score < 0.30.

    Intent labels:
      ACTIVE_SEEKING   — explicitly looking for a solution / asking for recommendations
      EXPRESSING_PAIN  — describes a problem; not yet searching for solutions
      COMPETITOR_CHURN — mentions switching from / dissatisfied with a competitor
      TREND            — general topic discussion; no personal buyer intent
    """
    if not snippet:
        return None

    prompt = f"""You are an inbound OSINT intent classifier.
Classify the raw buying intent or operational pain expressed in this public web content for a system solving: {icp_description}

Triggering Google Query: {query}
Title: {title}
Snippet: {snippet}
URL: {url}

Respond with ONLY a JSON object (no markdown fences):
{{
  "intent_label": "ACTIVE_SEEKING" | "EXPRESSING_PAIN" | "COMPETITOR_CHURN" | "TREND" | "NONE",
  "intent_score": <float 0.0 to 1.0>,
  "matched_pain_keywords": ["keyword1", "keyword2"],
  "company_name": "<company name if detectable, else null>",
  "industry_hint": "<industry if detectable, else null>",
  "reasoning": "<one sentence>"
}}

Classification rules (OSINT Focus):
- ACTIVE_SEEKING  (0.75-1.0): Explicitly looking for a solution, asking for help on a forum/board.
- COMPETITOR_CHURN(0.70-0.95): Complaining about a current tool/service, frustrated with a provider.
- EXPRESSING_PAIN (0.40-0.75): Venting about a raw operational problem, symptom, or inefficiency.
- TREND           (0.10-0.45): General market discussion; no personal pain expressed.
- NONE            (0.0 -0.29): Polished marketing copy, SEO articles, directories, or irrelevant noise.

CONTEXT-AWARE CONVERSATIONAL INFERENCE:
Analyze the Google snippet in relation to the triggering query. If the query contains dialog dorks (like "pm me" or "still available"), use the snippet's metadata, title, and conversational phrases to reverse-engineer the thread's state. If a forum reply or social comment indicates buying intent, or the thread contextually addresses a direct need matching the USER BIO, classify it as ACTIVE_SEEKING or EXPRESSING_PAIN. Do not drop threads solely because they are forums/social posts.

SELLER EXCLUSION RULE: If the content represents a provider, competitor, broker, agent, vendor, or seller offering the same or similar services as described in the system solver description (e.g., real estate agents/brokers in property campaigns, immigration agencies in visa/study campaigns, or lead generation agencies in outbound sales campaigns), you MUST classify them as NONE with an intent_score of 0.0. Do not capture competitors.

INFORMATIONAL FILTER: General educational articles, blogs, listicles, directories, comparisons, news stories, and guides that do NOT contain a direct complaint, support ticket, or active buying query from a specific individual or company must be classified as NONE with an intent_score of 0.0."""
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig
        from core.clients import init_vertex  # type: ignore[import]
        init_vertex()

        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        model = GenerativeModel(model_name)
        resp = model.generate_content(
            prompt,
            generation_config=GenerationConfig(temperature=0.05, max_output_tokens=512),
        )
        raw = resp.text.strip()
        # Strip markdown code fences if model wraps output
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        scored = json.loads(raw)

        raw_score = float(scored.get("intent_score", 0))
        log.info(
            "trace_inbound_raw_score",
            url=url[:80],
            score=raw_score,
            label=scored.get("intent_label"),
            reasoning=scored.get("reasoning", "")
        )

        if scored.get("intent_label") == "NONE" or raw_score < 0.30:
            return None

        return {
            "intent_label":          scored.get("intent_label", "EXPRESSING_PAIN"),
            "intent_score":          round(float(scored.get("intent_score", 0.5)), 3),
            "pain_keywords":         scored.get("matched_pain_keywords", []),
            "company_name":          scored.get("company_name"),
            "industry_hint":         scored.get("industry_hint"),
            "gemini_reasoning":      scored.get("reasoning", ""),
        }
    except Exception as exc:
        log.warning("gemini_scoring_failed", url=url[:80], error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Main service class
# ---------------------------------------------------------------------------

class InboundSentimentService:
    """
    Platform-agnostic inbound sales signal detector.

    Usage:
        svc = InboundSentimentService(persona=persona_dict, campaign=campaign_dict)
        signals = svc.run()   # list of signal dicts sorted by intent_score desc
    """

    def __init__(self, persona: dict, campaign: dict, force_day_of_week: Optional[int] = None):
        self.persona       = persona
        self.campaign      = campaign
        self.force_day_of_week = force_day_of_week
        self.pain_kws      = [str(p) for p in (persona.get("pain_points") or [])[:5]]
        self.industry      = str(persona.get("industry") or "B2B software")
        self.competitors   = [str(c) for c in (persona.get("competitors") or [])[:3]]
        self.icp_desc      = str(
            persona.get("icp_description")
            or persona.get("persona_description")
            or self.industry
        )
        self.icp_job_title = str(persona.get("icp_job_title") or "operations manager")
        self.company_type  = str(persona.get("company_type") or "company")

    # ------------------------------------------------------------------
    # Query building
    # ------------------------------------------------------------------

    def _build_queries(self) -> list[str]:
        """Build today's search queries using a blended daily rotation mode.
        
        Blends queries across multiple core intent modes (Active Intent, Competitor Churn,
        Review Signals, and Community Signals) to prevent temporal lag while capping the
        total number of queries to keep Serper credit costs constant and low.
        """
        day_of_week = datetime.utcnow().weekday() if self.force_day_of_week is None else self.force_day_of_week
        primary_mode = SIGNAL_MODES[day_of_week]

        subs_base = {
            "industry":      self.industry,
            "competitor":    self.competitors[0] if self.competitors else "legacy tool",
            "icp_job_title": self.icp_job_title,
            "company_type":  self.company_type,
        }

        # Select a blended mix of templates
        selected_templates: list[str] = []
        
        # 1. First 2 templates from primary mode of the day
        selected_templates.extend(primary_mode["templates"][:2])
        
        # 2. Blend with 1 template from 2 other core intent modes
        # Core modes: 0 (active), 1 (pain), 2 (churn), 4 (reviews)
        core_days = [0, 1, 2, 4]
        if day_of_week in core_days:
            core_days.remove(day_of_week)
        # Take 1 template from the first two other core modes
        for d in core_days[:2]:
            other_mode = SIGNAL_MODES[d]
            if other_mode["templates"]:
                selected_templates.append(other_mode["templates"][0])

        sourcing_vector = self.campaign.get("sourcing_vector", "")
        is_consumer = (sourcing_vector or "").upper().strip() in {"B2C", "B2B2C", "D2C"}
        dialog_suffix = ' ("pm me" OR "pm sent" OR "still available" OR "send details" OR "anyone know")' if is_consumer else ''

        queries: list[str] = []
        for pain_kw in self.pain_kws[:3]:  # Cap pain keywords count to protect Serper budget
            subs = {**subs_base, "pain_keyword": pain_kw}
            for template in selected_templates:
                try:
                    q = template.format(**subs) + dialog_suffix + GLOBAL_NEGATIVE
                    queries.append(q)
                except KeyError:
                    pass

        # Unconstrained catch-all — surfaces long-tail sources Google knows about
        for pain_kw in self.pain_kws[:2]:
            queries.append(f'"{pain_kw}" "{self.industry}"' + dialog_suffix + GLOBAL_NEGATIVE)

        log.info(
            "inbound_queries_built",
            mode=primary_mode["name"],
            day=day_of_week,
            blended_templates=len(selected_templates),
            count=len(queries),
        )
        return list(dict.fromkeys(queries))  # deduplicate, preserve order

    # ------------------------------------------------------------------
    # Serper search
    # ------------------------------------------------------------------

    def _search_serper(self, query: str, num: int = 5) -> list[dict]:
        """Execute a single Serper search. Returns list of organic result dicts."""
        query = _clean_query_syntax(query)
        gl = self.campaign.get("gl") or "us"
        location = self.campaign.get("location")
        
        # V24.1.23: Restrict inbound sweeps to the past year to prevent cold/stale historical leads.
        # Allow override via campaign.inbound_timeframe (defaults to "qdr:y").
        timeframe = self.campaign.get("inbound_timeframe") or "qdr:y"
        payload = {"q": query, "num": num, "gl": gl, "hl": "en"}
        if timeframe and timeframe != "all":
            payload["tbs"] = timeframe

        if location:
            payload["location"] = location

        try:
            resp = httpx.post(
                SERPER_URL,
                headers={
                    "X-API-KEY":    _get_serper_key(),
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json().get("organic", [])
        except Exception as exc:
            log.warning("serper_call_failed", query=query[:80], error=str(exc))
            return []

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def _is_noise_url(self, url: str) -> bool:
        """Pre-screens a URL to check if it matches directory, listicle, or competitor signatures."""
        url_lower = url.lower()

        # 1. Directory and review aggregators blocklist
        noise_domains = {
            "yelp.com", "expertise.com", "g2.com", "capterra.com", "upwork.com",
            "glassdoor.com", "indeed.com", "linkedin.com/jobs", "quora.com",
            "wikipedia.org", "amazon.com", "zoominfo.com", "crunchbase.com",
            "trustpilot.com/review"
        }
        if any(domain in url_lower for domain in noise_domains):
            return True

        # 2. Competitors URL check
        for comp in self.competitors:
            comp_clean = comp.lower().strip().replace(" ", "")
            if comp_clean and len(comp_clean) > 3 and comp_clean in url_lower:
                return True

        # 3. Social Media Bypass: Skip blog/listicle path patterns for major social hubs
        social_domains = {
            "facebook.com", "reddit.com", "twitter.com", "x.com", "quora.com",
            "news.ycombinator.com"
        }
        if any(social in url_lower for social in social_domains):
            return False

        # 4. Path keywords indicating listicles, blogs, and other non-footprint pages
        noise_patterns = [
            r"/blog/", r"/article/", r"/post/", r"/best-", r"/top-", r"/vs/",
            r"/compare/", r"/pricing", r"/login", r"/signup", r"/careers", r"/jobs"
        ]
        if any(re.search(pat, url_lower) for pat in noise_patterns):
            return True

        return False

    def run(self, max_queries: int = 12, results_per_query: int = 5) -> list[dict]:
        """
        Full pipeline: build queries → search → Gemini score → return signals.

        Returns:
            List of signal dicts sorted by intent_score descending.
            Each dict has stable signal_id (SHA-256 of URL[:16]).
        """
        queries   = self._build_queries()[:max_queries]
        signals   = []
        seen_urls: set[str] = set()

        for query in queries:
            for result in self._search_serper(query, num=results_per_query):
                url = result.get("link", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                # Pre-screen URL to drop obvious lists, blogs, or competitors
                if self._is_noise_url(url):
                    log.info("inbound_url_pre_screen_filtered", url=url[:80])
                    continue

                title   = result.get("title", "")
                snippet = result.get("snippet", "")

                scored = _score_with_gemini(title, snippet, url, query, self.icp_desc)
                if not scored:
                    continue

                # Determine which pain_kw triggered this query
                triggering_kw = next(
                    (kw for kw in self.pain_kws if kw.lower() in query.lower()),
                    self.pain_kws[0] if self.pain_kws else "general",
                )

                signals.append({
                    "signal_id":           hashlib.sha256(url.encode()).hexdigest()[:16],
                    "source_url":          url,
                    "source_platform":     _detect_platform(url),
                    "headline":            title,
                    "snippet":             snippet[:300],
                    "serper_query":        query,
                    "triggering_keyword":  triggering_kw,
                    "matched_persona":     self.persona.get("persona_name", ""),
                    "matched_campaign_id": self.campaign.get("campaign_id", ""),
                    "week":                _week_label(),
                    "status":              "new",
                    **scored,  # intent_label, intent_score, pain_keywords, company_name, etc.
                })

        result_list = sorted(signals, key=lambda x: x["intent_score"], reverse=True)
        log.info(
            "inbound_sentiment_run_complete",
            queries_run=len(queries),
            urls_scanned=len(seen_urls),
            signals_found=len(result_list),
        )
        return result_list


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _week_label() -> str:
    """ISO year-week label e.g. '2025-W23'."""
    now = datetime.utcnow()
    return f"{now.year}-W{now.isocalendar()[1]:02d}"
