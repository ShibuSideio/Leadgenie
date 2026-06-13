"""
Pipeline-main — GCP client singleton factory.

Identical pattern to orchestrator/core/clients.py.

Phase 3 fix (M-5 / hot-loop audit):
  The Serper API key is fetched from Secret Manager ONCE at first call and
  cached in ``_serper_key_cache``.  Subsequent calls return the cached value.
  This eliminates the per-query Secret Manager RPC that was adding 20-100 ms
  of latency per Serper call in the scraping hot loop.

V23 Hardening (Task 2 — gRPC Lazy Init):
  get_db(), get_secret_manager_client(), and init_vertex() use threading.Lock
  double-checked locking instead of bare lru_cache.  Under Gunicorn's gthread
  worker class, two threads can simultaneously observe a lru_cache miss and
  both call the gRPC constructor — producing two stubs sharing the same
  underlying channel, corrupting state and causing pre-fork deadlocks.
  The Lock serialises first-call construction; all subsequent calls return the
  cached singleton instantly with zero contention.
"""
from __future__ import annotations

import functools
import os
import threading
from typing import Optional

from google.cloud import firestore, bigquery, tasks_v2, secretmanager, storage
import vertexai

# ---------------------------------------------------------------------------
# Serper API key — threading.Lock double-checked locking (Postmortem Fix #14)
#
# Previously: bare global with no lock — two Gunicorn gthreads could race on
# cold start, both call Secret Manager simultaneously, defeating the cache.
# Fix: identical DCL pattern used by get_db(), get_bq_client(), etc.
# ---------------------------------------------------------------------------
_serper_key_cache: Optional[str] = None
_serper_key_lock: threading.Lock  = threading.Lock()


def get_serper_key(secret_name: Optional[str] = None) -> str:
    """Return the Serper API key, fetching from Secret Manager on first call.

    The key is cached in a module-level variable for the lifetime of the
    container process.  This eliminates the per-query RPC that was adding
    latency in the scraping hot loop (M-5 audit finding).

    Postmortem Fix #14: Uses threading.Lock double-checked locking (DCL)
    so that exactly one Secret Manager RPC is made per container lifetime,
    even under Gunicorn gthread concurrency.

    Args:
        secret_name: Full Secret Manager resource name.  Defaults to
            ``projects/{PROJECT_ID}/secrets/serper_api_key/versions/latest``.

    Returns:
        Serper API key string.

    Raises:
        RuntimeError: If the Secret Manager call fails.
    """
    global _serper_key_cache
    # Fast path — no lock needed if cache is warm
    if _serper_key_cache is not None:
        return _serper_key_cache

    with _serper_key_lock:
        # Double-checked inside lock to prevent race on cold start
        if _serper_key_cache is not None:
            return _serper_key_cache
        _project = os.environ.get("PROJECT_ID", "sideio-leads-v16")
        _name = secret_name or f"projects/{_project}/secrets/serper_api_key/versions/latest"
        try:
            sm = get_secret_manager_client()
            response = sm.access_secret_version(request={"name": _name})
            _serper_key_cache = response.payload.data.decode("UTF-8").strip()
            return _serper_key_cache
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch Serper key from Secret Manager: {exc}") from exc


# ---------------------------------------------------------------------------
# Firestore — threading.Lock double-checked locking (V23 Task 2 fix)
# ---------------------------------------------------------------------------
_db_lock: threading.Lock = threading.Lock()
_db_instance: Optional[firestore.Client] = None


def get_db() -> firestore.Client:
    """Return the shared Firestore client (lazy, thread-safe).

    Uses double-checked locking to guarantee exactly one ``firestore.Client``
    is constructed per process even under Gunicorn gthread workers.

    Returns:
        :class:`google.cloud.firestore.Client`
    """
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:  # re-check inside lock
                _db_instance = firestore.Client()
    return _db_instance


