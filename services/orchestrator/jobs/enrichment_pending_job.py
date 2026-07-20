"""
V27.3.0 — Resume or expire enrichment_pending leads.

Problem: dispatch parks thin leads as enrichment_pending; they occupy velocity
quota and never re-enter the pipeline.

Policy:
  - Age < resume_after_hours: skip (give concurrent mesh time)
  - resume_after_hours ≤ age < expire_after_hours: requeue URL + status=queued + dispatch task
  - age ≥ expire_after_hours: status=scored_out reason=enrichment_stale

Triggered by POST /api/internal/cron/enrichment-pending-resume
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from core.clients import get_db  # type: ignore[import]
from core.config import LOCATION, ORCHESTRATOR_SA_EMAIL, PIPELINE_URL, PROJECT_ID, QUEUE  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]

log = get_logger("orchestrator.jobs.enrichment_pending")

RESUME_AFTER_HOURS = int(os.environ.get("ENRICHMENT_PENDING_RESUME_HOURS", "6"))
EXPIRE_AFTER_HOURS = int(os.environ.get("ENRICHMENT_PENDING_EXPIRE_HOURS", "168"))  # 7d
MAX_PER_RUN = int(os.environ.get("ENRICHMENT_PENDING_MAX_PER_RUN", "50"))


def _parse_ts(raw: Any) -> datetime.datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime.datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=datetime.timezone.utc)
        return raw
    if isinstance(raw, str):
        try:
            text = raw.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except ValueError:
            return None
    return None


def _dispatch_url(tenant_id: str, campaign_id: str, url: str) -> bool:
    if not PIPELINE_URL or not campaign_id or not url:
        return False
    try:
        from google.cloud import tasks_v2 as _tv2
        from core.clients import get_tasks_client  # type: ignore[import]

        tc = get_tasks_client()
        queue_path = tc.queue_path(PROJECT_ID, LOCATION, QUEUE)
        body = json.dumps({
            "tenant_id": tenant_id,
            "campaign_id": campaign_id,
            "force_url": url,
        }).encode()
        task: dict = {
            "http_request": {
                "http_method": _tv2.HttpMethod.POST,
                "url": f"{PIPELINE_URL}/dispatch",
                "headers": {"Content-Type": "application/json"},
                "body": body,
            },
        }
        if ORCHESTRATOR_SA_EMAIL:
            task["http_request"]["oidc_token"] = {
                "service_account_email": ORCHESTRATOR_SA_EMAIL,
                "audience": PIPELINE_URL,
            }
        tc.create_task(request={"parent": queue_path, "task": task})
        return True
    except Exception as exc:
        log.warning("enrichment_pending_dispatch_failed", error=str(exc), url=url[:80])
        return False


def run() -> dict:
    """Scan enrichment_pending leads and resume or expire."""
    db = get_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    resume_cutoff = now - datetime.timedelta(hours=RESUME_AFTER_HOURS)
    expire_cutoff = now - datetime.timedelta(hours=EXPIRE_AFTER_HOURS)

    # Cap scan for scale
    try:
        docs = list(
            db.collection("leads")
            .where(filter=FieldFilter("status", "==", "enrichment_pending"))
            .limit(MAX_PER_RUN * 3)
            .stream()
        )
    except Exception as exc:
        log.error("enrichment_pending_query_failed", error=str(exc))
        return {"error": str(exc), "resumed": 0, "expired": 0, "skipped": 0}

    resumed = expired = skipped = 0
    for doc in docs:
        if resumed + expired >= MAX_PER_RUN:
            break
        data = doc.to_dict() or {}
        ts = _parse_ts(data.get("updatedAt")) or _parse_ts(data.get("createdAt"))
        if ts is None:
            skipped += 1
            continue
        if ts > resume_cutoff:
            skipped += 1
            continue

        lead_id = doc.id
        tenant_id = str(data.get("tenant_id") or "")
        campaign_id = str(
            data.get("campaign_id")
            or (data.get("matched_campaigns") or [None])[0]
            or ""
        )
        url = str(data.get("source_url") or data.get("url") or "").strip()

        if ts <= expire_cutoff:
            try:
                doc.reference.update({
                    "status": "scored_out",
                    "scored_out_reason": "enrichment_stale",
                    "enrichment_expired_at": now.isoformat(),
                    "updatedAt": now,
                })
                expired += 1
                log.info(
                    "enrichment_pending_expired",
                    lead_id=lead_id,
                    age_hours=round((now - ts).total_seconds() / 3600, 1),
                )
            except Exception as exc:
                log.warning("enrichment_pending_expire_failed", lead_id=lead_id, error=str(exc))
            continue

        # Resume: requeue + optional dispatch
        if not url or not tenant_id:
            skipped += 1
            continue
        try:
            updates = {
                "status": "queued",
                "enrichment_resume_count": int(data.get("enrichment_resume_count") or 0) + 1,
                "enrichment_resumed_at": now.isoformat(),
                "updatedAt": now,
            }
            # Cap resume attempts
            if updates["enrichment_resume_count"] > 3:
                doc.reference.update({
                    "status": "scored_out",
                    "scored_out_reason": "enrichment_resume_exhausted",
                    "updatedAt": now,
                })
                expired += 1
                continue
            doc.reference.update(updates)
            # Re-append URL to campaign queue for normal dispatch path
            if campaign_id:
                try:
                    from google.cloud import firestore as _fs
                    db.collection("campaigns").document(campaign_id).update({
                        "unprocessed_queue": _fs.ArrayUnion([url]),
                    })
                except Exception:
                    pass
            _dispatch_url(tenant_id, campaign_id, url)
            resumed += 1
            log.info(
                "enrichment_pending_resumed",
                lead_id=lead_id,
                campaign_id=campaign_id,
                resume_count=updates["enrichment_resume_count"],
            )
        except Exception as exc:
            log.warning("enrichment_pending_resume_failed", lead_id=lead_id, error=str(exc))

    result = {
        "scanned": len(docs),
        "resumed": resumed,
        "expired": expired,
        "skipped": skipped,
        "resume_after_hours": RESUME_AFTER_HOURS,
        "expire_after_hours": EXPIRE_AFTER_HOURS,
    }
    log.info("enrichment_pending_job_complete", **result)
    return result
