"""
Orchestrator — Firestore serialization utilities.

Problem:
  Firestore's Python SDK silently drops (or raises on some SDK versions) update()
  calls that contain Python ``datetime`` objects when the SDK expects either a
  Firestore sentinel (``SERVER_TIMESTAMP``, ``Increment``) or a plain Python
  primitive / ISO-8601 string.

Solution:
  ``sanitize_update(updates)`` — call this before every ``db.collection(...).update()``
  to normalise any ``datetime`` values in the payload to ``.isoformat()`` strings.

Sentinel safety:
  GCP sentinel objects (``firestore.SERVER_TIMESTAMP``, ``firestore.Increment``,
  ``firestore.ArrayUnion``, ``firestore.ArrayRemove``, ``firestore.DELETE_FIELD``)
  are NOT ``datetime`` instances.  ``to_firestore_ts`` detects them via their
  ``_document_path`` or ``_value`` attribute (all GCP sentinel classes carry one
  of these) and returns them **untouched**.  This preserves atomic server-side
  operations that would be destroyed if cast to a string.

Legacy DatetimeWithNanoseconds read path:
  Firestore documents returned by ``doc.to_dict()`` may contain
  ``DatetimeWithNanoseconds`` objects (a ``datetime`` subclass with an extra
  ``.nanoseconds`` attribute and a ``.timestamp()`` method).  These ARE datetime
  instances and are therefore safely serialised via ``.isoformat()``.
  The ``hasattr(value, "timestamp")`` guard mentioned in the sweep logic is
  preserved here: if a value is not a ``datetime`` but has a ``.timestamp``
  callable it is also serialised via ``.isoformat()`` to cover edge-cases.

Usage:
    from core.firestore_utils import sanitize_update

    db.collection("campaigns").document(campaign_id).update(
        sanitize_update({"next_produce_due": some_datetime, "status": "active"})
    )
"""
from __future__ import annotations

import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Internal sentinel detection
# ---------------------------------------------------------------------------

# All GCP Firestore sentinel objects (SERVER_TIMESTAMP, Increment, ArrayUnion,
# ArrayRemove, DELETE_FIELD) are instances of one of these internal base classes.
# We detect them by checking for well-known attributes rather than importing the
# private class directly, keeping this module dependency-free and testable without
# GCP credentials.
_SENTINEL_ATTRS = (
    "_document_path",   # present on transforms (Increment, ArrayUnion, …)
    "_value",           # present on FieldTransform sentinels in some SDK versions
    "sentinel_value",   # present on SentinelValue (DELETE_FIELD, SERVER_TIMESTAMP)
    "_marker",          # present in older SDK versions
)


def _is_firestore_sentinel(value: Any) -> bool:
    """Return True if *value* is a Firestore sentinel that must not be cast.

    Checked attributes cover all public sentinel types across SDK versions:
      - ``firestore.SERVER_TIMESTAMP``
      - ``firestore.Increment(n)``
      - ``firestore.ArrayUnion(items)``
      - ``firestore.ArrayRemove(items)``
      - ``firestore.DELETE_FIELD``

    Args:
        value: Any Python value.

    Returns:
        True when the value is a Firestore sentinel object.
    """
    if value is None:
        return False
    # Fast path: plain Python primitives are definitely not sentinels.
    if isinstance(value, (bool, int, float, str, bytes, list, dict)):
        return False
    # Check for any of the sentinel marker attributes.
    for attr in _SENTINEL_ATTRS:
        if hasattr(value, attr):
            return True
    # Fallback: class name heuristic for forward-compatibility.
    cls_name = type(value).__name__
    if any(kw in cls_name for kw in ("Sentinel", "Transform", "Increment",
                                      "ArrayUnion", "ArrayRemove", "FieldTransform",
                                      "DeleteField", "ServerTimestamp")):
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def to_firestore_ts(value: Any) -> Any:
    """Convert a single value to a Firestore-safe representation.

    Conversion rules (applied in priority order):

    1. **Firestore sentinel** — returned unchanged.
    2. **``datetime.datetime``** — converted to ISO-8601 string.
       If naive (no timezone), UTC is assumed and injected before serialisation.
    3. **Non-datetime with ``.timestamp`` callable** — converted to ISO-8601
       via ``.isoformat()``.  Covers ``DatetimeWithNanoseconds`` read-path
       objects that inherit from ``datetime`` (already handled by rule 2) as well
       as any custom wrappers.
    4. **Everything else** — returned unchanged (str, int, None, list, …).

    Args:
        value: The value to normalise.

    Returns:
        ISO-8601 string if value was a datetime, otherwise *value* unchanged.

    Examples:
        >>> import datetime
        >>> to_firestore_ts(datetime.datetime(2026, 1, 1, 12, 0, 0))
        '2026-01-01T12:00:00+00:00'
        >>> to_firestore_ts("already-a-string")
        'already-a-string'
        >>> to_firestore_ts(42)
        42
    """
    # Rule 1: sentinel bypass — MUST come first.
    if _is_firestore_sentinel(value):
        return value

    # Rule 2: datetime (including DatetimeWithNanoseconds subclass).
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            # Naive datetime — assume UTC to avoid ambiguous comparisons.
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.isoformat()

    # Rule 3: non-datetime objects that look like datetimes (legacy wrappers).
    if not isinstance(value, (bool, int, float, str, bytes, list, dict, type(None))):
        if callable(getattr(value, "timestamp", None)) and hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:
                pass  # Fall through to rule 4.

    # Rule 4: passthrough.
    return value


