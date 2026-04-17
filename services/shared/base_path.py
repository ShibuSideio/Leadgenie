"""
Canonical URL → ontology_map base-path resolver.

This is the SINGLE authoritative implementation.  Both orchestrator and
pipeline-main import from here.  If the two implementations ever drift the
RLHF feedback loop silently breaks — keeping this file is the architectural
invariant that prevents that failure mode (Design Invariant #15, V22 TSD).
"""
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Social / walled-garden domain registry
# Used by parse_base_path() and by the PRISM OperatingModeRouter.
# Kept here so both services share the exact same set.
# ---------------------------------------------------------------------------
SOCIAL_ONTOLOGY_DOMAINS: frozenset[str] = frozenset({
    "reddit.com", "facebook.com", "linkedin.com", "quora.com",
    "kaggle.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
})


def parse_base_path(url: str) -> str:
    """Return the canonical ontology_map document key for *url*.

    Rules (V22 TSD §22):
      * Social / Walled-Garden domains  →  ``domain + first 2 path segments``
        e.g. ``reddit.com/r/Entrepreneur/comments/xyz`` → ``reddit.com/r/Entrepreneur``
      * All other (B2B / news / directories) →  root domain only
        e.g. ``www.techcrunch.com/2024/03/article``     → ``techcrunch.com``

    Strips ``www.``, query params, fragments, and trailing slashes.
    Returns ``'unknown'`` as a safe sentinel if parsing fails.

    Args:
        url: Raw URL string (with or without scheme).

    Returns:
        Canonical ontology_map key string, never empty.
    """
    try:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        hostname = parsed.hostname or ""
        domain = hostname.removeprefix("www.")
        if not domain:
            return "unknown"

        if any(domain.endswith(s) for s in SOCIAL_ONTOLOGY_DOMAINS):
            segments = [s for s in parsed.path.split("/") if s]
            key_parts = [domain] + segments[:2]
            return "/".join(key_parts)

        return domain
    except Exception:
        return "unknown"
