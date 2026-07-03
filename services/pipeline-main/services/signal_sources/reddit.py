"""
Reddit Signal Source — V25.1.0

Discovers intent signals from Reddit using the official public JSON API.
No authentication required for reading public subreddits.

API reference:
  Subreddit new:  GET https://www.reddit.com/r/{subreddit}/new.json
  Subreddit search: GET https://www.reddit.com/r/{subreddit}/search.json
  Global search:  GET https://www.reddit.com/search.json

Reddit returns full post text in the ``selftext`` field — no PRISM scrape
needed for text posts. Link posts have empty selftext; their URL is returned
for optional PRISM enrichment.

Rate limits: 60 requests/minute without OAuth. We enforce a 1.1s inter-request
sleep to stay safely under the limit.
"""
from __future__ import annotations

import time
import datetime
from typing import Optional

import requests
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from core.logging import get_logger                          # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore[import]

log = get_logger("pipeline.signal_sources.reddit")

_USER_AGENT = "LeadGenie/25.1.0 (intent signal collector; contact@sideio.com)"
_BASE_URL   = "https://www.reddit.com"
_CONNECT_TIMEOUT = 10  # seconds
_READ_TIMEOUT    = 20  # seconds
_INTER_REQUEST_SLEEP = 1.1  # seconds — stays under 60 req/min
_DELETED_SENTINEL = frozenset({"[deleted]", "[removed]", ""})


class RedditSource(BaseSignalSource):
    """Intent signal discovery via Reddit's public JSON API.

    Fetches new posts from specified subreddits and optionally searches within
    them using caller-supplied search terms. All configuration is injected
    at construction time — no hardcoding inside this class.

    Args:
        subreddits:    List of subreddit names to monitor (without the r/ prefix).
                       e.g. ["marketing", "indianstartups"]
        search_terms:  List of queries to run as subreddit searches. Each term
                       is searched in every subreddit. May be empty — in that
                       case only new-post browsing is performed.
        geo_terms:     Optional list of geographic keywords (e.g. ["Oman", "Muscat"]).
                       When provided, posts are filtered to those containing at
                       least one geo term in title or selftext (case-insensitive).
                       When None or empty, no geo filtering is applied.
        max_age_days:  Discard posts older than this many days. Default 14.
        max_per_source: Max signals to return from this source per discover() call.
    """

    source_type = "reddit"

    def __init__(
        self,
        subreddits: list[str],
        search_terms: list[str],
        geo_terms: Optional[list[str]] = None,
        max_age_days: int = 14,
        max_per_source: int = 50,
    ) -> None:
        self._subreddits    = [s.lstrip("r/") for s in subreddits]
        self._search_terms  = search_terms or []
        self._geo_terms     = [g.lower() for g in (geo_terms or [])]
        self._max_age_days  = max_age_days
        self._max_per_source = max_per_source
        self._cutoff_ts     = (
            datetime.datetime.utcnow()
            - datetime.timedelta(days=max_age_days)
        ).timestamp()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Fetch intent signals from all configured subreddits."""
        seen_ids: set[str] = set()
        signals:  list[SignalItem] = []

        for subreddit in self._subreddits:
            if len(signals) >= self._max_per_source:
                break

            # 1. Organic new posts (no query)
            try:
                items = self._fetch_new(subreddit)
                for item in items:
                    if item.url not in seen_ids:
                        seen_ids.add(item.url)
                        signals.append(item)
            except Exception as exc:
                log.warning(
                    "reddit_fetch_new_failed",
                    subreddit=subreddit,
                    error=str(exc),
                )
            time.sleep(_INTER_REQUEST_SLEEP)

            # 2. Targeted search per term
            for term in self._search_terms:
                if len(signals) >= self._max_per_source:
                    break
                try:
                    items = self._fetch_search(subreddit, term)
                    for item in items:
                        if item.url not in seen_ids:
                            seen_ids.add(item.url)
                            signals.append(item)
                except Exception as exc:
                    log.warning(
                        "reddit_search_failed",
                        subreddit=subreddit,
                        query=term[:80],
                        error=str(exc),
                    )
                time.sleep(_INTER_REQUEST_SLEEP)

        # Apply geo filter if terms provided
        if self._geo_terms:
            signals = self._apply_geo_filter(signals)

        log.info(
            "reddit_discover_complete",
            subreddits=len(self._subreddits),
            search_terms=len(self._search_terms),
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
    def _get_json(self, url: str, params: dict) -> dict:
        """HTTP GET with standard Reddit headers. Raises on non-200."""
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        if resp.status_code == 429:
            log.warning("reddit_rate_limited", url=url)
            time.sleep(30)
            resp = requests.get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        resp.raise_for_status()
        return resp.json()

    def _fetch_new(self, subreddit: str) -> list[SignalItem]:
        """Fetch the newest posts from a subreddit."""
        data = self._get_json(
            f"{_BASE_URL}/r/{subreddit}/new.json",
            {"limit": 25, "raw_json": 1},
        )
        return self._parse_listing(data, subreddit=subreddit)

    def _fetch_search(self, subreddit: str, query: str) -> list[SignalItem]:
        """Search within a subreddit for a specific query."""
        data = self._get_json(
            f"{_BASE_URL}/r/{subreddit}/search.json",
            {
                "q":           query,
                "sort":        "new",
                "t":           "month",
                "restrict_sr": 1,
                "limit":       25,
                "raw_json":    1,
            },
        )
        return self._parse_listing(data, subreddit=subreddit, query=query)

    def _parse_listing(
        self,
        data: dict,
        subreddit: str,
        query: str = "",
    ) -> list[SignalItem]:
        """Parse Reddit listing JSON into SignalItems."""
        signals: list[SignalItem] = []
        children = (
            data.get("data", {}).get("children", [])
            if isinstance(data, dict)
            else []
        )

        for child in children:
            post = child.get("data", {})
            if not isinstance(post, dict):
                continue

            # Age filter
            created_utc = float(post.get("created_utc", 0))
            if created_utc and created_utc < self._cutoff_ts:
                continue

            selftext = post.get("selftext", "") or ""
            if selftext in _DELETED_SENTINEL:
                selftext = ""

            title     = post.get("title", "") or ""
            permalink = post.get("permalink", "")
            canonical = (
                f"{_BASE_URL}{permalink}"
                if permalink.startswith("/")
                else (post.get("url", "") or "")
            )
            if not canonical:
                continue

            # Combine title + selftext for scoring
            combined = f"{title}\n\n{selftext}".strip()

            # Mark as thin if selftext is empty (link post — needs PRISM)
            is_thin = not bool(selftext.strip())

            signals.append(SignalItem(
                url         = canonical,
                text        = combined,
                title       = title,
                author      = post.get("author", "") or "",
                source_type = self.source_type,
                fetched_at  = self._now_iso(),
                metadata    = {
                    "subreddit":       post.get("subreddit", subreddit),
                    "score":           post.get("score", 0),
                    "num_comments":    post.get("num_comments", 0),
                    "is_thin_content": is_thin,
                    "search_query":    query,
                    "post_id":         post.get("id", ""),
                },
            ))

        return signals

    def _apply_geo_filter(self, signals: list[SignalItem]) -> list[SignalItem]:
        """Keep only signals that mention at least one geo term."""
        filtered = []
        for sig in signals:
            haystack = (sig.title + " " + sig.text).lower()
            if any(term in haystack for term in self._geo_terms):
                filtered.append(sig)
        return filtered
