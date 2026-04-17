"""
Tech-Stack X-Ray HTML fingerprint signatures.

Canonical single source — import from here rather than defining inline.
Used by GeneralDomainHook and scrape_url() legacy path.
"""

# Maps human-readable tech name → HTML pattern to search for (lowercase match)
TECH_SIGNATURES: dict[str, str] = {
    "wordpress":        "wp-content",
    "shopify":          "cdn.shopify.com",
    "stripe":           "js.stripe.com",
    "react":            "react-root",
    "hubspot":          "js.hs-scripts.com",
    "salesforce":       "force.com",
    "google analytics": "google-analytics.com",
    "segment":          "cdn.segment.com",
    "intercom":         "widget.intercom.io",
    "crisp":            "crisp.chat",
    "zendesk":          "zopim.com",
    "drift":            "drift.com/drift-frame",
}


def extract_tech_stack(html_blob: str) -> list[str]:
    """Return list of detected technology names from raw HTML.

    Args:
        html_blob: Raw HTML string (case-insensitive matching applied).

    Returns:
        List of tech names whose fingerprint was found in *html_blob*.
    """
    lowered = html_blob.lower()
    return [name for name, sig in TECH_SIGNATURES.items() if sig in lowered]
