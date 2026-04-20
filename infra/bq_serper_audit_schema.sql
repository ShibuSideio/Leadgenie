-- BigQuery DDL: lead-sniper-prod:swarm_analytics.serper_audit_logs
--
-- Purpose: Per-query audit trail for every Serper API call fired by the
--          pipeline-main PRISM search loop (search_serper() interceptor).
--
-- Partition: DAY on `timestamp`  — enables cheap date-range reads.
-- Cluster:   campaign_id, tenant_id — speeds up per-campaign and per-tenant slices.
--
-- To provision (idempotent):
--   bq mk --project_id=lead-sniper-prod \
--          --dataset_id=swarm_analytics \
--          --table=serper_audit_logs \
--          --schema=infra/bq_serper_audit_schema.json \
--          --time_partitioning_field=timestamp \
--          --time_partitioning_type=DAY \
--          --clustering_fields=campaign_id,tenant_id

CREATE TABLE IF NOT EXISTS `lead-sniper-prod.swarm_analytics.serper_audit_logs`
(
  timestamp          TIMESTAMP NOT NULL,
  campaign_id        STRING    NOT NULL,
  tenant_id          STRING    NOT NULL,
  raw_query          STRING    NOT NULL,
  serper_parameters  JSON,
  result_count       INTEGER,
  credit_cost        INTEGER,
  engine             STRING,     -- 'search' | 'places' | 'news'
  serper_status_code INTEGER,    -- HTTP status from Serper (200, 429, etc.)
  error_message      STRING      -- populated on non-200, else NULL
)
PARTITION BY DATE(timestamp)
CLUSTER BY campaign_id, tenant_id
OPTIONS (
  description = 'Per-query Serper API audit log. One row per outbound search call.',
  partition_expiration_days = 365
);
