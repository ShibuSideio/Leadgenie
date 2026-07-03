"""
Hacker News Signal Source — V25.1.0

Discovers intent signals from Hacker News using the official Algolia search
API. No authentication required — this is a fully open, free API.

API reference: https://hn.algolia.com/api

Endpoint: GET https://hn.algolia.com/api/v1/search_by_date
  query          — search query string
  tags           — comma-separated post type filter:
                   "ask_hn"  — Ask HN questions (highest intent signal)
                   "story"   — Standard posts / Show HN
  numericFilters — "created_at_i>{unix_timestamp}" for recency filtering
  hitsPerPage    — number of results (max 1000)

HN is the primary source for tech-founder, SaaS, and engineering ICP signals.
Ask HN posts are particularly valuable: people explicitly asking for vendor
recommendations, expressing frustration with existing tools, or requesting
advice on a specific problem.
"""
from __future__ import annotations

import datetime
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup                                          # type: ignore[import]
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from core.logging import get_logger                                     # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem   # type: ignore[import]

log = get_logger("pipeline.signal_sources.hackernews")

_ALGOLIA_URL     = "https://hn.algolia.com/api/v1/search_by_date"
_HN_ITEM_URL     = "https://news.ycombinator.com/item"
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT    = 15
_INTER_REQUEST_SLEEP = 0.5  # Algolia is generous; 0.5s is courteous


class HackerNewsSource(BaseSignalSource):
    """Intent signal discovery from Hacker News via the Algolia search API.

    Args:
        search_queries:   List of search terms to query. Each term is run
                          separately for Ask HN and/or Stories.
        max_age_days:     Ignore posts older than this many days. Default 30.
        include_ask:      If True, search Ask HN posts (highest intent). Default True.
        include_stories:  If True, search standard Story posts. Default True.
        min_comments:     Skip posts with fewer than this many comments (noise filter).
                          Default 0 (no filter).
        max_per_source:   Maximum signals to return per discover() call.
    """

    source_type = "hackernews"

    def __init__(
        self,
        search_queries: list[str],
        max_age_days: int = 30,
        include_ask: bool = True,
        include_stories: bool = True,
        min_comments: int = 0,
        max_per_source: int = 40,
    ) -> None:
        self._queries         = search_queries or []
        self._max_age_days    = max_age_days
        self._include_ask     = include_ask
        self._include_stories = include_stories
        self._min_comments    = min_comments
        self._max_per_source  = max_per_source
        self._cutoff_ts       = int(
            (datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)).timestamp()
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Fetch intent signals from HN for all configured queries."""
        seen_ids: set[str] = set()
        signals:  list[SignalItem] = []

        post_types: list[str] = []
        if self._include_ask:
            post_types.append("ask_hn")
        if self._include_stories:
            post_types.append("story")

        for query in self._queries:
            for post_type in post_types:
                if len(signals) >= self._max_per_source:
                    break
                try:
                    batch = self._fetch(query, post_type)
                    for item in batch:
                        if item.url not in seen_ids:
                            seen_ids.add(item.url)
                            signals.append(item)
                except Exception as exc:
                    log.warning(
                        "hackernews_fetch_failed",
                        query=query[:80],
                        post_type=post_type,
                        error=str(exc),
                    )
                time.sleep(_INTER_REQUEST_SLEEP)

        log.info(
            "hackernews_discover_complete",
            queries=len(self._queries),
            signals_found=len(signals),
        )
        return signals[: self._max_per_source]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=8),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
    )
    def _get_json(self, params: dict) -> dict:
        """Fetch from the Algolia HN API."""
        resp = requests.get(
            _ALGOLIA_URL,
            params=params,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch(self, query: str, post_type: str) -> list[SignalItem]:
        """Fetch signals for a single query + post_type combination."""
        data = self._get_json({
            "query":          query,
            "tags":           post_type,
            "numericFilters": f"created_at_i>{self._cutoff_ts}",
            "hitsPerPage":    25,
        })

        signals: list[SignalItem] = []
        for hit in data.get("hits", []):
            hn_id       = hit.get("objectID", "")
            title       = hit.get("title", "") or ""
            story_text  = hit.get("story_text", "") or ""
            num_comments = int(hit.get("num_comments", 0) or 0)

            if num_comments < self._min_comments:
                continue

            # Strip HTML from story_text
            clean_text = self._strip_html(story_text)

            # Build canonical URL — prefer external URL for link posts,
            # fall back to HN item page (which is always the gold standard for Ask HN)
            external_url = hit.get("url", "") or ""
            hn_url = f"{_HN_ITEM_URL}?id={hn_id}" if hn_id else ""
            canonical = hn_url  # Always use HN URL as canonical for dedup stability

            if not canonical:
                continue

            combined = f"{title}\n\n{clean_text}".strip()

            signals.append(SignalItem(
                url         = canonical,
                text        = combined,
                title       = title,
                author      = hit.get("author", "") or "",
                source_type = self.source_type,
                fetched_at  = self._now_iso(),
                metadata    = {
                    "hn_id":          hn_id,
                    "post_type":      post_type,
                    "num_comments":   num_comments,
                    "points":         int(hit.get("points", 0) or 0),
                    "external_url":   external_url,
                    "search_query":   query,
                    "is_thin_content": not bool(clean_text.strip()),
                },
            ))

        return signals

    @staticmethod
    def _strip_html(html: str) -> str:
        """Strip HTML tags from a string using BeautifulSoup."""
        if not html:
            return ""
        try:
            return BeautifulSoup(html, "html.parser").get_text(separator=" ").strip()
        except Exception:
            return html
