"""
Campaign work-queue helpers (V27.3.0) — keep campaign docs lean.

Primary queue remains ``campaigns/{id}.unprocessed_queue`` (URL strings) for BC.
Additionally dual-writes to ``campaigns/{id}/queue_items/{sha16}`` so operators
can inspect/drain without bloating the parent, and so a future cutover can drop
the array field.

Also estimates approximate queue payload size for telemetry.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Optional


def url_item_id(url: str) -> str:
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]


def approx_queue_bytes(urls: Iterable[str]) -> int:
    n = 0
    for u in urls:
        n += len((u or "").encode("utf-8")) + 8  # array overhead approx
    return n


def dual_write_queue_items(
    db: Any,
    campaign_id: str,
    urls: list[str],
    *,
    source: str = "produce",
    log: Optional[Any] = None,
) -> int:
    """Write queue item docs (merge). Non-fatal. Returns count written."""
    if not campaign_id or not urls:
        return 0
    written = 0
    try:
        from google.cloud import firestore  # type: ignore[import]

        col = db.collection("campaigns").document(campaign_id).collection("queue_items")
        batch = db.batch()
        ops = 0
        for u in urls:
            u = (u or "").strip()
            if not u:
                continue
            ref = col.document(url_item_id(u))
            batch.set(
                ref,
                {
                    "url": u,
                    "source": source,
                    "status": "queued",
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                    "createdAt": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            ops += 1
            written += 1
            if ops >= 400:
                batch.commit()
                batch = db.batch()
                ops = 0
        if ops:
            batch.commit()
    except Exception as exc:
        if log:
            log.warning(
                "campaign_queue_dual_write_failed",
                campaign_id=campaign_id,
                error=str(exc),
            )
        return 0
    return written


def mark_queue_items_consumed(
    db: Any,
    campaign_id: str,
    urls: list[str],
    *,
    log: Optional[Any] = None,
) -> None:
    """Mark dual-write items consumed when dispatch pops them."""
    if not campaign_id or not urls:
        return
    try:
        from google.cloud import firestore  # type: ignore[import]

        col = db.collection("campaigns").document(campaign_id).collection("queue_items")
        for u in urls:
            u = (u or "").strip()
            if not u:
                continue
            try:
                col.document(url_item_id(u)).set(
                    {"status": "consumed", "consumedAt": firestore.SERVER_TIMESTAMP},
                    merge=True,
                )
            except Exception:
                pass
    except Exception as exc:
        if log:
            log.warning(
                "campaign_queue_mark_consumed_failed",
                campaign_id=campaign_id,
                error=str(exc),
            )
