-- SF-013: shadow_track_events — qualified lead quality signal table
--
-- Purpose:
--   One row per lead that passes the Gemini score gate in /dispatch.
--   Feeds the RLHF statistical router in query_brain.py:
--     - pain_point phrases → n-gram reinforcement for Intent_Keywords
--     - prism_mode distribution → informs PRISM hook routing weights
--     - score distribution per sourcing_vector → informs vector selection
--
-- Previously only the produce-side (URL discovery) was tracked.  Without
-- dispatch outcomes, the RLHF router had no signal on which generated
-- queries actually produced qualified leads — a critical feedback gap.
--
-- Partition: DAY on timestamp  (cheap date-range reads for RLHF jobs)
-- Cluster:   tenant_id, campaign_id  (per-tenant and per-campaign slices)
--
-- To provision (idempotent):
--   bq mk --project_id=lead-sniper-prod \
--          --dataset_id=swarm_analytics \
--          --table=shadow_track_events \
--          --time_partitioning_field=timestamp \
--          --time_partitioning_type=DAY \
--          --clustering_fields=tenant_id,campaign_id \
--          --schema=infra/bq_shadow_track_events_schema.json

CREATE TABLE IF NOT EXISTS `lead-sniper-prod.swarm_analytics.shadow_track_events`
(
  tenant_id         STRING    NOT NULL  OPTIONS(description='Tenant UID'),
  campaign_id       STRING    NOT NULL  OPTIONS(description='Campaign UID'),
  lead_id           STRING    NOT NULL  OPTIONS(description='SHA-256 lead dedup key'),
  url               STRING              OPTIONS(description='Source URL, max 500 chars'),
  score             INTEGER   NOT NULL  OPTIONS(description='Gemini quality score 0-10'),
  sourcing_vector   STRING              OPTIONS(description='PRISM vector label, e.g. Classic B2B'),
  pain_point        STRING              OPTIONS(description='AI-extracted pain point phrase, max 1000 chars'),
  prism_mode        STRING              OPTIONS(description='PRISM hook that served the URL, e.g. WalledGarden'),
  stage             STRING    NOT NULL  OPTIONS(description='"produce" | "dispatch" — pipeline stage of origin'),
  timestamp         TIMESTAMP NOT NULL  OPTIONS(description='UTC event time')
)
PARTITION BY DATE(timestamp)
CLUSTER BY tenant_id, campaign_id
OPTIONS (
  description = 'Qualified lead quality signal feed for RLHF statistical router.',
  partition_expiration_days = 730
);
