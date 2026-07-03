"""
LeadGenie Signal Sources Package — V25.2.0

Provides archetype-aware, multi-source signal discovery without hardcoded
campaign values. Each source implements BaseSignalSource and returns
SignalItem objects with full content for downstream Gemini inline scoring.

Design principles:
  - No hardcoded subreddits, keywords, or URLs — all configured by source_router
  - Full content always (post body, article text, job description) — no snippets
  - PRISM scraping used only when a source cannot provide full text inline
  - Every source handles failures gracefully and returns partial results

Available sources:
  === B2B ===
  RedditSource             — Reddit RSS (public) + OAuth JSON API (upgrade path)
  HackerNewsSource         — HN Algolia API (no auth, full text)
  RssFeedSource            — Generic RSS/Atom parser (any feed URL)
  SerperDiscoverySource    — Serper as URL discovery only → PRISM scrapes full content
  JobPostSource            — Job board signals: capability gap = buying trigger

  === B2C / D2C ===
  ClassifiedListingSource  — Expat forums, property portals, classified ads
                             (expatriates.com, dubizzle.com, propertyfinder.ae, OLX)
                             Highest-quality B2C signal: "Looking for 3BR villa Muscat"
  ConsumerForumSource      — Consumer review and product comparison platforms
                             (r/BuyItForLife, r/frugal, Quora, ProductHunt, IndieHackers)
                             D2C founder signal: Show HN, IndieHackers, Shopify community
  GoogleReviewSource       — Google Maps reviews of competitor businesses.
                             Gemini derives competitor names from ICP; Serper Maps + Reviews
                             API fetches buyer-language reviews. All archetypes.
  YouTubeSource            — YouTube video discovery via YouTube Data API v3.
                             Video title + description as buyer-intent signal text.
                             B2C and D2C archetypes only.

  === Base ===
  BaseSignalSource         — Abstract base class for all sources
  SignalItem               — Standardized signal container with full content

Archetype routing: see services.source_router.SourceRouter
"""
from __future__ import annotations

from services.signal_sources.base import SignalItem, BaseSignalSource  # noqa: F401
from services.signal_sources.google_reviews import GoogleReviewSource   # noqa: F401
from services.signal_sources.youtube import YouTubeSource               # noqa: F401
