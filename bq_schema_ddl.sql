-- =============================================================================
-- HYBRID STARTER MOTOR — BigQuery DDL
-- Run once in project: sideio-leads-v16
-- Dataset: swarm_analytics
-- =============================================================================

-- Table 1: Intent_Keywords
-- Tracks N-gram signals extracted from approved (converted) lead pain_points.
-- Used by the Confidence Threshold Router to decide STATISTICAL vs GEMINI mode.
-- Partitioned by last_seen for cost-efficient daily scans.
CREATE TABLE IF NOT EXISTS `sideio-leads-v16.swarm_analytics.Intent_Keywords`
(
    persona_category  STRING    OPTIONS(description='Persona/campaign name — ML category key'),
    n_gram            STRING    OPTIONS(description='2-4 word phrase, e.g. struggling with'),
    occurrence_count  INT64     OPTIONS(description='Raw count of approved leads containing this n-gram'),
    yield_weight      FLOAT64   OPTIONS(description='Weighted confidence score. SUM per category drives router threshold'),
    tenant_id         STRING    OPTIONS(description='Tenant scope. GLOBAL = cross-tenant signal'),
    last_seen         TIMESTAMP OPTIONS(description='Last reinforcement timestamp')
)
PARTITION BY DATE(last_seen)
OPTIONS(require_partition_filter=FALSE);

-- Table 2: Negative_Signals (from previous session — idempotent)
CREATE TABLE IF NOT EXISTS `sideio-leads-v16.swarm_analytics.Negative_Signals`
(
    entity_name      STRING    OPTIONS(description='Company name or author display name'),
    root_domain      STRING    OPTIONS(description='Clean root domain, e.g. salesforce.com'),
    rejection_reason STRING    OPTIONS(description='competitor | author'),
    tenant_id        STRING    OPTIONS(description='Rejecting tenant. GLOBAL = cross-tenant suppression'),
    timestamp        TIMESTAMP OPTIONS(description='UTC insert time')
)
PARTITION BY DATE(timestamp)
OPTIONS(require_partition_filter=FALSE);
