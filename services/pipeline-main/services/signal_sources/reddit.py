"""
Reddit Signal Source — V25.1.1
================================
Discovers intent signals from Reddit using two access paths:

PRIMARY (always works, no auth):
  Reddit publishes RSS 2.0 feeds for every subreddit and search result.
  These are publicly accessible at:
    Subreddit new:  https://www.reddit.com/r/{subreddit}/new.rss
    Subreddit search: https://www.reddit.com/r/{subreddit}/search.rss?q={query}&sort=new

UPGRADE (when OAuth credentials available):
  The OAuth Client Credentials grant provides access to the JSON API,
  which returns full ``selftext`` (complete post body). RSS descriptions
  are limited to ~100-300 chars. To enable: set environment variables
    REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET
  These are obtained by registering a free script app at:
    https://www.reddit.com/prefs/apps

NOTE (V25.1.1): Reddit's JSON API now returns 403 Forbidden for unauthenticated
requests. The RSS-first strategy is the correct publicly-available approach.
RSS descriptions are sufficient for Gemini intent classification. Full content
available via PRISM scraping the post URL (which opens the thread).

Rate limits:
  RSS: generous, no documented limit
  OAuth API: 100 requests/minute
"""
from __future__ import annotations

import os
import time
import datetime
import threading
from typing import Optional
from urllib.parse import quote_plus

import requests
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from core.logging import get_logger                                    # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore[import]
from services.signal_sources.rss_feed import RssFeedSource             # type: ignore[import]

log = get_logger("pipeline.signal_sources.reddit")

_USER_AGENT         = "LeadGenie/25.1.0 (intent signal collector; contact@sideio.com)"
_BASE_URL           = "https://www.reddit.com"
_OAUTH_BASE_URL     = "https://oauth.reddit.com"
_TOKEN_URL          = "https://www.reddit.com/api/v1/access_token"
_CONNECT_TIMEOUT    = 10
_READ_TIMEOUT       = 20
_INTER_REQUEST_SLEEP = 0.5  # RSS is generous; short sleep for courtesy

# Thread-safe token cache
_token_cache: dict[str, str | float] = {}
_token_lock = threading.Lock()


