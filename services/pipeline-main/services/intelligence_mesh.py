"""
pipeline-main — Intelligence Mesh (V24.0)

Pluggable multi-source enrichment engine. Each IntelligenceProvider
searches a specific data source for signals about a company/domain.
All providers use Serper as the underlying search engine (no new API keys).

Providers run in parallel with a 3-second total timeout.
Failing providers are silently skipped — lead is saved regardless.
"""
from __future__ import annotations

import concurrent.futures
from abc import ABC, abstractmethod
from typing import Optional

from core.logging import get_logger  # type: ignore[import]

log = get_logger("pipeline.intelligence_mesh")


class IntelligenceProvider(ABC):
    """Base class for intelligence providers."""
    name: str = "unknown"

    @abstractmethod
    def fetch_signals(self, company_name: str, domain: str,
                      serper_fn, **kwargs) -> list[dict]:
        """Fetch signal entries for a given company.
        
        Args:
            company_name: Company name (may be None).
            domain: Root domain.
            serper_fn: Callable that executes a Serper search.
                       Signature: serper_fn(query, location=None, gl=None) -> list[dict]
        
        Returns:
            List of signal dicts: {signal_type, source, evidence_text, confidence}
        """
        ...


class HiringSignalProvider(IntelligenceProvider):
    """Detect hiring activity via careers page dorks."""
    name = "hiring"

    def fetch_signals(self, company_name, domain, serper_fn, **kwargs):
        signals = []
        query = f'site:{domain} ("careers" OR "jobs" OR "hiring" OR "join our team")'
        try:
            results = serper_fn(query)
            for r in (results or [])[:3]:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                if any(kw in (title + snippet).lower() for kw in ["hiring", "career", "job", "position", "apply"]):
                    signals.append({
                        "signal_type": "HIRING_INTENT",
                        "source": r.get("link", domain),
                        "evidence_text": f"Careers page active: {snippet[:100]}",
                        "confidence": 0.65,
                    })
        except Exception as exc:
            log.warning("hiring_provider_error: domain=%s err=%s", domain, exc)
        return signals


class ReviewSignalProvider(IntelligenceProvider):
    """Find reviews on G2/Capterra."""
    name = "reviews"

    def fetch_signals(self, company_name, domain, serper_fn, **kwargs):
        signals = []
        if not company_name:
            return signals
        query = f'(site:g2.com OR site:capterra.com) "{company_name}"'
        try:
            results = serper_fn(query)
            for r in (results or [])[:2]:
                snippet = r.get("snippet", "")
                signals.append({
                    "signal_type": "REVIEW_SIGNAL",
                    "source": r.get("link", "g2.com"),
                    "evidence_text": f"Review found: {snippet[:100]}",
                    "confidence": 0.55,
                })
        except Exception as exc:
            log.warning("review_provider_error: company=%s err=%s", company_name, exc)
        return signals


class FundingSignalProvider(IntelligenceProvider):
    """Detect recent funding events via news dorks."""
    name = "funding"

    def fetch_signals(self, company_name, domain, serper_fn, **kwargs):
        signals = []
        if not company_name:
            return signals
        query = f'"{company_name}" ("raised" OR "funding" OR "Series A" OR "Series B" OR "seed round")'
        try:
            results = serper_fn(query)
            for r in (results or [])[:2]:
                snippet = r.get("snippet", "")
                if any(kw in snippet.lower() for kw in ["raised", "funding", "series", "seed", "million"]):
                    signals.append({
                        "signal_type": "FUNDING_EVENT",
                        "source": r.get("link", ""),
                        "evidence_text": f"Funding signal: {snippet[:100]}",
                        "confidence": 0.70,
                    })
        except Exception as exc:
            log.warning("funding_provider_error: company=%s err=%s", company_name, exc)
        return signals


class NewsSignalProvider(IntelligenceProvider):
    """Monitor recent news mentions."""
    name = "news"

    def fetch_signals(self, company_name, domain, serper_fn, **kwargs):
        signals = []
        if not company_name:
            return signals
        query = f'"{company_name}" ("launch" OR "expansion" OR "partnership" OR "acquisition")'
        try:
            results = serper_fn(query)
            for r in (results or [])[:2]:
                snippet = r.get("snippet", "")
                signals.append({
                    "signal_type": "COMMUNITY_MENTION",
                    "source": r.get("link", ""),
                    "evidence_text": f"News mention: {snippet[:100]}",
                    "confidence": 0.50,
                })
        except Exception as exc:
            log.warning("news_provider_error: company=%s err=%s", company_name, exc)
        return signals


# Registry of all active providers
_PROVIDERS: list[IntelligenceProvider] = [
    HiringSignalProvider(),
    ReviewSignalProvider(),
    FundingSignalProvider(),
    NewsSignalProvider(),
]


def enrich_signals(
    company_name: Optional[str],
    domain: str,
    serper_fn,
    timeout_s: float = 3.0,
) -> list[dict]:
    """Run all intelligence providers in parallel. Non-blocking.

    Returns combined list of signal dicts from all providers.
    Individual provider failures are silently swallowed with a warning log.
    """
    all_signals: list[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_PROVIDERS)) as pool:
        future_map = {
            pool.submit(p.fetch_signals, company_name, domain, serper_fn): p.name
            for p in _PROVIDERS
        }
        done, _ = concurrent.futures.wait(future_map, timeout=timeout_s)
        for fut in done:
            provider_name = future_map[fut]
            try:
                result = fut.result(timeout=0.1)
                if result:
                    all_signals.extend(result)
                    log.info("mesh_provider_ok: provider=%s signals=%d", provider_name, len(result))
            except Exception as exc:
                log.warning("mesh_provider_failed: provider=%s err=%s", provider_name, exc)

    return all_signals
