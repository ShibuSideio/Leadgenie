"""
BQ Reprovisioning — urllib only (no google-cloud-bigquery SDK needed).
Uses the BigQuery REST API with ADC token from gcloud.
"""
import json, subprocess, sys, urllib.request, urllib.error

LOCATION = "asia-south1"

# ── Get ADC access token from gcloud ───────────────────────────────────────
def get_token():
    r = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, shell=True
    )
    token = r.stdout.strip()
    if not token:
        print("ERROR: could not get access token:", r.stderr)
        sys.exit(1)
    return token

def api(method, url, body=None, token=None):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return (json.loads(raw) if raw else {}), resp.status
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        if e.code == 404 and method == "DELETE":
            return {}, 404
        print(f"  HTTP {e.code}: {body_txt[:400]}")
        return None, e.code

BASE = "https://bigquery.googleapis.com/bigquery/v2"

# ── Schema definitions (REST format) ───────────────────────────────────────

SERPER_AUDIT_FIELDS = [
    {"name":"timestamp",          "type":"TIMESTAMP","mode":"REQUIRED"},
    {"name":"campaign_id",        "type":"STRING",   "mode":"REQUIRED"},
    {"name":"tenant_id",          "type":"STRING",   "mode":"REQUIRED"},
    {"name":"raw_query",          "type":"STRING",   "mode":"REQUIRED"},
    {"name":"serper_parameters",  "type":"JSON",     "mode":"NULLABLE"},
    {"name":"result_count",       "type":"INTEGER",  "mode":"NULLABLE"},
    {"name":"credit_cost",        "type":"INTEGER",  "mode":"NULLABLE"},
    {"name":"engine",             "type":"STRING",   "mode":"NULLABLE"},
    {"name":"serper_status_code", "type":"INTEGER",  "mode":"NULLABLE"},
    {"name":"error_message",      "type":"STRING",   "mode":"NULLABLE"},
]

SHADOW_TRACK_FIELDS = [
    {"name":"tenant_id",       "type":"STRING",    "mode":"REQUIRED"},
    {"name":"campaign_id",     "type":"STRING",    "mode":"REQUIRED"},
    {"name":"lead_id",         "type":"STRING",    "mode":"REQUIRED"},
    {"name":"url",             "type":"STRING",    "mode":"NULLABLE"},
    {"name":"score",           "type":"INTEGER",   "mode":"REQUIRED"},
    {"name":"sourcing_vector", "type":"STRING",    "mode":"NULLABLE"},
    {"name":"pain_point",      "type":"STRING",    "mode":"NULLABLE"},
    {"name":"prism_mode",      "type":"STRING",    "mode":"NULLABLE"},
    {"name":"stage",           "type":"STRING",    "mode":"REQUIRED"},
    {"name":"timestamp",       "type":"TIMESTAMP", "mode":"REQUIRED"},
]

RLHF_EVENTS_FIELDS = [
    {"name":"event_id",           "type":"STRING",    "mode":"NULLABLE"},
    {"name":"timestamp",          "type":"TIMESTAMP", "mode":"NULLABLE"},
    {"name":"tenant_id",          "type":"STRING",    "mode":"NULLABLE"},
    {"name":"prism_mode",         "type":"STRING",    "mode":"NULLABLE"},
    {"name":"conversion_status",  "type":"STRING",    "mode":"NULLABLE"},
    {"name":"intent_hash",        "type":"STRING",    "mode":"NULLABLE"},
    {"name":"raw_signal_payload", "type":"STRING",    "mode":"NULLABLE"},
]

INTENT_KEYWORDS_FIELDS = [
    {"name":"persona_category", "type":"STRING",    "mode":"NULLABLE"},
    {"name":"n_gram",           "type":"STRING",    "mode":"NULLABLE"},
    {"name":"occurrence_count", "type":"INTEGER",   "mode":"NULLABLE"},
    {"name":"yield_weight",     "type":"FLOAT",     "mode":"NULLABLE"},
    {"name":"tenant_id",        "type":"STRING",    "mode":"NULLABLE"},
    {"name":"last_seen",        "type":"TIMESTAMP", "mode":"NULLABLE"},
]

