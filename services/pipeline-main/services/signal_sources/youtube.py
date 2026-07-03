"""
YouTube Signal Source — V25.2.0

Strategy: buyer-intent signal discovery from YouTube video content.

Why YouTube:
  B2C and D2C buyers increasingly research purchases on YouTube before
  converting. Searches like "best interior designers Muscat review" or
  "running shoes comparison 2025" reveal active purchase intent. The
  video title and description contain the buyer's language and context
  without requiring authentication or scraping.

Access method:
  Uses the YouTube Data API v3 (search.list endpoint) to discover recent
  videos matching ICP search queries. Video title + description (first
  500 chars) are used as signal text — no video download or transcript
  extraction is required.

API key:
  Set ``YOUTUBE_API_KEY`` environment variable. If not set, the source
  logs a warning and returns an empty list (graceful degradation).

Cost model:
  YouTube Data API v3 is free up to 10,000 units/day. Each search.list
  call costs 100 units. Budget: ~100 search calls/day at default quota.
"""
from __future__ import annotations

import datetime
import os
from typing import Optional
from urllib.parse import urlencode

import requests
from tenacity import (
    retry,
    wait_exponential,
    stop_after_attempt,
    retry_if_exception_type,
)

from core.logging import get_logger                                    # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore[import]

log = get_logger("pipeline.signal_sources.youtube")

_YT_SEARCH_URL   = "https://www.googleapis.com/youtube/v3/search"
_CONNECT_TIMEOUT = 8
_READ_TIMEOUT    = 15


class YouTubeSource(BaseSignalSource):
    """YouTube video discovery via the YouTube Data API v3.

    Discovers recent videos matching ICP search queries and returns
    their title + description as signal text for Gemini inline scoring.
    No video download or transcript extraction is required — the
    description alone typically contains sufficient buyer-intent language.

    Args:
        search_queries: List of YouTube search queries derived from the
                        campaign ICP by the source router.
        max_results:    Maximum videos to return per query. Default 10.
        max_age_days:   Only return videos published within this many
                        days. Default 30.
        api_key:        YouTube Data API v3 key. Falls back to
                        ``YOUTUBE_API_KEY`` environment variable.
                        Source returns [] if neither is set.
    """

    source_type = "youtube"

    def __init__(
        self,
        search_queries: list[str],
        max_results: int = 10,
        max_age_days: int = 30,
        api_key: str = "",
    ) -> None:
        self._queries      = search_queries or []
        self._max_results  = max_results
        self._max_age_days = max_age_days
        self._api_key      = api_key or os.environ.get("YOUTUBE_API_KEY", "")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Run all search queries and return YouTube intent signals."""
        if not self._api_key:
            log.warning(
                "youtube_no_api_key",
                note="Set YOUTUBE_API_KEY env var to enable YouTubeSource.",
            )
            return []

        published_after = (
            datetime.datetime.utcnow() - datetime.timedelta(days=self._max_age_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        seen_video_ids: set[str] = set()
        raw_items: list[tuple[dict, str]] = []  # (item, originating_query)

        for query in self._queries:
            try:
                items = self._search(query, published_after)
                for item in items:
                    vid_id = (
                        item.get("id", {}).get("videoId")
                        or item.get("id", {}).get("videoID")
                        or ""
                    )
                    if vid_id and vid_id not in seen_video_ids:
                        seen_video_ids.add(vid_id)
                        raw_items.append((item, query))
            except Exception as exc:
                log.warning(
                    "youtube_search_failed",
                    query=query[:100],
                    error=str(exc),
                )

        signals: list[SignalItem] = []
        for item, query in raw_items:
            signal = self._to_signal(item, query)
            if signal is not None:
                signals.append(signal)

        log.info(
            "youtube_discover_complete",
            queries=len(self._queries),
            unique_videos=len(seen_video_ids),
            signals_built=len(signals),
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
    def _search(self, query: str, published_after: str) -> list[dict]:
        """Execute a single YouTube search and return raw API items.

        Args:
            query:           YouTube search string.
            published_after: ISO 8601 UTC timestamp lower bound.

        Returns:
            List of raw YouTube Data API v3 search result item dicts.
        """
        params = {
            "part":             "snippet",
            "q":                query,
            "type":             "video",
            "maxResults":       str(self._max_results),
            "publishedAfter":   published_after,
            "relevanceLanguage": "en",
            "key":              self._api_key,
        }
        resp = requests.get(
            _YT_SEARCH_URL,
            params=params,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
        return resp.json().get("items", [])

    def _to_signal(self, item: dict, query: str) -> Optional[SignalItem]:
        """Convert a raw YouTube API item to a SignalItem.

        Args:
            item:  Raw YouTube Data API v3 search result item.
            query: The search query that surfaced this item (for metadata).

        Returns:
            SignalItem with title + description as text, or None on error.
        """
        try:
            video_id = (
                item.get("id", {}).get("videoId")
                or item.get("id", {}).get("videoID")
                or ""
            )
            if not video_id:
                return None

            snippet      = item.get("snippet", {})
            title        = (snippet.get("title") or "").strip()
            description  = (snippet.get("description") or "").strip()[:500]
            channel      = (snippet.get("channelTitle") or "").strip()
            published_at = (snippet.get("publishedAt") or "").strip()

            url  = f"https://www.youtube.com/watch?v={video_id}"
            text = f"{title}\n\n{description}".strip()

            return SignalItem(
                url         = url,
                text        = text,
                title       = title,
                author      = channel,
                source_type = self.source_type,
                fetched_at  = self._now_iso(),
                metadata    = {
                    "is_thin_content": False,
                    "content_source":  "youtube_api",
                    "social_platform": "youtube",
                    "video_id":        video_id,
                    "channel":         channel,
                    "published_at":    published_at,
                    "search_query":    query,
                    "serper_snippet":  description,
                },
            )

        except Exception as exc:
            log.warning(
                "youtube_to_signal_failed",
                error=str(exc),
            )
            return None
