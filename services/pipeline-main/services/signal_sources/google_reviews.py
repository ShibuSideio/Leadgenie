"""
Google Reviews Signal Source — V25.2.0

Strategy: competitor reviews as buyer-intent signals.

Why this works:
  A 4- or 5-star review on a competitor's Google Maps listing is written by
  a REAL buyer who has already spent money in this category. They describe
  their exact use case, pain, and outcome in the buyer's own language.
  Aggregated across 3-5 competitors, this corpus reveals:
    - What buyers value (mentioned in praise)
    - What pain they were solving (mentioned in context)
    - What language they use (verbatim buyer vocabulary)

Archetype usage:
  Best for B2C and D2C ICPs where buyers transact locally (restaurants,
  salons, clinics, real estate agents, interior designers, etc.). Works
  for B2B service firms where client reviews appear on Google Maps
  (accounting firms, law firms, logistics companies).

Access method:
  1. Gemini derives 3-5 competitor business names from the ICP context.
  2. Serper Maps API (https://google.serper.dev/maps) locates each
     competitor and returns their place_id.
  3. Serper Reviews API (https://google.serper.dev/reviews) fetches the
     latest 10 reviews for each place_id.
  4. Reviews with rating > 3 and text ≥ 40 chars are converted to
     SignalItems with is_thin_content=False (full buyer text inline).

Rate limits: Serper Maps + Reviews consume 1 credit each per call.
"""
from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote_plus

import requests
from tenacity import (
    retry,
    wait_exponential,
    stop_after_attempt,
    retry_if_exception_type,
)

from core.logging import get_logger                                    # type: ignore[import]
from services.gemini_service import call_gemini_2_5                   # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore[import]

log = get_logger("pipeline.signal_sources.google_reviews")

_MAPS_URL    = "https://google.serper.dev/maps"
_REVIEWS_URL = "https://google.serper.dev/reviews"
_CONNECT_TIMEOUT = 8
_READ_TIMEOUT    = 15

_COMPETITOR_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "competitor_names": {
            "type": "ARRAY",
            "description": "3-5 competitor business names operating in the geo that serve the same ICP.",
            "items": {"type": "STRING"},
        },
    },
    "required": ["competitor_names"],
}