NEGATIVE_SIGNALS_FIELDS = [
    {"name":"entity_name",      "type":"STRING",    "mode":"NULLABLE"},
    {"name":"root_domain",      "type":"STRING",    "mode":"NULLABLE"},
    {"name":"rejection_reason", "type":"STRING",    "mode":"NULLABLE"},
    {"name":"tenant_id",        "type":"STRING",    "mode":"NULLABLE"},
    {"name":"timestamp",        "type":"TIMESTAMP", "mode":"NULLABLE"},
]

def delete_dataset(project, dataset, token):
    url = f"{BASE}/projects/{project}/datasets/{dataset}?deleteContents=true"
    _, code = api("DELETE", url, token=token)
    if code in (200, 204, 404):
        print(f"  [DELETED] {project}:{dataset} (code {code})")
    else:
        print(f"  [WARN] delete returned {code} — continuing")

def create_dataset(project, dataset, token):
    url  = f"{BASE}/projects/{project}/datasets"
    body = {
        "datasetReference": {"projectId": project, "datasetId": dataset},
        "location": LOCATION,
        "description": f"Sideio swarm analytics — {LOCATION}",
    }
    resp, code = api("POST", url, body, token=token)
    if code in (200, 409):  # 409 = already exists
        print(f"  [DATASET OK] {project}:{dataset} @ {LOCATION}")
    else:
        print(f"  [ERROR] create dataset {code}: {resp}")
        sys.exit(1)

def create_table(project, dataset, table, fields, partition_field=None,
                 cluster_fields=None, partition_expiry_ms=None, token=None):
    url  = f"{BASE}/projects/{project}/datasets/{dataset}/tables"
    body = {
        "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
        "schema": {"fields": fields},
    }
    if partition_field:
        body["timePartitioning"] = {
            "type": "DAY",
            "field": partition_field,
            **({"expirationMs": str(partition_expiry_ms)} if partition_expiry_ms else {}),
        }
    if cluster_fields:
        body["clustering"] = {"fields": cluster_fields}
    resp, code = api("POST", url, body, token=token)
    if code == 200:
        print(f"  [TABLE OK] {project}.{dataset}.{table}")
    elif code == 409:
        print(f"  [TABLE EXISTS] {project}.{dataset}.{table}")
    else:
        print(f"  [ERROR] create table {table} code {code}: {resp}")
        sys.exit(1)

def run():
    print("Fetching gcloud access token...")
    token = get_token()
    print(f"Token OK ({token[:12]}...)\n")

    # ── lead-sniper-prod ────────────────────────────────────────────────────
    print("=== lead-sniper-prod ===")
    delete_dataset("lead-sniper-prod", "swarm_analytics", token)
    create_dataset("lead-sniper-prod", "swarm_analytics", token)

    create_table("lead-sniper-prod", "swarm_analytics", "serper_audit_logs",
                 SERPER_AUDIT_FIELDS,
                 partition_field="timestamp",
                 cluster_fields=["campaign_id","tenant_id"],
                 partition_expiry_ms=365*86400*1000,
                 token=token)

    create_table("lead-sniper-prod", "swarm_analytics", "shadow_track_events",
                 SHADOW_TRACK_FIELDS,
                 partition_field="timestamp",
                 cluster_fields=["tenant_id","campaign_id"],
                 partition_expiry_ms=730*86400*1000,
                 token=token)

    create_table("lead-sniper-prod", "swarm_analytics", "rlhf_events",
                 RLHF_EVENTS_FIELDS,
                 partition_field="timestamp",
                 cluster_fields=["tenant_id"],
                 token=token)

    # ── sideio-leads-v16 → lead-sniper-prod (project decommissioned) ────────
    # Intent_Keywords and Negative_Signals are queried via PROJECT_ID env var
    # which resolves to lead-sniper-prod in production. Provision there.
    print("\n=== lead-sniper-prod (RLHF tables — was sideio-leads-v16) ===")

    create_table("lead-sniper-prod", "swarm_analytics", "Intent_Keywords",
                 INTENT_KEYWORDS_FIELDS,
                 partition_field="last_seen",
                 token=token)

    create_table("lead-sniper-prod", "swarm_analytics", "Negative_Signals",
                 NEGATIVE_SIGNALS_FIELDS,
                 partition_field="timestamp",
                 token=token)

    print("\n✅  All done. swarm_analytics provisioned in asia-south1.")
    print("    Tables: serper_audit_logs, shadow_track_events, rlhf_events,")
    print("            Intent_Keywords, Negative_Signals")
    print("    Project: lead-sniper-prod  |  Location: asia-south1")

if __name__ == "__main__":
    run()
