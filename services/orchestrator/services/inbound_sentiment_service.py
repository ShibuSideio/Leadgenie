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


# ---------------------------------------------------------------------------
# 7-Mode signal rotation — one mode selected by day_of_week (0=Mon … 6=Sun)
# All templates are platform-agnostic (no site: operators)
# ---------------------------------------------------------------------------
SIGNAL_MODES: dict[int, dict] = {
    0: {
        "name": "active_intent",
        "templates": [
            '"{pain_keyword}" "looking for" "tool" OR "software" OR "solution"',
            '"{pain_keyword}" "recommendation" OR "suggest" OR "what do you use"',
            '"{industry}" "we need" OR "anyone using" OR "best tool for"',
            '"{pain_keyword}" "vendor" OR "RFP" OR "evaluation"',
        ],
    },
    1: {
        "name": "pain_expression",
        "templates": [
            '"{pain_keyword}" "struggling" OR "frustrated" OR "nightmare"',
            '"{industry}" "problem" OR "issue" OR "failing" OR "broken process"',
            '"{pain_keyword}" "manually" OR "spreadsheet" OR "no visibility"',
            '"{pain_keyword}" "wasting time" OR "inefficient" OR "error-prone"',
        ],
    },
    2: {
        "name": "competitor_churn",
        "templates": [
            '"{competitor}" "alternative" OR "switch" OR "better than"',
            '"{competitor}" "cancel" OR "leaving" OR "disappointed"',
            '"{industry}" "looking for alternative to" OR "fed up with"',
            '"{pain_keyword}" "not satisfied" OR "switched from" OR "replaced"',
        ],
    },
    3: {
        "name": "hiring_signals",
        "templates": [
            '"{industry}" "hiring" "{icp_job_title}"',
            '"{company_type}" "Series A" OR "Series B" OR "raised" "{industry}"',
            '"{industry}" "expanding" OR "new office" OR "growing team"',
            '"{pain_keyword}" "scale" OR "growing pains" OR "outgrown"',
        ],
    },
    4: {
        "name": "review_signals",
        "templates": [
            '"{pain_keyword}" review comparison "pros" "cons"',
            '"{industry}" software "best" OR "top" 2024 OR 2025',
            '"{competitor}" review "wish it had" OR "missing feature"',
            '"{pain_keyword}" "G2" OR "Capterra" OR "Trustpilot" review',
        ],
    },
    5: {
        "name": "trend_signals",
        "templates": [
            '"{industry}" "digital transformation" OR "modernize" OR "automate"',
            '"{pain_keyword}" trend 2025 OR 2026',
            '"{industry}" "new regulation" OR "compliance" OR "mandate"',
            '"{industry}" "cost reduction" OR "efficiency" OR "ROI"',
        ],
    },
    6: {
        "name": "community_signals",
        "templates": [
            '"{pain_keyword}" forum OR community OR discussion "help"',
            '"{industry}" association OR conference OR "best practice" 2025',
            '"{pain_keyword}" "case study" OR "how we solved"',
            '"{industry}" "what tools" OR "how do you" OR "our stack"',
        ],
    },
}

# Appended to EVERY query — strips known garbage before results hit Gemini
GLOBAL_NEGATIVE = ' -"buy now" -"click here" -"sign up free" -"privacy policy"'


# ---------------------------------------------------------------------------
# Gemini scoring
# ---------------------------------------------------------------------------

def _score_with_gemini(title: str, snippet: str, url: str, icp_description: str) -> Optional[dict]:
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

    prompt = f"""You are an inbound B2B sales signal classifier.

Classify the buying intent of this public web content for a company selling solutions
related to: {icp_description}

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

Classification rules:
- ACTIVE_SEEKING  (0.75-1.0): Explicitly looking for a solution, asking for recommendations
- COMPETITOR_CHURN(0.70-0.95): Mentions switching from or dissatisfied with a competitor
- EXPRESSING_PAIN (0.40-0.75): Describes a problem but not yet searching for solutions
- TREND           (0.10-0.45): General discussion; no personal pain expressed
- NONE            (0.0 -0.29): Not relevant; marketing copy; news article with no pain signal
"""
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig
        from core.clients import init_vertex  # type: ignore[import]
        init_vertex()

        model = GenerativeModel("gemini-1.5-flash-001")
        resp = model.generate_content(
            prompt,
            generation_config=GenerationConfig(temperature=0.05, max_output_tokens=512),
        )
        raw = resp.text.strip()
        # Strip markdown code fences if model wraps output
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        scored = json.loads(raw)

        if scored.get("intent_label") == "NONE" or float(scored.get("intent_score", 0)) < 0.30:
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

    def __init__(self, persona: dict, campaign: dict):
        self.persona       = persona
        self.campaign      = campaign
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
        """Build today's search queries using the daily rotation mode."""
        day_of_week = datetime.utcnow().weekday()  # 0=Mon, 6=Sun
        mode = SIGNAL_MODES[day_of_week]

        subs_base = {
            "industry":      self.industry,
            "competitor":    self.competitors[0] if self.competitors else "legacy tool",
            "icp_job_title": self.icp_job_title,
            "company_type":  self.company_type,
        }

        queries: list[str] = []
        for pain_kw in self.pain_kws:
            subs = {**subs_base, "pain_keyword": pain_kw}
            for template in mode["templates"]:
                try:
                    q = template.format(**subs) + GLOBAL_NEGATIVE
                    queries.append(q)
                except KeyError:
                    # Template contains a variable not in subs — skip gracefully
                    pass

        # Unconstrained catch-all — surfaces long-tail sources Google knows about
        for pain_kw in self.pain_kws[:2]:
            queries.append(f'"{pain_kw}" "{self.industry}"' + GLOBAL_NEGATIVE)

        log.info(
            "inbound_queries_built",
            mode=mode["name"],
            day=day_of_week,
            count=len(queries),
        )
        return list(dict.fromkeys(queries))  # deduplicate, preserve order

    # ------------------------------------------------------------------
    # Serper search
    # ------------------------------------------------------------------

    def _search_serper(self, query: str, num: int = 5) -> list[dict]:
        """Execute a single Serper search. Returns list of organic result dicts."""
        try:
            resp = httpx.post(
                SERPER_URL,
                headers={
                    "X-API-KEY":    _get_serper_key(),
                    "Content-Type": "application/json",
                },
                json={"q": query, "num": num, "gl": "us", "hl": "en"},
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

                title   = result.get("title", "")
                snippet = result.get("snippet", "")

                scored = _score_with_gemini(title, snippet, url, self.icp_desc)
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
