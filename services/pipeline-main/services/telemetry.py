"""
Pipeline-main — Circuit breaker telemetry service.

Extracted from ``main.py:_update_circuit_telemetry()``.

Maintains a 15-minute sliding window of Serper 429 and scraper OOM error
rates in Firestore ``system_telemetry/circuit_breaker_state``.  The
orchestrator's cron sweep reads these counters before dispatching tasks.

Design contract:
  - ALL exceptions are swallowed — telemetry failure must NEVER block the pipeline.
  - Uses ``get_db()`` lazy accessor — never opens a gRPC channel at import time.

SF-009 FIX (2026-04-18):
  The original implementation used a non-atomic read-then-write pattern:
    1. cb_ref.get()            → read window_reset_at
    2. cb_ref.set({...})       → reset counters (if window expired)
    3. cb_ref.set(updates, merge=True) → apply increment

  Two concurrent Gunicorn threads arriving simultaneously both read the same
  snapshot, both see elapsed > window, and both execute the full reset (step 2).
  The second reset overwrites the first's increment (step 3) → counter loss.

  Fix: Wrap the entire read-check-reset-increment sequence in a
  @google.cloud.firestore.transactional function. Firestore transactions
  use optimistic concurrency — the second concurrent call retries instead
  of silently overwriting.
"""
from __future__ import annotations

import datetime
import os

from core.logging import get_logger  # type: ignore[import]
from core.clients import get_db  # type: ignore[import]

log = get_logger("pipeline.telemetry")

_CB_WINDOW_MINUTES = int(os.environ.get("CB_WINDOW_MINUTES", "15"))


def update_circuit_telemetry(event_type: str) -> None:
    """Atomically update the circuit breaker sliding-window counters in Firestore.

    SF-009 FIX: The window reset + increment are now wrapped in a single
    @firestore.transactional function, preventing concurrent workers from
    racing on the read-then-set pattern and corrupting window_reset_at.

    Args:
        event_type: One of ``"serper_call"``, ``"serper_429"``,
                    ``"scraper_call"``, ``"scraper_oom"``.
    """
    _increment_map = {
        "serper_call":  {"serper_calls_window":  _sentinel_increment(1)},
        "serper_429":   {"serper_calls_window":  _sentinel_increment(1),
                         "serper_429s_window":   _sentinel_increment(1)},
        "scraper_call": {"scraper_calls_window": _sentinel_increment(1)},
        "scraper_oom":  {"scraper_calls_window": _sentinel_increment(1),
                         "scraper_ooms_window":  _sentinel_increment(1)},
    }
    updates = _increment_map.get(event_type)
    if not updates:
        return

    try:
        from google.cloud import firestore  # type: ignore[import]

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        cb_ref  = get_db().collection("system_telemetry").document("circuit_breaker_state")

        # SF-009 FIX: Wrap read-check-reset-increment in a transaction.
        # Without this, two concurrent workers both see elapsed > window,
        # both execute the full reset, and the second reset silently discards
        # the first worker's increment — corrupting the sliding window counters.
        @firestore.transactional
        def _atomic_update(txn: firestore.Transaction) -> None:
            snap    = cb_ref.get(transaction=txn)
            cb_data = snap.to_dict() if snap.exists else {}

            window_reset = cb_data.get("window_reset_at")
            if window_reset:
                # Normalise naive datetimes from Firestore (stored without tzinfo)
                if hasattr(window_reset, "tzinfo") and window_reset.tzinfo is None:
                    window_reset = window_reset.replace(tzinfo=datetime.timezone.utc)
                elapsed = (now_utc - window_reset).total_seconds()

                if elapsed > _CB_WINDOW_MINUTES * 60:
                    # Window expired — reset all counters atomically
                    txn.set(cb_ref, {
                        "serper_calls_window":  0,
                        "serper_429s_window":   0,
                        "scraper_calls_window": 0,
                        "scraper_ooms_window":  0,
                        "window_reset_at":      now_utc,
                    }, merge=False)
                    # Apply the current event's increment on top of the reset
                    txn.set(cb_ref, updates, merge=True)
                    return

            # Window still valid (or first write) — just apply the increment.
            # Firestore Increment sentinels are safe inside transactions.
            if not snap.exists:
                updates["window_reset_at"] = now_utc
            txn.set(cb_ref, updates, merge=True)

        txn = get_db().transaction()
        _atomic_update(txn)

    except Exception as tel_err:
        log.warning("circuit_telemetry_write_failed",
                    event_type=event_type, error=str(tel_err))


def _sentinel_increment(n: int):
    """Return a Firestore Increment sentinel without importing at module scope."""
    from google.cloud import firestore  # type: ignore[import]
    return firestore.Increment(n)
