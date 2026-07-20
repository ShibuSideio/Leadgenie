"""
Campaign work-queue SSOT (V27.4.0) — dual-path without feature breakage.

Storage:
  A) campaigns/{id}.unprocessed_queue  — legacy array of URL strings (BC)
  B) campaigns/{id}/queue_items/{sha16} — scalable subcollection docs

Modes (env CAMPAIGN_QUEUE_MODE):
  hybrid (default) — dual-read merge + dual-write; pop clears both
  subcollection    — read/write subcollection as primary; still ArrayRemove
                     any legacy array URLs on pop so old data drains
  array            — array only (emergency rollback)

Guarantees:
  - Empty either store alone still works
  - Pop is idempotent (ArrayRemove + mark consumed)
  - Depth/backpressure uses merged view
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Iterable, Optional


QUEUE_ITEMS_COLL = "queue_items"
STATUS_QUEUED = "queued"
STATUS_CONSUMED = "consumed"


def url_item_id(url: str) -> str:
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]


def approx_queue_bytes(urls: Iterable[str]) -> int:
    n = 0
    for u in urls:
        n += len((u or "").encode("utf-8")) + 8
    return n


def queue_mode() -> str:
    mode = (os.environ.get("CAMPAIGN_QUEUE_MODE") or "hybrid").strip().lower()
    if mode in ("hybrid", "subcollection", "array"):
        return mode
    return "hybrid"


def _hard_cap() -> int:
    try:
        from shared.scale_limits import UNPROCESSED_QUEUE_HARD_CAP  # type: ignore[import]
        return int(UNPROCESSED_QUEUE_HARD_CAP)
    except Exception:
        return 200


def _backpressure() -> int:
    try:
        from shared.scale_limits import UNPROCESSED_QUEUE_BACKPRESSURE  # type: ignore[import]
        return int(UNPROCESSED_QUEUE_BACKPRESSURE)
    except Exception:
        return 150


def _normalize_urls(urls: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        s = (u or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def load_queue_items_urls(
    db: Any,
    campaign_id: str,
    *,
    limit: int = 250,
    log: Optional[Any] = None,
) -> list[str]:
    """Load URLs from queue_items with status=queued."""
    if not campaign_id or db is None:
        return []
    try:
        col = db.collection("campaigns").document(campaign_id).collection(QUEUE_ITEMS_COLL)
        # Prefer equality filter; fall back to full scan of recent docs
        try:
            snaps = list(
                col.where("status", "==", STATUS_QUEUED).limit(limit).stream()
            )
        except Exception:
            snaps = list(col.limit(limit).stream())
        urls: list[str] = []
        for snap in snaps:
            data = snap.to_dict() or {}
            if data.get("status") and data.get("status") != STATUS_QUEUED:
                continue
            u = (data.get("url") or "").strip()
            if u:
                urls.append(u)
        return urls
    except Exception as exc:
        if log:
            try:
                log.warning("campaign_queue_items_load_failed", campaign_id=campaign_id, error=str(exc))
            except Exception:
                pass
        return []


def load_queued_urls(
    db: Any,
    campaign_id: str,
    campaign_doc: Optional[dict] = None,
    *,
    limit: Optional[int] = None,
    log: Optional[Any] = None,
) -> list[str]:
    """Merged view of array + subcollection (deduped, array order first)."""
    cap = limit if limit is not None else _hard_cap()
    mode = queue_mode()
    camp = campaign_doc if isinstance(campaign_doc, dict) else {}
    array_urls = _normalize_urls(camp.get("unprocessed_queue") or [])
    item_urls: list[str] = []
    if mode != "array" and db is not None and campaign_id:
        item_urls = _normalize_urls(
            load_queue_items_urls(db, campaign_id, limit=max(cap * 2, 50), log=log)
        )

    if mode == "array":
        return array_urls[:cap]
    if mode == "subcollection":
        # Prefer items; fall back to array so legacy campaigns still drain
        merged = item_urls if item_urls else array_urls
        # Also include array URLs not yet in items (migration window)
        if item_urls and array_urls:
            seen = set(item_urls)
            for u in array_urls:
                if u not in seen:
                    item_urls.append(u)
                    seen.add(u)
            merged = item_urls
        return merged[:cap]

    # hybrid: array order first, then items not already present
    seen = set(array_urls)
    merged = list(array_urls)
    for u in item_urls:
        if u not in seen:
            merged.append(u)
            seen.add(u)
    return merged[:cap]


def queue_depth(
    db: Any,
    campaign_id: str,
    campaign_doc: Optional[dict] = None,
    *,
    log: Optional[Any] = None,
) -> int:
    return len(load_queued_urls(db, campaign_id, campaign_doc, log=log))


def dual_write_queue_items(
    db: Any,
    campaign_id: str,
    urls: list[str],
    *,
    source: str = "produce",
    log: Optional[Any] = None,
) -> int:
    """Write/refresh queue_items as status=queued. Non-fatal."""
    if not campaign_id or not urls or db is None:
        return 0
    written = 0
    try:
        from google.cloud import firestore  # type: ignore[import]

        col = db.collection("campaigns").document(campaign_id).collection(QUEUE_ITEMS_COLL)
        batch = db.batch()
        ops = 0
        for u in _normalize_urls(urls):
            ref = col.document(url_item_id(u))
            batch.set(
                ref,
                {
                    "url": u,
                    "source": source,
                    "status": STATUS_QUEUED,
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
            try:
                log.warning(
                    "campaign_queue_dual_write_failed",
                    campaign_id=campaign_id,
                    error=str(exc),
                )
            except Exception:
                pass
        return 0
    return written


def mark_queue_items_consumed(
    db: Any,
    campaign_id: str,
    urls: list[str],
    *,
    log: Optional[Any] = None,
) -> None:
    if not campaign_id or not urls or db is None:
        return
    try:
        from google.cloud import firestore  # type: ignore[import]

        col = db.collection("campaigns").document(campaign_id).collection(QUEUE_ITEMS_COLL)
        for u in _normalize_urls(urls):
            try:
                col.document(url_item_id(u)).set(
                    {
                        "status": STATUS_CONSUMED,
                        "consumedAt": firestore.SERVER_TIMESTAMP,
                        "updatedAt": firestore.SERVER_TIMESTAMP,
                    },
                    merge=True,
                )
            except Exception:
                pass
    except Exception as exc:
        if log:
            try:
                log.warning(
                    "campaign_queue_mark_consumed_failed",
                    campaign_id=campaign_id,
                    error=str(exc),
                )
            except Exception:
                pass


def append_urls(
    db: Any,
    campaign_ref: Any,
    campaign_id: str,
    urls: list[str],
    *,
    source: str = "produce",
    campaign_doc: Optional[dict] = None,
    log: Optional[Any] = None,
) -> dict[str, Any]:
    """
    Append URLs with backpressure. Dual-write in hybrid/subcollection modes.

    Returns: {appended: int, depth_before: int, depth_after: int, skipped: str|None}
    """
    from google.cloud import firestore  # type: ignore[import]

    clean = _normalize_urls(urls)
    if not clean:
        return {"appended": 0, "depth_before": 0, "depth_after": 0, "skipped": "empty"}

    mode = queue_mode()
    cap = _hard_cap()
    bp = _backpressure()
    camp = campaign_doc
    if camp is None and campaign_ref is not None:
        try:
            camp = (campaign_ref.get().to_dict() or {})
        except Exception:
            camp = {}
    camp = camp or {}

    depth_before = queue_depth(db, campaign_id, camp, log=log)
    if depth_before >= bp:
        if log:
            try:
                log.info(
                    "campaign_queue_backpressure",
                    campaign_id=campaign_id,
                    depth=depth_before,
                    threshold=bp,
                )
            except Exception:
                pass
        return {
            "appended": 0,
            "depth_before": depth_before,
            "depth_after": depth_before,
            "skipped": "backpressure",
        }

    room = max(cap - depth_before, 0)
    to_add = clean[:room]
    if not to_add:
        return {
            "appended": 0,
            "depth_before": depth_before,
            "depth_after": depth_before,
            "skipped": "at_cap",
        }

    # Write subcollection first (authoritative under subcollection/hybrid)
    if mode != "array":
        dual_write_queue_items(db, campaign_id, to_add, source=source, log=log)

    # Array write for hybrid/array (BC + atomic ArrayUnion)
    if mode != "subcollection" and campaign_ref is not None:
        try:
            campaign_ref.update({"unprocessed_queue": firestore.ArrayUnion(to_add)})
            # Trim defense-in-depth
            try:
                post = list((campaign_ref.get().to_dict() or {}).get("unprocessed_queue") or [])
                if len(post) > cap:
                    campaign_ref.update({"unprocessed_queue": post[:cap]})
            except Exception:
                pass
        except Exception as exc:
            if log:
                try:
                    log.warning(
                        "campaign_queue_array_append_failed",
                        campaign_id=campaign_id,
                        error=str(exc),
                    )
                except Exception:
                    pass
            # If array fails but items written, still OK in hybrid
            if mode == "array":
                return {
                    "appended": 0,
                    "depth_before": depth_before,
                    "depth_after": depth_before,
                    "skipped": "array_write_failed",
                }

    # subcollection mode: actively shrink array toward empty to reduce 1MB risk
    if mode == "subcollection" and campaign_ref is not None:
        try:
            arr = list(camp.get("unprocessed_queue") or [])
            if arr:
                # Keep array only as drain mirror of what's already in items
                campaign_ref.update({"unprocessed_queue": []})
                if log:
                    try:
                        log.info(
                            "campaign_queue_array_cleared",
                            campaign_id=campaign_id,
                            cleared=len(arr),
                            note="subcollection mode — parent array cleared after item write",
                        )
                    except Exception:
                        pass
        except Exception:
            pass

    depth_after = depth_before + len(to_add)
    return {
        "appended": len(to_add),
        "depth_before": depth_before,
        "depth_after": depth_after,
        "skipped": None,
        "approx_bytes": approx_queue_bytes(to_add),
        "mode": mode,
    }


def pop_batch(
    db: Any,
    campaign_ref: Any,
    campaign_id: str,
    campaign_doc: Optional[dict],
    *,
    batch_size: int = 10,
    log: Optional[Any] = None,
) -> list[str]:
    """
    Destructive pop of up to batch_size URLs.
    - Always ArrayRemove from parent array (idempotent if empty/missing)
    - Always mark queue_items consumed
    """
    from google.cloud import firestore  # type: ignore[import]

    camp = campaign_doc if isinstance(campaign_doc, dict) else {}
    current = load_queued_urls(db, campaign_id, camp, log=log)
    if not current:
        return []
    batch_urls = current[: max(1, int(batch_size))]

    # ArrayRemove (safe no-op if URLs not in array)
    if campaign_ref is not None:
        try:
            campaign_ref.update({"unprocessed_queue": firestore.ArrayRemove(batch_urls)})
        except Exception as exc:
            if log:
                try:
                    log.warning(
                        "campaign_queue_array_remove_failed",
                        campaign_id=campaign_id,
                        error=str(exc),
                    )
                except Exception:
                    pass

    mark_queue_items_consumed(db, campaign_id, batch_urls, log=log)
    return batch_urls
