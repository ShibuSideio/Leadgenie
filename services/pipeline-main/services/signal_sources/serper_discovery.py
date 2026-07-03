"""
Serper Discovery Source — V25.2.0

Uses Serper (Google Search API) strictly as a URL DISCOVERY TOOL.
This source returns candidate URLs with thin content — the full content
is obtained by PRISM scraping in the dispatch layer.

CRITICAL DESIGN PRINCIPLE:
  Serper snippets (140 chars) are NOT used as signal content by default.
  They are stored as a thin discovery hint only.
  The ``is_thin_content`` metadata flag tells signal_harvest.py to
  route these signals through PRISM scraping before Gemini scoring.

  EXCEPTION — Social snippet bypass (V25.2.0):
  For social-domain URLs (LinkedIn, X, Facebook, Instagram, Threads)
  that PRISM cannot access without authentication, signal_harvest.py
  Stage 4.5 reads ``metadata["serper_snippet"]`` (the raw Google
  snippet, ≤140 chars) as the signal text. This is the buyer's own
  words as indexed by Google and is used verbatim for Gemini scoring.
  ``metadata["serper_title"]`` carries the raw Google title.

Why this source still exists:
  Serper/Google is the best tool for finding specific forum threads,
  community discussions, and niche pages that aren't covered by
  Reddit/HN/RSS sources. The query can include site: operators to
  target high-quality discussion forums.

Example queries this source handles well:
  - "site:expatriates.com Oman villa rent looking"
  - "site:community.hubspot.com marketing automation frustration"
  - "site:forums.redfin.com OR site:houzz.com buyer complaints"
"""
from __future__ import annotations

import json
from typing import Optional

import requests
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from core.logging import get_logger                                    # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore[import]

log = get_logger("pipeline.signal_sources.serper_discovery")

_SERPER_URL      = "https://google.serper.dev/search"
_CONNECT_TIMEOUT = 8
_READ_TIMEOUT    = 15


class SerperDiscoverySource(BaseSignalSource):
    """URL discovery via Serper/Google Search.

    Returns SignalItems with ``is_thin_content=True``. These signals are
    routed through PRISM scraping before inline Gemini scoring.

    IMPORTANT: The ``geo_code`` parameter (e.g. "om", "in", "ae") is used
    ONLY when explicitly provided. If Serper returns zero results for a
    geo-restricted query, the request is NOT retried globally — that failure
    is logged and the source returns an empty list. Geo fallback caused the
    original pipeline failures and is not retried here.

    Args:
        discovery_queries: List of Serper query strings. Can include Google
                           Search operators (site:, inurl:, filetype:, etc.).
        serper_api_key:    Serper API key.
        num_results:       Results per query. Default 10.
        geo_code:          ISO 3166-1 alpha-2 country code (e.g. "om", "in").
                           Empty string = no geo restriction.
    """

    source_type = "serper_url"

    def __init__(
        self,
        discovery_queries: list[str],
        serper_api_key: str,
        num_results: int = 10,
        geo_code: str = "",
    ) -> None:
        self._queries      = discovery_queries or []
        self._api_key      = serper_api_key
        self._num_results  = num_results
        self._geo_code     = geo_code.lower().strip()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Run all discovery queries and return candidate URL signals."""
        seen_urls: set[str] = set()
        signals:   list[SignalItem] = []

        for query in self._queries:
            try:
                batch = self._search(query)
                for item in batch:
                    if item.url and item.url not in seen_urls:
                        seen_urls.add(item.url)
                        signals.append(item)
            except Exception as exc:
                log.warning(
                    "serper_discovery_failed",
                    query=query[:100],
                    error=str(exc),
                )

        log.info(
            "serper_discovery_complete",
            queries=len(self._queries),
            signals_found=len(signals),
        )
        return signals

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=8),
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
    )
    def _search(self, query: str) -> list[SignalItem]:
        """Execute a single Serper search and return discovery signals."""
        payload: dict = {"q": query, "num": self._num_results}
        if self._geo_code:
            payload["gl"] = self._geo_code

        resp = requests.post(
            _SERPER_URL,
            headers={
                "X-API-KEY":   self._api_key,
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
        data = resp.json()

        organic = data.get("organic", [])
        if not organic and self._geo_code:
            log.info(
                "serper_geo_returned_empty",
                query=query[:80],
                geo_code=self._geo_code,
                note="No global fallback — geo-zero is a signal quality issue, not a retry target.",
            )
            return []

        signals: list[SignalItem] = []
        for result in organic:
            url     = result.get("link", "")
            title   = result.get("title", "") or ""
            snippet = result.get("snippet", "") or ""

            if not url:
                continue

            # Thin content — just a discovery hint. PRISM will enrich.
            hint_text = (
                f"[SERPER DISCOVERY HINT — PRISM WILL SCRAPE FULL CONTENT]\n"
                f"Title: {title}\n"
                f"Snippet: {snippet}"
            )

            signals.append(SignalItem(
                url         = url,
                text        = hint_text,
                title       = title,
                author      = "",
                source_type = self.source_type,
                fetched_at  = self._now_iso(),
                metadata    = {
                    "search_query":    query,
                    "geo_code":        self._geo_code,
                    "is_thin_content": True,
                    "position":        result.get("position", 0),
                    "serper_snippet":  snippet,   # raw Google snippet (buyer's words for social URLs)
                    "serper_title":    title,     # raw Google title
                },
            ))

        return signals