class GoogleReviewSource(BaseSignalSource):
    """Google Maps reviews of competitor businesses as buyer-intent signals.

    Mines real buyer language from Google Maps reviews posted about
    competitor businesses. Reviews are written in the buyer's own words
    and describe their exact use case, pain point, and outcome — making
    them high-quality input for Gemini inline scoring.

    Args:
        icp_context:    Assembled ICP context string from context_builder.
                        Used by Gemini to derive relevant competitor names.
        geo:            Campaign location string (e.g. "Muscat, Oman").
                        Appended to Maps search queries for geo-scoping.
        archetype:      Sourcing vector (B2B, B2C, D2C, B2B2C). Logged
                        for observability; routing logic is ICP-driven.
        serper_api_key: Serper API key (X-API-KEY header).
        max_age_days:   Not used for filtering (Maps API does not expose
                        review dates reliably). Kept for interface parity.
    """

    source_type = "google_review"

    def __init__(
        self,
        icp_context: str,
        geo: str,
        archetype: str,
        serper_api_key: str,
        max_age_days: int = 60,
    ) -> None:
        self._icp_context  = icp_context
        self._geo          = geo.strip()
        self._archetype    = archetype.upper()
        self._api_key      = serper_api_key
        self._max_age_days = max_age_days

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Derive competitors via Gemini, then fetch and return their reviews."""
        signals: list[SignalItem] = []

        try:
            names = self._derive_competitor_names()
        except Exception as exc:
            log.warning(
                "google_reviews_competitor_derivation_failed",
                geo=self._geo,
                error=str(exc),
            )
            return signals

        for name in names[:5]:
            try:
                batch = self._fetch_reviews_for_place(name)
                signals.extend(batch)
            except Exception as exc:
                log.warning(
                    "google_reviews_place_failed",
                    place_name=name[:80],
                    error=str(exc),
                )

        log.info(
            "google_reviews_discover_complete",
            competitors_queried=len(names[:5]),
            signals_found=len(signals),
            geo=self._geo,
            archetype=self._archetype,
        )
        return signals

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _derive_competitor_names(self) -> list[str]:
        """Ask Gemini to identify 3-5 competitor business names in the geo.

        Returns:
            List of business name strings (may be empty on Gemini failure).
        """
        geo_note = f"Target geography: {self._geo}." if self._geo else "Geography: Global."
        prompt = (
            f"You are a competitive intelligence analyst for a lead generation platform.\n\n"
            f"TASK: Identify 3-5 real, named competitor businesses operating in the market "
            f"described by the ICP context below. These must be businesses a potential buyer "
            f"would visit, review, or compare against on Google Maps.\n\n"
            f"ARCHETYPE: {self._archetype}\n"
            f"{geo_note}\n\n"
            f"ICP CONTEXT:\n{self._icp_context}\n\n"
            f"Return ONLY actual business names — no generic descriptions. "
            f"Choose businesses likely to have Google Maps listings and real customer reviews."
        )

        result = call_gemini_2_5(
            prompt,
            expect_json=True,
            response_schema=_COMPETITOR_SCHEMA,
        )
        if not isinstance(result, dict):
            log.warning(
                "google_reviews_competitor_bad_response",
                response_type=type(result).__name__,
            )
            return []

        names = [n for n in result.get("competitor_names", []) if isinstance(n, str) and n.strip()]
        log.info(
            "google_reviews_competitors_derived",
            count=len(names),
            geo=self._geo,
        )
        return names

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=8),
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
    )
    def _maps_search(self, query: str) -> dict:
        """POST to Serper Maps API and return the top result dict.

        Args:
            query: Full text search query (e.g. "Reem Interiors Muscat, Oman").

        Returns:
            Top result dict from Serper Maps, or empty dict on no results.
        """
        resp = requests.post(
            _MAPS_URL,
            headers={
                "X-API-KEY":    self._api_key,
                "Content-Type": "application/json",
            },
            data=json.dumps({"q": query, "num": 1}),
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
        places = resp.json().get("places", [])
        return places[0] if places else {}

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=8),
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
    )
    def _maps_reviews(self, place_id: str) -> list[dict]:
        """POST to Serper Reviews API and return the reviews list.

        Args:
            place_id: Google Maps place_id string.

        Returns:
            List of review dicts from Serper.
        """
        resp = requests.post(
            _REVIEWS_URL,
            headers={
                "X-API-KEY":    self._api_key,
                "Content-Type": "application/json",
            },
            data=json.dumps({"placeId": place_id}),
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
        return resp.json().get("reviews", [])

    def _fetch_reviews_for_place(self, place_name: str) -> list[SignalItem]:
        """Fetch Maps listing and reviews for a single competitor name.

        Args:
            place_name: Business name as returned by Gemini.

        Returns:
            List of SignalItems, one per qualifying review.
        """
        query = f"{place_name} {self._geo}".strip()
        place = self._maps_search(query)
        if not place:
            log.info(
                "google_reviews_place_not_found",
                place_name=place_name[:80],
                query=query[:120],
            )
            return []

        place_id = place.get("placeId") or place.get("place_id") or ""
        if not place_id:
            log.info(
                "google_reviews_no_place_id",
                place_name=place_name[:80],
            )
            return []

        try:
            reviews = self._maps_reviews(place_id)
        except Exception as exc:
            log.warning(
                "google_reviews_fetch_failed",
                place_name=place_name[:80],
                place_id=place_id,
                error=str(exc),
            )
            return []

        signals: list[SignalItem] = []
        for review in reviews:
            rating      = review.get("rating", 0)
            review_text = (review.get("snippet") or review.get("text") or "").strip()
            review_date = review.get("date") or review.get("publishedAt") or ""

            # Skip low-rating reviews (complaints skew signal; stay positive)
            # Skip very short reviews (not enough buyer language to score)
            if rating <= 3 or len(review_text) < 40:
                continue

            url   = f"https://www.google.com/maps/place/?q={quote_plus(place_id)}"
            text  = f"{rating}star review on '{place_name}': {review_text}"
            title = f"{place_name} \u2014 {rating}\u2605 review"

            signals.append(SignalItem(
                url         = url,
                text        = text,
                title       = title,
                author      = review.get("author") or review.get("user") or "",
                source_type = self.source_type,
                fetched_at  = self._now_iso(),
                metadata    = {
                    "is_thin_content": False,
                    "content_source":  "google_review",
                    "social_platform": "google_maps",
                    "place_name":      place_name,
                    "place_id":        place_id,
                    "rating":          rating,
                    "review_date":     review_date,
                    "serper_snippet":  review_text,
                },
            ))

        log.info(
            "google_reviews_place_complete",
            place_name=place_name[:80],
            place_id=place_id,
            reviews_total=len(reviews),
            signals_kept=len(signals),
        )
        return signals
