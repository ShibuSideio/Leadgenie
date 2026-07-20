"""
Orchestrator — Inbound Maps Service.

Queries the Serper Maps API to discover negative Google Maps (GMB) reviews
for target competitors, identifying local business churn and customer paint points.

V24.1.14 — Added June 2026
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

import httpx

from core.logging import get_logger  # type: ignore[import]
from services.inbound_sentiment_service import _get_serper_key, _score_with_gemini  # type: ignore[import]

log = get_logger("orchestrator.inbound_maps")

SERPER_MAPS_URL = "https://google.serper.dev/maps"


class InboundMapsService:
    """
    Local GMB review scanner for competitive intelligence.
    
    Usage:
        svc = InboundMapsService(persona=persona_dict, campaign=campaign_dict)
        signals = svc.run()
    """

    def __init__(self, persona: dict, campaign: dict):
        self.persona = persona
        self.campaign = campaign
        _junk = {"legacy tool", "target persona", "general business", "n/a", "placeholder"}
        self.competitors = [
            str(c).strip()
            for c in (persona.get("competitors") or [])[:3]
            if str(c).strip() and str(c).strip().lower() not in _junk
        ]
        # V26.0.4.3 / V27.1.0: Derive a short maps-safe phrase — never full bio.
        # Full bio/"Seed investment Sideio"/"AI Lead Generation for B2B" as
        # "{bio} near me" costs 3 Serper credits for near-zero Maps utility.
        from services.inbound_sentiment_service import (  # type: ignore[import]
            _sanitize_phrase,
        )
        _raw_industry = str(
            persona.get("industry")
            or campaign.get("campaign_focus")
            or campaign.get("keywords")
            or ""
        ).strip()
        # Prefer keywords / focus over free-text bio for Maps queries.
        if not _raw_industry:
            _raw_industry = str(
                campaign.get("effective_bio") or campaign.get("bio") or ""
            ).strip()
        self.industry = _sanitize_phrase(_raw_industry, max_words=4)
        self.icp_desc = str(
            persona.get("icp_description")
            or persona.get("persona_description")
            or self.industry
        )

    def _build_queries(self) -> list[str]:
        """Build maps search queries using competitor names (max 3)."""
        queries: list[str] = []
        for comp in self.competitors[:3]:
            if comp and len(comp) >= 3:
                queries.append(comp)
        # V26.0.4.3 / V27.1.0: industry fallback only if short + search-safe.
        if not queries and self.industry and len(self.industry) >= 3:
            if self.industry.lower() not in {
                "target persona", "general business", "legacy tool",
            }:
                queries.append(f"{self.industry} near me")
        if not queries:
            log.info(
                "inbound_maps_no_queries",
                campaign_id=self.campaign.get("campaign_id", ""),
                note="No competitors and no meaningful industry — "
                     "skipping Maps search to save credits.",
            )
        return queries

    def _search_maps(self, query: str) -> list[dict]:
        """Execute a Serper Maps query. Returns list of place dicts."""
        gl = self.campaign.get("gl") or "us"
        location = self.campaign.get("location")
        payload = {"q": query, "gl": gl, "hl": "en"}
        if location:
            payload["location"] = location

        try:
            resp = httpx.post(
                SERPER_MAPS_URL,
                headers={
                    "X-API-KEY": _get_serper_key(),
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json().get("places", [])
        except Exception as exc:
            log.warning("serper_maps_call_failed", query=query, error=str(exc))
            return []

    def _fetch_place_reviews(self, cid: str) -> list[dict]:
        """Fetch actual reviews for a place using its Google Customer ID (cid) via Serper."""
        # V27.1.0: Never call Reviews API without a real cid — empty payloads
        # still bill 1 credit each (blank Query rows in Serper audit).
        if not cid or not str(cid).strip():
            log.info("serper_reviews_skipped_empty_cid")
            return []
        try:
            resp = httpx.post(
                "https://google.serper.dev/reviews",
                headers={
                    "X-API-KEY": _get_serper_key(),
                    "Content-Type": "application/json",
                },
                json={"cid": str(cid).strip()},
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json().get("reviews", [])
        except Exception as exc:
            log.warning("serper_reviews_call_failed", cid=cid, error=str(exc))
            return []

    def run(self, max_places: int = 5) -> list[dict]:
        """
        Scan maps for negative reviews and convert to inbound signals.
        """
        queries = self._build_queries()
        signals = []

        for query in queries:
            places = self._search_maps(query)[:max_places]
            for place in places:
                place_name = place.get("title", "")
                address = place.get("address", "")
                place_rating = place.get("rating")
                cid = place.get("cid")
                
                # Fetch actual reviews using the GMB CID identifier
                reviews = self._fetch_place_reviews(cid) if cid else []
                
                # Fallback to warning synthetic reviews only if no real reviews are found AND rating is low
                if not reviews and place_rating is not None and float(place_rating) <= 2.5:
                    reviews = [{
                        "name": "Anonymous GMB User",
                        "rating": int(float(place_rating)),
                        "text": f"GMB listing average rating is low ({place_rating}/5.0). Customers reported issues."
                    }]

                for rev in reviews:
                    rev_rating = rev.get("rating")
                    rev_text = rev.get("text", "")
                    
                    # Target only negative reviews (2 stars or less)
                    if rev_rating is not None and int(rev_rating) <= 2 and rev_text:
                        # Construct a virtual URL for deduplication and traceback
                        url = f"https://www.google.com/maps/place/?q={place_name.replace(' ', '+')}&addr={address.replace(' ', '+')}&rev={hashlib.sha256(rev_text.encode()).hexdigest()[:8]}"
                        
                        title = f"Negative Google Map Review for {place_name}"
                        snippet = f"[{rev_rating}/5 Star Rating] Reviewer: {rev.get('name', 'Anonymous')}. Review: {rev_text}"

                        scored = _score_with_gemini(title, snippet, url, f"Maps API: {query}", self.icp_desc)
                        if not scored:
                            continue

                        signals.append({
                            "signal_id": hashlib.sha256(url.encode()).hexdigest()[:16],
                            "source_url": url,
                            "source_platform": "gmb",
                            "headline": title,
                            "snippet": snippet[:300],
                            "serper_query": f"Maps API: {query}",
                            "triggering_keyword": query,
                            "matched_persona": self.persona.get("persona_name", ""),
                            "matched_campaign_id": self.campaign.get("campaign_id", ""),
                            "week": _week_label(),
                            "status": "new",
                            **scored
                        })

        return signals


def _week_label() -> str:
    from datetime import datetime
    now = datetime.utcnow()
    return f"{now.year}-W{now.isocalendar()[1]:02d}"
