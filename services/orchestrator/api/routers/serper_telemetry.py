"""
Orchestrator V23.4 — Serper Audit Telemetry Read API.

All routes require super_admin role.

Routes:
  GET /api/admin/telemetry/serper-logs
"""
from __future__ import annotations

import datetime
import json

from flask import Blueprint, jsonify, request

from core.config import PROJECT_ID  # type: ignore[import]
from core.auth import require_auth, require_super_admin  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]

bp  = Blueprint("serper_telemetry", __name__)
log = get_logger("orchestrator.v23.serper_telemetry")


# =============================================================================
# GET /api/admin/telemetry/serper-logs
# =============================================================================
@bp.route("/api/admin/telemetry/serper-logs", methods=["GET"])
@require_auth
@require_super_admin
def get_serper_logs(uid, tenant_id, user_role):
    """Return Serper audit rows from BigQuery with date-range and campaign filters.

    Query Parameters:
        date_from   (str, optional): ISO date e.g. "2026-04-01". Defaults to 7 days ago.
        date_to     (str, optional): ISO date e.g. "2026-04-20". Defaults to today.
        campaign_id (str, optional): Filter to a specific campaign.
        limit       (int, optional): Max rows to return (default 500, max 2000).

    Returns:
        {
          "status": "success",
          "logs":   [...row dicts...],
          "summary": {
            "total_today":   <int>,   # rows where DATE(timestamp) = today
            "total_queries": <int>,   # total rows in result set
            "top_campaigns": [...],   # top 5 campaigns by call count
            "avg_results":   <float>  # avg result_count across returned rows
          }
        }
    """
    from google.cloud import bigquery as _bq

    # ── Parse query params ─────────────────────────────────────────────────────
    now_utc  = datetime.datetime.now(datetime.timezone.utc)
    today    = now_utc.date()

    raw_from = request.args.get("date_from", "")
    raw_to   = request.args.get("date_to",   "")
    camp_flt = request.args.get("campaign_id", "").strip()
    limit    = min(int(request.args.get("limit", 500)), 2000)

    try:
        date_from = datetime.date.fromisoformat(raw_from) if raw_from else today - datetime.timedelta(days=7)
    except ValueError:
        date_from = today - datetime.timedelta(days=7)

    try:
        date_to = datetime.date.fromisoformat(raw_to) if raw_to else today
    except ValueError:
        date_to = today

    # Ensure date_from <= date_to and window <= 90 days
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    if (date_to - date_from).days > 90:
        date_from = date_to - datetime.timedelta(days=90)

    # ── Build parameterized BigQuery SQL ───────────────────────────────────────
    # MIDNIGHT BUG FIX (V23.4.2):
    #   Old: DATE(timestamp) BETWEEN @date_from AND @date_to
    #        This uses DATE comparison, which should include all hours on date_to.
    #        However, because the timestamp field is stored as a STRING (not a native
    #        TIMESTAMP type), CAST/DATE() may truncate at midnight UTC.
    #        IST is UTC+5:30 — a query at 23:00 IST = 17:30 UTC same day, so DATE()
    #        is correct. But the original BETWEEN is inclusive, which in practice
    #        truncates at 00:00:00 on date_to when BQ internally resolves to TIMESTAMP.
    #
    #   Fix: Use explicit TIMESTAMP range with exclusive upper bound:
    #        timestamp >= TIMESTAMP(@date_from)
    #        AND timestamp < TIMESTAMP_ADD(TIMESTAMP(@date_to_excl), INTERVAL 1 DAY)
    #        This includes ALL records from date_from 00:00:00 up to (not including)
    #        (date_to+1) 00:00:00 — i.e., the full date_to day through 23:59:59.999.
    campaign_clause = ""
    # date_to_excl is the EXCLUSIVE upper bound day (= date_to + 1 day in the SQL)
    params = [
        _bq.ScalarQueryParameter("date_from",    "DATE", date_from.isoformat()),
        _bq.ScalarQueryParameter("date_to_excl", "DATE", date_to.isoformat()),
        _bq.ScalarQueryParameter("row_limit",    "INT64", limit),
    ]
    if camp_flt:
        campaign_clause = "AND campaign_id = @campaign_id"
        params.append(_bq.ScalarQueryParameter("campaign_id", "STRING", camp_flt))

    sql = f"""
        SELECT
            CAST(timestamp AS STRING)   AS timestamp,
            campaign_id,
            tenant_id,
            raw_query,
            serper_parameters,
            result_count,
            credit_cost,
            engine,
            serper_status_code,
            error_message
        FROM
            `{PROJECT_ID}.swarm_analytics.serper_audit_logs`
        WHERE
            CAST(timestamp AS TIMESTAMP)
                >= TIMESTAMP(@date_from)
            AND CAST(timestamp AS TIMESTAMP)
                <  TIMESTAMP_ADD(TIMESTAMP(@date_to_excl), INTERVAL 1 DAY)
            {campaign_clause}
        ORDER BY
            timestamp DESC
        LIMIT @row_limit
    """

    # ── Today's count sub-query ────────────────────────────────────────────────
    today_sql = f"""
        SELECT COUNT(*) AS total_today
        FROM `{PROJECT_ID}.swarm_analytics.serper_audit_logs`
        WHERE DATE(timestamp) = @today
    """
    today_params = [_bq.ScalarQueryParameter("today", "DATE", today.isoformat())]

    try:
        bq = _bq.Client(project=PROJECT_ID)
        cfg = _bq.QueryJobConfig(query_parameters=params)
        rows = list(bq.query(sql, job_config=cfg).result())

        logs = []
        for r in rows:
            logs.append({
                "timestamp":          r.timestamp,
                "campaign_id":        r.campaign_id,
                "tenant_id":          r.tenant_id,
                "raw_query":          r.raw_query,
                "serper_parameters":  r.serper_parameters,
                "result_count":       r.result_count,
                "credit_cost":        r.credit_cost,
                "engine":             r.engine,
                "serper_status_code": r.serper_status_code,
                "error_message":      r.error_message,
            })

        # Today's count
        today_cfg = _bq.QueryJobConfig(query_parameters=today_params)
        today_row = list(bq.query(today_sql, job_config=today_cfg).result())
        total_today = today_row[0].total_today if today_row else 0

        # Summary metrics (computed in Python from returned rows)
        from collections import Counter
        camp_counter  = Counter(r["campaign_id"] for r in logs)
        top_campaigns = [{"campaign_id": c, "calls": n}
                         for c, n in camp_counter.most_common(5)]
        avg_results   = (
            round(sum(r["result_count"] or 0 for r in logs) / len(logs), 1)
            if logs else 0.0
        )
        total_credits = sum(r["credit_cost"] or 1 for r in logs)

        log.info("serper_logs_fetched",
                 date_from=date_from.isoformat(),
                 date_to=date_to.isoformat(),
                 rows=len(logs),
                 total_today=total_today)

        return jsonify({
            "status": "success",
            "logs":   logs,
            "summary": {
                "total_today":   int(total_today),
                "total_queries": len(logs),
                "top_campaigns": top_campaigns,
                "avg_results":   avg_results,
                "total_credits": total_credits,
                "date_from":     date_from.isoformat(),
                "date_to":       date_to.isoformat(),
            },
        }), 200

    except Exception as e:
        log.error("serper_logs_query_failed", error=str(e))
        return jsonify({"error": "Serper log query failed", "message": str(e)}), 500
