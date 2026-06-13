# Firestore TTL Policy Setup — Postmortem Fixes #2 & #12

Firestore TTL policies cannot be set via application code — they must be configured
once per collection via the GCP Console or `gcloud` CLI.

## Why This Matters

| Collection | Problem Without TTL | Documents/Year |
|---|---|---|
| `global_lead_locks` | Domain blacklisted for 14d on WAF false-positive; ~547k stale docs | 547,500 |
| `scraped_cache` | Unbounded WalledGarden text cache grows forever | 547,500 |
| `autonomous_dedup` | 60-day dedup window enforced in code, not Firestore | ~200,000 |

---

## Option A: GCP Console (Recommended, UI-friendly)

1. Go to: https://console.cloud.google.com/firestore/databases/-default-/ttl
2. Click **Create Policy**
3. Fill in for each collection:

### global_lead_locks.expire_at
- **Collection group**: `global_lead_locks`
- **Timestamp field**: `expire_at`
- Click **Create**

### scraped_cache.expire_at
- **Collection group**: `scraped_cache`
- **Timestamp field**: `expire_at`
- Click **Create**

### autonomous_dedup.expire_at
- **Collection group**: `autonomous_dedup`
- **Timestamp field**: `expire_at`
- Click **Create**
- **Note**: After this, add `expire_at` to autonomous dedup writes in `engine.py`
  (currently uses 60-day window enforced in Python, not TTL policy).

---

## Option B: gcloud CLI

```bash
# global_lead_locks
gcloud firestore fields ttls update expire_at \
  --collection-group=global_lead_locks \
  --project=sideio-leads-v16

# scraped_cache
gcloud firestore fields ttls update expire_at \
  --collection-group=scraped_cache \
  --project=sideio-leads-v16

# autonomous_dedup
gcloud firestore fields ttls update expire_at \
  --collection-group=autonomous_dedup \
  --project=sideio-leads-v16
```

---

## Verification

After enabling:
- TTL policies become active within 24-48 hours (Firestore SLA)
- Documents with `expire_at` in the past are deleted lazily (within minutes to hours)
- You can monitor deletion via: Console → Firestore → Usage → Document deletes/day

---

## What the Code Already Does

After the postmortem fixes:
- `dispatch.py` writes `expire_at` on every `global_lead_locks` document (14d TTL)
- `prism_pipeline.py` writes `expire_at` on every `scraped_cache` document (30d TTL)
- `engine.py` writes `expire_at` on every `predictive_cache` document (72h TTL)

You just need to **activate the GCP policy** above to enable auto-deletion.

---

## BigQuery Row Erasure (DPDP Compliance)

The `handle_purge` function now fully erases all Firestore data for a tenant.
BigQuery rows require a separate scheduled DELETE job (not auto-purged by Firestore TTL):

```sql
-- Run in BigQuery after each erasure request
DELETE FROM `sideio-leads-v16.swarm_analytics.rlhf_events`
WHERE tenant_id = '<TENANT_ID>';

DELETE FROM `sideio-leads-v16.swarm_analytics.Negative_Signals`
WHERE tenant_id = '<TENANT_ID>';

DELETE FROM `sideio-leads-v16.swarm_analytics.Intent_Keywords`
WHERE tenant_id = '<TENANT_ID>';
```

Wire this as a Cloud Function triggered by the `/purge` endpoint response, or run
manually as part of the DPDP erasure process.