# ---------------------------------------------------------------------------
# BigQuery — threading.Lock double-checked locking (V23 Amendment 3)
# ---------------------------------------------------------------------------
_bq_lock: threading.Lock = threading.Lock()
_bq_instance: Optional[bigquery.Client] = None


def get_bq_client() -> bigquery.Client:
    """Return the shared BigQuery client (lazy, thread-safe), pinned to asia-south1.

    Upgraded from lru_cache to threading.Lock DCL to prevent concurrent
    gRPC constructor races under Gunicorn gthread workers (V23 Amendment 3).

    REGIONALITY FIX (2026-04-28):
    Without an explicit location the BQ SDK defaults to US, causing
    "Not found" (Code 5) for datasets provisioned in asia-south1.

    Returns:
        :class:`google.cloud.bigquery.Client`
    """
    global _bq_instance
    if _bq_instance is None:
        with _bq_lock:
            if _bq_instance is None:
                _bq_instance = bigquery.Client(
                    project=os.environ.get("PROJECT_ID", "sideio-leads-v16"),
                    location="asia-south1",
                )
    return _bq_instance


# ---------------------------------------------------------------------------
# Cloud Tasks — threading.Lock double-checked locking (V23 Amendment 3)
# ---------------------------------------------------------------------------
_tasks_lock: threading.Lock = threading.Lock()
_tasks_instance: Optional[tasks_v2.CloudTasksClient] = None


def get_tasks_client() -> tasks_v2.CloudTasksClient:
    """Return the shared Cloud Tasks client (lazy, thread-safe).

    Upgraded from lru_cache to threading.Lock DCL (V23 Amendment 3).

    Returns:
        :class:`google.cloud.tasks_v2.CloudTasksClient`
    """
    global _tasks_instance
    if _tasks_instance is None:
        with _tasks_lock:
            if _tasks_instance is None:
                _tasks_instance = tasks_v2.CloudTasksClient()
    return _tasks_instance


# ---------------------------------------------------------------------------
# Secret Manager — threading.Lock double-checked locking (V23 Task 2 fix)
# ---------------------------------------------------------------------------
_sm_lock: threading.Lock = threading.Lock()
_sm_instance: Optional[secretmanager.SecretManagerServiceClient] = None


def get_secret_manager_client() -> secretmanager.SecretManagerServiceClient:
    """Return the shared Secret Manager client (lazy, thread-safe).

    Uses double-checked locking — see get_db() rationale.

    Returns:
        :class:`google.cloud.secretmanager.SecretManagerServiceClient`
    """
    global _sm_instance
    if _sm_instance is None:
        with _sm_lock:
            if _sm_instance is None:
                _sm_instance = secretmanager.SecretManagerServiceClient()
    return _sm_instance


# Alias used by service modules for consistent naming
get_sm_client = get_secret_manager_client


# ---------------------------------------------------------------------------
# GCS
# ---------------------------------------------------------------------------
def get_gcs_client() -> storage.Client:
    """Return a GCS client (not cached — used infrequently).

    Returns:
        :class:`google.cloud.storage.Client`
    """
    return storage.Client()


# ---------------------------------------------------------------------------
# Vertex AI — threading.Lock double-checked locking (V23 Task 2 fix)
# ---------------------------------------------------------------------------
_vertex_lock: threading.Lock = threading.Lock()
_vertex_initialised: bool = False


def init_vertex() -> None:
    """Initialise Vertex AI SDK (idempotent, thread-safe).

    Uses double-checked locking to guarantee ``vertexai.init()`` is called
    exactly once per process, preventing concurrent gRPC channel setup races
    under Gunicorn gthread workers.

    Returns:
        None
    """
    global _vertex_initialised
    if not _vertex_initialised:
        with _vertex_lock:
            if not _vertex_initialised:
                _project  = os.environ.get("PROJECT_ID", "trendpulse-app-2025")
                _location = os.environ.get("LOCATION", "asia-south1")
                vertexai.init(project=_project, location=_location)
                _vertex_initialised = True
