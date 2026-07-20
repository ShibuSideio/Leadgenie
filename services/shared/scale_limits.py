"""
Scale limits for 1000+ tenant operation (V27.2.0).

Central constants so produce, harvest, dispatch, and orchestrator share caps.
"""
from __future__ import annotations

# Campaign work queue (URLs on campaign doc — keep lean; strings only)
UNPROCESSED_QUEUE_HARD_CAP = 200
UNPROCESSED_QUEUE_BACKPRESSURE = 150

# Produce dedup: scan more docs under multi-tenant load (was 500 → re-queue risk)
DEDUP_SCAN_LIMIT = 2500
DEDUP_SCAN_PAGE_SIZE = 500

# Novelty memory signatures on campaign doc
QUERY_NOVELTY_MEMORY_CAP = 80

# Unbounded ArrayUnion guards
DYNAMIC_BLOCKLIST_CAP = 500
KNOWLEDGE_BASE_TEXT_ENTRIES_CAP = 100
LEAD_INTERACTIONS_CAP = 200

# Entity extraction multi-instance rate (Firestore-backed counter)
ENTITY_DOMAIN_MAX_PER_DAY = 40  # across all instances
ENTITY_DOMAIN_MAX_PER_BATCH = 5  # soft per-process batch hint

# Reviews / maps
GOOGLE_REVIEWS_MAX_COMPETITORS = 5
INBOUND_MAPS_MAX_QUERIES = 3
INBOUND_MAPS_MAX_PLACES = 5