class RedditSource(BaseSignalSource):
    """Intent signal discovery from Reddit subreddits.

    Uses RSS feeds (always available, no auth) as the primary access path.
    Upgrades to OAuth JSON API automatically when REDDIT_CLIENT_ID and
    REDDIT_CLIENT_SECRET environment variables are set — providing full
    post text instead of RSS excerpts.

    Args:
        subreddits:     List of subreddit names (without r/ prefix).
        search_terms:   Search queries to run within each subreddit.
        geo_terms:      Optional geographic filter keywords.
        max_age_days:   Discard posts older than this many days.
        max_per_source: Maximum signals returned per discover() call.
        allow_serper:   When True (produce-gated only), empty RSS may fall
                        back to Serper site:reddit.com queries. Harvest must
                        leave this False so automatic jobs never burn credits.
    """

    source_type = "reddit"

    def __init__(
        self,
        subreddits: list[str],
        search_terms: list[str],
        geo_terms: Optional[list[str]] = None,
        max_age_days: int = 14,
        max_per_source: int = 50,
        allow_serper: bool = False,
    ) -> None:
        self._subreddits     = [s.lstrip("r/") for s in subreddits]
        self._search_terms   = search_terms or []
        self._geo_terms      = [g.lower() for g in (geo_terms or [])]
        self._max_age_days   = max_age_days
        self._max_per_source = max_per_source
        self._allow_serper   = bool(allow_serper)

        # Detect OAuth credentials for JSON API upgrade
        self._client_id     = os.environ.get("REDDIT_CLIENT_ID", "")
        self._client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
        self._use_oauth     = bool(self._client_id and self._client_secret)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Fetch intent signals from Reddit via RSS (or JSON API if OAuth configured)."""
        if self._use_oauth:
            log.info("reddit_using_oauth_path")
            return self._discover_via_json_api()
        else:
            log.info("reddit_using_rss_path")
            return self._discover_via_rss()

    # ------------------------------------------------------------------
    # RSS path (primary — no auth required)
    # ------------------------------------------------------------------

    def _discover_via_rss(self) -> list[SignalItem]:
        """Fetch posts via Reddit RSS feeds — always publicly accessible."""
        seen_urls: set[str] = set()
        signals:   list[SignalItem] = []

        rss_urls: list[str] = []

        for subreddit in self._subreddits:
            # New posts feed (no search — organic browsing)
            rss_urls.append(f"{_BASE_URL}/r/{subreddit}/new.rss?limit=25")

            # Search results for each term within this subreddit
            for term in self._search_terms:
                encoded = quote_plus(term)
                rss_urls.append(
                    f"{_BASE_URL}/r/{subreddit}/search.rss"
                    f"?q={encoded}&sort=new&t=month&restrict_sr=1"
                )

        if rss_urls:
            rss = RssFeedSource(
                feed_urls    = rss_urls,
                geo_terms    = self._geo_terms if self._geo_terms else None,
                max_age_days = self._max_age_days,
                max_per_source = self._max_per_source,
            )
            raw_signals = rss.discover()

            for sig in raw_signals:
                if sig.url and sig.url not in seen_urls:
                    seen_urls.add(sig.url)
                    # Tag source_type as reddit (RSS parser returns rss_feed)
                    signals.append(SignalItem(
                        url         = sig.url,
                        text        = sig.text,
                        title       = sig.title,
                        author      = sig.author,
                        source_type = self.source_type,
                        fetched_at  = sig.fetched_at or self._now_iso(),
                        metadata    = {
                            **sig.metadata,
                            "access_path":     "rss",
                            # RSS descriptions are ~100-300 chars — thin for PRISM upgrade
                            "is_thin_content": len(sig.text) < 200,
                        },
                    ))

        log.info(
            "reddit_rss_discover_complete",
            subreddits=len(self._subreddits),
            signals_found=len(signals),
        )

        # V26.0.4: Serper fallback — fires ONLY when RSS returns 0 items.
        # PRODUCE-GATED: require allow_serper=True. Harvest must never take
        # this path (automatic jobs must not burn Serper credits).
        # Limited to 3 terms × 2 subreddits (max 6 queries) to control spend.
        if not signals and self._subreddits and self._search_terms and not self._allow_serper:
            log.info(
                "reddit_serper_fallback_skipped",
                reason="not_produce_gated",
                allow_serper=False,
                note="RSS empty but Serper fallback blocked — harvest/free path. "
                     "Produce-gated runs may set allow_serper=True.",
            )
        elif not signals and self._subreddits and self._search_terms and self._allow_serper:
            try:
                from services.serper_service import search_serper  # type: ignore[import]

                _fallback_subs = self._subreddits[:2]
                _fallback_terms = self._search_terms[:3]
                log.info(
                    "reddit_serper_fallback_triggered",
                    subreddits=_fallback_subs,
                    terms=_fallback_terms,
                    note="All RSS feeds returned 0 items. Falling back to Serper (produce-gated).",
                )
                _serper_seen: set[str] = set()
                for sub in _fallback_subs:
                    for term in _fallback_terms:
                        _query = f"site:reddit.com/r/{sub} {term}"
                        try:
                            _results = search_serper(_query)
                            for r in (_results or []):
                                _link = r.get("link", "")
                                _title = r.get("title", "") or ""
                                _snippet = r.get("snippet", "") or ""
                                if not _link or _link in _serper_seen:
                                    continue
                                _serper_seen.add(_link)
                                signals.append(SignalItem(
                                    url=_link,
                                    text=f"{_title}\n\n{_snippet}".strip(),
                                    title=_title,
                                    author="",
                                    source_type=self.source_type,
                                    fetched_at=self._now_iso(),
                                    metadata={
                                        "access_path": "serper_fallback",
                                        "is_thin_content": True,
                                        "search_query": _query,
                                        "serper_snippet": _snippet,
                                        "serper_title": _title,
                                    },
                                ))
                        except Exception as _q_err:
                            log.warning(
                                "reddit_serper_fallback_query_failed",
                                query=_query[:100],
                                error=str(_q_err),
                            )
                log.info(
                    "reddit_serper_fallback_complete",
                    signals_found=len(signals),
                )
            except Exception as _fb_err:
                log.warning(
                    "reddit_serper_fallback_failed",
                    error=str(_fb_err),
                    note="Serper fallback is non-fatal. Returning 0 Reddit signals.",
                )

        return signals[: self._max_per_source]

    # ------------------------------------------------------------------
    # JSON API path (OAuth upgrade — full selftext available)
    # ------------------------------------------------------------------

    def _discover_via_json_api(self) -> list[SignalItem]:
        """Fetch posts via Reddit OAuth JSON API — full post text available."""
        token = self._get_oauth_token()
        if not token:
            log.warning("reddit_oauth_token_failed", note="Falling back to RSS path.")
            return self._discover_via_rss()

        seen_ids: set[str] = set()
        signals:  list[SignalItem] = []
        cutoff_ts = (
            datetime.datetime.utcnow() - datetime.timedelta(days=self._max_age_days)
        ).timestamp()

        for subreddit in self._subreddits:
            if len(signals) >= self._max_per_source:
                break
            try:
                items = self._json_fetch_new(subreddit, token, cutoff_ts)
                for item in items:
                    if item.url not in seen_ids:
                        seen_ids.add(item.url)
                        signals.append(item)
            except Exception as exc:
                log.warning("reddit_json_fetch_failed", subreddit=subreddit, error=str(exc))
            time.sleep(_INTER_REQUEST_SLEEP)

            for term in self._search_terms:
                if len(signals) >= self._max_per_source:
                    break
                try:
                    items = self._json_search(subreddit, term, token, cutoff_ts)
                    for item in items:
                        if item.url not in seen_ids:
                            seen_ids.add(item.url)
                            signals.append(item)
                except Exception as exc:
                    log.warning(
                        "reddit_json_search_failed",
                        subreddit=subreddit,
                        query=term[:60],
                        error=str(exc),
                    )
                time.sleep(_INTER_REQUEST_SLEEP)

        if self._geo_terms:
            signals = [
                s for s in signals
                if any(g in (s.title + " " + s.text).lower() for g in self._geo_terms)
            ]

        log.info("reddit_json_discover_complete", signals_found=len(signals))
        return signals[: self._max_per_source]

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=8),
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
    )
    def _json_fetch_new(
        self, subreddit: str, token: str, cutoff_ts: float
    ) -> list[SignalItem]:
        resp = requests.get(
            f"{_OAUTH_BASE_URL}/r/{subreddit}/new.json",
            params={"limit": 25, "raw_json": 1},
            headers={
                "User-Agent":    _USER_AGENT,
                "Authorization": f"Bearer {token}",
            },
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
        return self._parse_json_listing(resp.json(), cutoff_ts)

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=8),
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
    )
    def _json_search(
        self, subreddit: str, query: str, token: str, cutoff_ts: float
    ) -> list[SignalItem]:
        resp = requests.get(
            f"{_OAUTH_BASE_URL}/r/{subreddit}/search.json",
            params={"q": query, "sort": "new", "t": "month", "restrict_sr": 1, "limit": 25, "raw_json": 1},
            headers={
                "User-Agent":    _USER_AGENT,
                "Authorization": f"Bearer {token}",
            },
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
        return self._parse_json_listing(resp.json(), cutoff_ts, query=query)

    def _parse_json_listing(
        self, data: dict, cutoff_ts: float, query: str = ""
    ) -> list[SignalItem]:
        _DELETED = frozenset({"[deleted]", "[removed]", ""})
        signals: list[SignalItem] = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            if float(post.get("created_utc", 0)) < cutoff_ts:
                continue
            selftext  = (post.get("selftext") or "").strip()
            if selftext in _DELETED:
                selftext = ""
            title     = post.get("title", "") or ""
            permalink = post.get("permalink", "")
            canonical = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else (post.get("url") or "")
            if not canonical:
                continue
            signals.append(SignalItem(
                url         = canonical,
                text        = f"{title}\n\n{selftext}".strip(),
                title       = title,
                author      = post.get("author", "") or "",
                source_type = self.source_type,
                fetched_at  = self._now_iso(),
                metadata    = {
                    "subreddit":       post.get("subreddit", ""),
                    "score":           post.get("score", 0),
                    "num_comments":    post.get("num_comments", 0),
                    "access_path":     "oauth_json",
                    "is_thin_content": not bool(selftext.strip()),
                    "search_query":    query,
                },
            ))
        return signals

    def _get_oauth_token(self) -> str:
        """Get or refresh Reddit OAuth access token (client credentials grant)."""
        global _token_cache
        with _token_lock:
            if (
                _token_cache.get("token")
                and float(_token_cache.get("expires_at", 0)) > time.time() + 60
            ):
                return str(_token_cache["token"])
            try:
                resp = requests.post(
                    _TOKEN_URL,
                    auth=(self._client_id, self._client_secret),
                    data={"grant_type": "client_credentials"},
                    headers={"User-Agent": _USER_AGENT},
                    timeout=(8, 15),
                )
                resp.raise_for_status()
                token_data = resp.json()
                _token_cache["token"]      = token_data["access_token"]
                _token_cache["expires_at"] = time.time() + int(token_data.get("expires_in", 3600))
                return str(_token_cache["token"])
            except Exception as exc:
                log.warning("reddit_oauth_token_error", error=str(exc))
                return ""
