"""
Canonical geography → (location_string, gl_code) mapping.

Imported by:
  - orchestrator  (campaign creation + Synaptic Router)
  - pipeline-main (B2B2CIntermediaryFinder, produce route)

Design Invariant: never duplicate this dict inline.  A drift between services
causes geo-targeting to diverge silently across the funnel.
"""

# Maps lowercase keyword → (display_location_string, Serper gl code)
GL_MAP: dict[str, tuple[str, str]] = {
    "india":         ("India",     "in"),
    "usa":           ("USA",       "us"),
    "united states": ("USA",       "us"),
    "uk":            ("UK",        "gb"),
    "united kingdom":("UK",        "gb"),
    "canada":        ("Canada",    "ca"),
    "australia":     ("Australia", "au"),
    "germany":       ("Germany",   "de"),
    "singapore":     ("Singapore", "sg"),
    "uae":           ("UAE",       "ae"),
    "dubai":         ("UAE",       "ae"),
    "global":        ("",          ""),
}

# Serper-accepted gl code → human-readable label (inverse lookup for display)
GL_DISPLAY: dict[str, str] = {v[1]: v[0] for v in GL_MAP.values() if v[1]}


def resolve_geo(location_hint: str) -> tuple[str, str]:
    """Resolve a free-text location hint to ``(location_str, gl_code)``.

    Performs case-insensitive substring matching against :data:`GL_MAP`.
    Returns ``("", "")`` if no match found (global / unresolvable).

    Args:
        location_hint: Free-text location string from campaign or persona.

    Returns:
        Tuple of ``(location_string, gl_code)`` suitable for Serper API.
    """
    hint = location_hint.strip().lower()
    if not hint or hint == "global":
        return "", ""
    for keyword, vals in GL_MAP.items():
        if keyword in hint:
            return vals
    return "", ""
