-- LeadGenie V25.2.1 — BigQuery Table DDL
-- V25.2.1 fix: replaced hardcoded project ID with shell variable substitution.
--
-- Usage (run via setup_v25_2_0.sh which injects $PROJECT_ID):
--   PROJECT_ID=$(gcloud config get-value project)
--   sed "s/\${PROJECT_ID}/${PROJECT_ID}/g" infra/bq_v25_2_0_tables.sql \
--       | bq query --use_legacy_sql=false
--
-- Or run directly in the GCP Console BigQuery editor (substitute PROJECT_ID first).

-- Ensure dataset exists (idempotent — safe to re-run)
CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.swarm_analytics`
OPTIONS(description='LeadGenie OSINT analytics — raw signals, clusters, click events');

-- Table 1: raw_signals
-- Every scored signal from signal_harvest, all tiers (HIGH/MEDIUM/LOW).
-- Partitioned by date for cost efficiency.
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.swarm_analytics.raw_signals` (
  signal_id      STRING    NOT NULL  OPTIONS(description='UUID for deduplication'),
  campaign_id    STRING    NOT NULL  OPTIONS(description='Campaign that harvested this signal'),
  tenant_id      STRING    NOT NULL  OPTIONS(description='Tenant isolation key'),
  url            STRING              OPTIONS(description='Source URL'),
  source_type    STRING              OPTIONS(description='reddit/linkedin/x/classified/rss/hn/youtube/google_review/serper_url'),
  snippet_text   STRING              OPTIONS(description='Raw signal content (snippet or full text, max 2000 chars)'),
  content_source STRING              OPTIONS(description='full_text / serper_snippet / rss_entry / youtube_api / google_review / api_direct'),
  social_platform STRING             OPTIONS(description='linkedin / x / facebook / instagram / youtube / google_maps / empty for non-social'),
  inline_score   FLOAT64             OPTIONS(description='Gemini inline intent score 0-100'),
  intent_tier    STRING              OPTIONS(description='HIGH / MEDIUM / LOW'),
  geo            STRING              OPTIONS(description='Target geography from campaign'),
  topic_keywords STRING              OPTIONS(description='JSON array of Gemini-extracted topic keywords'),
  harvested_at   TIMESTAMP           OPTIONS(description='UTC timestamp when signal was collected'),
  archetype      STRING              OPTIONS(description='B2B / B2C / D2C / B2B2C')
)
PARTITION BY DATE(harvested_at)
CLUSTER BY tenant_id, campaign_id, source_type
OPTIONS(description='V25.2.0: Raw intent signals from all harvest sources. Input for signal_cluster_analyst.');

-- Table 2: intent_clusters
-- Gemini-derived intent clusters from correlated signals.
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.swarm_analytics.intent_clusters` (
  cluster_id        STRING    NOT NULL  OPTIONS(description='UUID'),
  campaign_id       STRING    NOT NULL,
  tenant_id         STRING    NOT NULL,
  cluster_label     STRING              OPTIONS(description='Short Gemini-generated label e.g. Interior design intent Muscat B2C'),
  signal_count      INT64               OPTIONS(description='Number of signals in this cluster'),
  source_diversity  INT64               OPTIONS(description='Number of distinct source_type values in cluster'),
  convergence_score FLOAT64             OPTIONS(description='0-100: signal_count x source_diversity x recency_decay'),
  intent_summary    STRING              OPTIONS(description='Gemini 2-3 sentence buyer intent reverse-engineer'),
  buyer_profile     STRING              OPTIONS(description='Gemini: who is expressing this intent'),
  geo               STRING,
  signal_urls       STRING              OPTIONS(description='JSON array of contributing signal URLs'),
  signal_snippets   STRING              OPTIONS(description='JSON array of contributing signal texts'),
  signal_platforms  STRING              OPTIONS(description='JSON array of contributing platform names'),
  clustered_at      TIMESTAMP           OPTIONS(description='When clustering ran'),
  lead_created      BOOL                OPTIONS(description='True if this cluster became a Firestore lead'),
  lead_id           STRING              OPTIONS(description='Firestore lead ID if lead_created=True')
)
PARTITION BY DATE(clustered_at)
CLUSTER BY tenant_id, campaign_id
OPTIONS(description='V25.2.0: Gemini intent clusters. Source for cluster-type leads.');

-- Table 3: click_events
-- Tracks when users click personal token (social passthrough) links.
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.swarm_analytics.click_events` (
  click_id    STRING    NOT NULL  OPTIONS(description='UUID'),
  lead_id     STRING              OPTIONS(description='Lead that generated the token'),
  tenant_id   STRING    NOT NULL  OPTIONS(description='Tenant isolation'),
  url         STRING              OPTIONS(description='Destination URL that was opened'),
  platform    STRING              OPTIONS(description='linkedin / x / facebook / reddit / etc.'),
  clicked_at  TIMESTAMP           OPTIONS(description='UTC click timestamp')
)
PARTITION BY DATE(clicked_at)
CLUSTER BY tenant_id, lead_id
OPTIONS(description='V25.2.0: Click-through tracking for social passthrough tokens.');