"""
Sideio Lead Sniper — Shared cross-service utilities.

Canonical single source of truth for symbols used in both orchestrator
and pipeline-main.  Import from here; never duplicate inline.

Usage (in any service):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from shared.base_path import parse_base_path, SOCIAL_ONTOLOGY_DOMAINS
    from shared.geo_map   import GL_MAP
    from shared.tech_signatures import TECH_SIGNATURES
    from shared.domain_constants import KNOWN_DOMAIN_FAMILIES, is_valid_domain_family
"""