def sanitize_update(updates: dict[str, Any]) -> dict[str, Any]:
    """Normalise all ``datetime`` values in a Firestore update payload.

    Walks the top-level key→value pairs of *updates* and applies
    ``to_firestore_ts`` to each value.  Nested dicts (rare in Firestore
    field-path update payloads) are NOT recursed into — Firestore field-path
    notation (``"wallet.reserved_credits"``) is flat by design.

    Firestore sentinel objects (``SERVER_TIMESTAMP``, ``Increment``, etc.) are
    returned **unchanged**, preserving atomic server-side operations.

    Args:
        updates: Firestore update payload dict (field-path → value).

    Returns:
        New dict with datetime values replaced by ISO-8601 strings.

    Examples:
        >>> import datetime
        >>> from google.cloud import firestore
        >>> sanitize_update({
        ...     "next_produce_due": datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc),
        ...     "status": "active",
        ...     "updatedAt": firestore.SERVER_TIMESTAMP,
        ... })
        {'next_produce_due': '2026-01-02T00:00:00+00:00', 'status': 'active', 'updatedAt': <SERVER_TIMESTAMP>}
    """
    return {k: to_firestore_ts(v) for k, v in updates.items()}


# ---------------------------------------------------------------------------
# Safe query materialization (public Retry — never touch gRPC ._retry)
# ---------------------------------------------------------------------------

def firestore_stream_retry():
    """Build an explicit public ``google.api_core.retry.Retry`` for Firestore streams.

    Why this exists
    ---------------
    ``Query.stream()`` defaults to ``retry=gapic_v1.method.DEFAULT``.  When a
    transient ``GoogleAPICallError`` occurs mid-stream, google-cloud-firestore
    tries to recover the default policy via::

        gapic_callable = transport.run_query
        retry = gapic_callable._retry   # private attribute

    Streaming RPCs are bound as ``_UnaryStreamMultiCallable``, which does **not**
    expose ``_retry``.  That produces::

        AttributeError: '_UnaryStreamMultiCallable' object has no attribute '_retry'

    Passing an explicit public ``Retry`` object skips that private-attribute path
    entirely (``_retry_query_after_exception`` uses ``retry._predicate(exc)`` on
    the object we provide).

    Returns:
        ``google.api_core.retry.Retry`` configured for common transient errors.
    """
    from google.api_core import exceptions as core_exceptions
    from google.api_core import retry as retries

    # ``timeout`` is the modern Retry kwarg (api-core ≥2.x).  Older releases
    # accepted ``deadline`` as an alias; prefer the public ``timeout`` name.
    return retries.Retry(
        predicate=retries.if_exception_type(
            core_exceptions.ServiceUnavailable,
            core_exceptions.InternalServerError,
            core_exceptions.DeadlineExceeded,
            core_exceptions.Aborted,
            core_exceptions.Unknown,
            core_exceptions.ResourceExhausted,
            core_exceptions.Cancelled,
        ),
        initial=0.5,
        maximum=30.0,
        multiplier=1.5,
        timeout=120.0,
    )


def materialize_query(
    query,
    *,
    timeout: float = 90.0,
    label: str = "firestore_query",
    empty_on_error: bool = False,
) -> list:
    """Materialize a Firestore query with a public Retry policy.

    Always passes an explicit ``retry=`` to ``.stream()`` so the SDK never
    resolves ``gapic_v1.method.DEFAULT`` via private ``callable._retry``.

    The stream is fully consumed into a list so lazy generator errors surface
    inside this helper (callers used to wrap only ``.stream()`` construction,
    which does not open the RPC).

    Args:
        query:          Firestore ``Query`` / ``CollectionReference``.
        timeout:        Per-RPC timeout seconds.
        label:          Log context tag.
        empty_on_error: When True, log and return ``[]`` instead of re-raising.

    Returns:
        List of ``DocumentSnapshot`` (may be empty).

    Raises:
        Exception: Re-raised when ``empty_on_error`` is False.
    """
    # Local import keeps firestore_utils importable without logging deps in tests.
    try:
        from core.logging import get_logger  # type: ignore[import]
        _log = get_logger("orchestrator.firestore_utils")
    except Exception:  # pragma: no cover
        import logging
        _log = logging.getLogger("orchestrator.firestore_utils")

    retry = firestore_stream_retry()
    try:
        # Explicit public Retry — never DEFAULT, never private gRPC attrs.
        return list(query.stream(retry=retry, timeout=timeout))
    except AttributeError as exc:
        # Defense in depth: if an older SDK path still hits ._retry, fall back
        # to Query.get() with the same explicit Retry (same RunQuery RPC but
        # different call site that often avoids the stream-retry helper).
        if "_retry" not in str(exc):
            if empty_on_error:
                _log.error(
                    "firestore_query_attribute_error",
                    label=label,
                    error=str(exc),
                    empty_on_error=True,
                )
                return []
            raise
        _log.warning(
            "firestore_stream_retry_attr_error",
            label=label,
            error=str(exc),
            note="Caught UnaryStreamMultiCallable/_retry bug. "
                 "Retrying once via Query.get() with explicit public Retry.",
        )
        try:
            return list(query.get(retry=retry, timeout=timeout))
        except Exception as fallback_exc:
            _log.error(
                "firestore_query_fallback_failed",
                label=label,
                error=str(fallback_exc),
                empty_on_error=empty_on_error,
                exc_info=True,
            )
            if empty_on_error:
                return []
            raise
    except Exception as exc:
        _log.error(
            "firestore_query_failed",
            label=label,
            error=str(exc),
            error_type=type(exc).__name__,
            empty_on_error=empty_on_error,
            exc_info=True,
        )
        if empty_on_error:
            return []
        raise
